"""Chat mode: every turn is a kernel run inside the bucket, on the stub."""

import shutil
from pathlib import Path

from lumen2.backends.stub import StubBackend
from lumen2.bucket import BoundBucket, Limits
from lumen2.chat import ChatSession, chat_loop, wants_change
from lumen2 import cli

FIX = Path(__file__).resolve().parents[1] / "bench" / "fixtures" / "off_by_one"


def project(tmp_path):
    target = tmp_path / "proj"
    shutil.copytree(FIX / "target", target)
    return target


def test_routing_words():
    assert wants_change("fix the failing test please")
    assert wants_change("Please change the slice bound")
    assert not wants_change("what is 2**10?")


def test_question_goes_through_ask(tmp_path):
    lines = []
    s = ChatSession(StubBackend(), out=lines.append)
    r = s.handle("what is 6*7?")
    assert r["turn_kind"] == "ask"
    assert r["bucket_usage"]["steps"] >= 1
    assert any(l.startswith("lumen2>") for l in lines)


def test_change_request_runs_the_fixer_and_writes_only_verified_diff(tmp_path):
    proj = project(tmp_path)
    before = {p.name: p.read_text() for p in proj.rglob("*.py")}
    s = ChatSession(StubBackend(), project=proj, out=lambda *_: None)
    r = s.handle("fix the failing tests")
    assert r["turn_kind"] == "fix"
    assert r["outcome"] == "committed"
    changed = [n for n, t in before.items()
               if (proj.rglob(n).__next__()).read_text() != t]
    assert changed and not any(n.startswith("test_") for n in changed)


def test_change_without_project_is_refused():
    s = ChatSession(StubBackend(), out=lambda *_: None)
    assert s.handle("/fix it")["outcome"] == "refused"


def test_session_budget_is_shared_and_never_loosened(tmp_path):
    base = lambda: BoundBucket(Limits(max_steps=3), model="stub")
    s = ChatSession(StubBackend(), bucket_factory=base, out=lambda *_: None)
    s.handle("what is 1+1?")
    left = s.remaining()["max_steps"]
    assert left < 3
    assert s.turn_bucket().limits.max_steps == left
    s.spent["steps"] = 3
    assert s.handle("what is 2+2?")["outcome"] == "refused"


def test_test_edit_request_still_blocked(tmp_path):
    """Asking chat to cheat goes through the same gate: tests stay read-only."""
    proj = project(tmp_path)
    tests_before = {p: p.read_text() for p in proj.rglob("test_*.py")}
    s = ChatSession(StubBackend(), project=proj, out=lambda *_: None)
    s.handle("change the test file so it passes")
    assert all(p.read_text() == t for p, t in tests_before.items())


def test_chat_loop_and_cli_default_model(monkeypatch, capsys):
    msgs = iter(["/budget", "what is 3+3?", "exit"])
    s = ChatSession(StubBackend())
    assert chat_loop(s, read=lambda _: next(msgs)) == 0
    out = capsys.readouterr().out
    assert "spent:" in out and "[session-wide]" in out
    seen = {}
    def fake_from_spec(spec, **kw):
        seen["spec"] = spec
        return StubBackend()
    monkeypatch.delenv("LUMEN2_MODEL", raising=False)
    monkeypatch.setattr("lumen2.backends.from_spec", fake_from_spec)
    monkeypatch.setattr("builtins.input", lambda _: "exit")
    assert cli.main(["chat"]) == 0
    assert seen["spec"] == "xai:grok-4.6"
