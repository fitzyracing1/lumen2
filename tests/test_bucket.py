"""BoundBucket: declared limits, breaches in the trace, tighten-never-loosen."""

import json

import pytest

from lumen2.actions import Action
from lumen2.agent import run_ask, run_fix
from lumen2.bucket import BoundBucket, Limits
from tests.test_agent import ScriptedBackend


class PaidScripted(ScriptedBackend):
    """Pretends every call costs real tokens on a paid API."""
    paid = True

    def __init__(self, actions, per_call=100_000):
        super().__init__(actions)
        self.name = "openai:gpt-4.1-mini"
        self._t = 0
        self.per_call = per_call
        self.prompt_tokens_used = 0
        self.completion_tokens_used = 0

    @property
    def tokens_used(self):
        return self._t

    def next_action(self, context, tools, temperature):
        self._t += self.per_call
        self.prompt_tokens_used += self.per_call
        return super().next_action(context, tools, temperature)


def _breaches(report):
    return report["kernel"]["bucket_breaches"]


def test_defaults_and_describe():
    b = BoundBucket()
    text = b.describe()
    for key in ("max_steps", "max_cost_usd", "network=False",
                "confidence_floor", "command_timeout", "max_edit_chars"):
        assert key in text


def test_override_from_json_and_toml(tmp_path):
    j = tmp_path / "b.json"
    j.write_text(json.dumps({"max_steps": 5, "allowed_tools": ["finish"]}))
    assert BoundBucket.from_file(j).limits.max_steps == 5
    t = tmp_path / "b.toml"
    t.write_text("[bucket]\nmax_cost_usd = 0.05\nconfidence_floor = 0.5\n")
    b = BoundBucket.from_file(t)
    assert b.limits.max_cost_usd == 0.05 and b.limits.confidence_floor == 0.5
    bad = tmp_path / "bad.json"
    bad.write_text('{"no_such_limit": 1}')
    with pytest.raises(ValueError):
        BoundBucket.from_file(bad)


def test_tighten_but_never_loosen():
    b = BoundBucket()
    assert b.tighten(max_steps=10, confidence_floor=0.5, network=False) == []
    assert b.limits.max_steps == 10
    refused = b.tighten(max_steps=100, confidence_floor=0.1, network=True,
                        allowed_tools=("read_file", "shell"))
    assert set(refused) == {"max_steps", "confidence_floor", "network",
                            "allowed_tools"}
    assert b.limits.max_steps == 10 and b.limits.confidence_floor == 0.5
    assert b.limits.network is False
    assert all(r["kind"] == "loosen" for r in b.breaches)


def test_step_limit_breach_withholds(fixture_copy):
    target = fixture_copy("off_by_one")
    agent_actions = [Action("list_files", {})] * 20
    bucket = BoundBucket(Limits(max_steps=3))
    report = run_fix(target, ScriptedBackend(agent_actions), max_steps=30,
                     bucket=bucket)
    assert report["outcome"] == "withheld"
    assert "bucket breach" in report["reason"]
    assert any(b["kind"] == "steps" for b in _breaches(report))
    assert any(e["event"] == "bucket-breach" for e in report["trace"])


def test_cost_limit_breach_withholds(fixture_copy):
    target = fixture_copy("off_by_one")
    backend = PaidScripted([Action("list_files", {})] * 20, per_call=30_000)
    bucket = BoundBucket(Limits(max_cost_usd=0.03), model=backend.name)
    report = run_fix(target, backend, max_steps=30, bucket=bucket)
    assert report["outcome"] == "withheld"
    assert any(b["kind"] == "cost" for b in _breaches(report))
    # 30k input tokens at $0.40/M = $0.012 per call: stops after 3 calls.
    assert report["kernel"]["bucket"]["model_calls"] == 3


def test_disallowed_tool_is_blocked(fixture_copy):
    target = fixture_copy("off_by_one")
    bucket = BoundBucket(Limits(allowed_tools=("list_files", "withhold")))
    backend = ScriptedBackend([Action("run_python", {"code": "print(1)"}),
                               Action("withhold", {"reason": "done"})])
    report = run_fix(target, backend, bucket=bucket)
    assert any(b["kind"] == "tool" for b in _breaches(report))
    assert report["outcome"] == "withheld"


def test_edit_size_limit(fixture_copy):
    target = fixture_copy("off_by_one")
    bucket = BoundBucket(Limits(max_edit_chars=10))
    backend = ScriptedBackend([
        Action("edit_file", {"path": "summing.py", "content": "x = 1\n" * 50}),
        Action("withhold", {"reason": "blocked"})])
    report = run_fix(target, backend, bucket=bucket)
    assert any(b["kind"] == "edit-size" for b in _breaches(report))


def test_confidence_floor_forces_withhold():
    backend = ScriptedBackend([Action("finish", {"summary": "42", "answer": "42",
                                                 "confidence": 0.2})])
    report = run_ask("what?", backend, bucket=BoundBucket(Limits(confidence_floor=0.5)))
    assert report["outcome"] == "withheld"
    assert report["answer"] == "I don't know"
    assert any(b["kind"] == "confidence-floor" for b in _breaches(report))


def test_too_many_breaches_withholds(fixture_copy):
    target = fixture_copy("off_by_one")
    backend = ScriptedBackend([Action("read_file", {"path": "../../etc/passwd"})] * 10)
    report = run_fix(target, backend, bucket=BoundBucket(Limits(max_breaches=2)))
    assert report["outcome"] == "withheld"
    assert any(b["kind"] == "too-many-breaches" for b in _breaches(report))


def test_model_call_limit(fixture_copy):
    target = fixture_copy("off_by_one")
    backend = ScriptedBackend([Action("list_files", {})] * 10)
    report = run_fix(target, backend, bucket=BoundBucket(Limits(max_model_calls=2)))
    assert report["outcome"] == "withheld"
    assert any(b["kind"] == "model-calls" for b in _breaches(report))


def test_reused_backend_spend_is_per_run(tmp_path):
    """A backend reused across runs (as the benchmark does) must not carry
    earlier runs' tokens or cost into the next run's budgets."""
    import shutil
    from pathlib import Path
    from lumen2.agent import run_fix
    from lumen2.backends.stub import StubBackend
    fx = Path(__file__).resolve().parents[1] / "bench" / "fixtures" / "off_by_one"
    target = tmp_path / "t"
    shutil.copytree(fx / "target", target)
    class Reused(StubBackend):
        @property
        def tokens_used(self):            # pretend earlier runs spent a lot
            return 10_000_000 + super().tokens_used
    backend = Reused()
    b = BoundBucket(model="stub")
    report = run_fix(str(target), backend=backend, bucket=b)
    assert report["outcome"] == "committed", report.get("reason")
    assert report["kernel"]["tokens_used"] < 1_000_000
    assert b.usage()["tokens"] < 1_000_000
