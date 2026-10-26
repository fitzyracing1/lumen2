"""Attacker stage ("give it grok to attack"): extra tests written by a separate
attacker model run in the inner sandbox before any commit."""

from lumen2.actions import Action
from lumen2.agent import run_fix
from lumen2.attacker import ATTACK_FILE, StubAttacker
from lumen2.backends import Backend
from lumen2.bucket import BoundBucket
from lumen2.cli import main as cli_main

WRONG = ("def sum_to(n):\n    \"\"\"Sum 1..n.\"\"\"\n"
         "    return n * (n + 1) // 2 if n > 0 else -1\n")
RIGHT = ("def sum_to(n):\n    \"\"\"Sum 1..n.\"\"\"\n"
         "    return sum(range(1, n + 1))\n")
ATTACK_ZERO = ("from summing import sum_to\n\n\n"
               "def test_zero_is_empty_sum():\n    assert sum_to(0) == 0\n")


class Scripted(Backend):
    name = "stub"

    def __init__(self, actions):
        self.actions = list(actions)
        self.contexts = []

    def start_task(self):
        pass

    def critique(self, proposal, context):
        return {"fatal": False, "issues": []}

    def next_action(self, context, tools, temperature):
        self.contexts.append(context)
        if not self.actions:
            return Action("withhold", {"reason": "script done"})
        return self.actions.pop(0)


def _finish():
    return Action("finish", {"summary": "fixed", "confidence": 0.9})


def _edit(content):
    return Action("edit_file", {"path": "summing.py", "content": content})


def _wrong_then_right():
    return [Action("run_tests", {}), _edit(WRONG), _finish(),
            _edit(RIGHT), _finish()]


def test_attacker_failure_blocks_commit_and_reaches_attempt_memory(fixture_copy):
    target = fixture_copy("off_by_one")
    backend = Scripted(_wrong_then_right())
    attacker = StubAttacker(ATTACK_ZERO)
    blocked = []
    report = run_fix(target, backend, max_steps=12, attacker=attacker,
                     on_attack_block=lambda changed: blocked.append(changed))
    # the wrong fix was blocked (not committed), the right one committed
    assert report["outcome"] == "committed"
    assert report["kernel"]["stats"]["attacker_blocked"] == 1
    assert len(blocked) == 1 and "-1" in blocked[0]["summing.py"]
    assert "range(1, n + 1)" in (target / "summing.py").read_text()
    # the next proposal saw the attacker failure in the attempt memory
    after = [c["previous_attempts"] for c in backend.contexts[3:]]
    assert any("attacker-failed" in p and "test_zero_is_empty_sum" in p
               for p in after)
    assert "attacker" in (backend.contexts[3]["last_rejected"] or "")


def test_attacker_blocks_when_no_better_fix(fixture_copy):
    target = fixture_copy("off_by_one")
    original = (target / "summing.py").read_text()
    backend = Scripted([Action("run_tests", {}), _edit(WRONG), _finish()])
    report = run_fix(target, backend, max_steps=8,
                     attacker=StubAttacker(ATTACK_ZERO))
    assert report["outcome"] == "withheld"
    assert (target / "summing.py").read_text() == original   # nothing committed


def test_generated_tests_run_in_sandbox_and_never_exported(fixture_copy):
    target = fixture_copy("off_by_one")
    marker = ("import os\nfrom summing import sum_to\n\n\n"
              "def test_runs_in_inner_copy():\n"
              "    here = os.getcwd()\n"
              "    assert 'inner-' in here and here.endswith('run')\n"
              "    assert sum_to(3) == 6\n")
    backend = Scripted([Action("run_tests", {}), _edit(RIGHT), _finish()])
    attacker = StubAttacker(marker)
    report = run_fix(target, backend, max_steps=8, attacker=attacker)
    assert report["outcome"] == "committed"
    assert report["attacker"]["attacker_tests_run"] == 1
    assert report["files_changed"] == ["summing.py"]
    assert not (target / ATTACK_FILE).exists()
    assert not any("lumen2_attack" in p.name for p in target.rglob("*"))
    # the attacker saw the task, the diff and the visible (read-only) tests
    brief = attacker.briefs[0]
    assert "+    return sum(range(1, n + 1))" in brief["diff"]
    assert "test_summing.py" in brief["visible_tests"]


def test_bad_attacker_tests_are_dropped(fixture_copy):
    target = fixture_copy("off_by_one")
    bad = ("from summing import sum_to\nfrom nosuchmodule import x\n\n\n"
           "def test_broken():\n    assert x\n")
    wrong_spec = ("from summing import sum_to\n\n\n"
                  "def test_made_up():\n    assert sum_to(3) == 7\n")
    for src, verdict in ((bad, True), (wrong_spec, False)):
        t = fixture_copy("off_by_one")
        backend = Scripted([Action("run_tests", {}), _edit(RIGHT), _finish()])
        attacker = StubAttacker(src, verdict=verdict)
        report = run_fix(t, backend, max_steps=8, attacker=attacker)
        assert report["outcome"] == "committed", src
        assert report["attacker"]["attacker_tests_dropped"] >= 1
    # judge only consulted for the test that fails on both versions
    assert attacker.judge_calls == 1


def test_bucket_counts_attacker_calls(fixture_copy):
    target = fixture_copy("off_by_one")
    bucket = BoundBucket()
    backend = Scripted(_wrong_then_right())
    run_fix(target, backend, max_steps=12, bucket=bucket,
            attacker=StubAttacker(ATTACK_ZERO))
    assert bucket.attacker_calls == 2          # one generation per finish
    assert bucket.usage()["attacker_calls"] == 2
    proposer_calls = len(backend.contexts)
    assert bucket.model_calls >= proposer_calls + 2


def test_bucket_cap_stops_attacker(fixture_copy):
    target = fixture_copy("off_by_one")
    bucket = BoundBucket()
    bucket.limits.max_model_calls = 3          # proposer uses all three
    backend = Scripted([Action("run_tests", {}), _edit(RIGHT), _finish()])
    attacker = StubAttacker(ATTACK_ZERO)
    run_fix(target, backend, max_steps=8, bucket=bucket, attacker=attacker)
    assert attacker.calls == 0 and bucket.attacker_calls == 0


def test_no_attacker_flag_turns_it_off(fixture_copy, capsys):
    target = fixture_copy("off_by_one")
    assert cli_main(["fix", str(target), "--no-attacker"]) == 0
    out = capsys.readouterr().out
    assert "[attacker]" not in out
    target2 = fixture_copy("off_by_one")
    assert cli_main(["fix", str(target2)]) == 0      # default: on (stub)
    assert "[attacker] stub" in capsys.readouterr().out
