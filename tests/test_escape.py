"""Escape tests: each containment layer must block on its own.

Bot > BoundBucket > outer sandbox > inner sandbox > executed code
"""

import sys
from pathlib import Path

import pytest

from lumen2.actions import Action
from lumen2.agent import run_fix
from lumen2.bucket import BoundBucket, Limits
from lumen2.workspace import Workspace
from tests.test_agent import ScriptedBackend


def _layers(bucket):
    return {b["layer"] for b in bucket.breaches}


@pytest.mark.parametrize("inner", [True, False])
def test_code_cannot_write_outside_workspace(fixture_copy, tmp_path, inner):
    target = fixture_copy("off_by_one")
    outside = tmp_path / "escaped.txt"
    bucket = BoundBucket()
    with Workspace(target, bucket=bucket, inner_enabled=inner) as ws:
        out = ws.run_python(f"open({str(outside)!r}, 'w').write('pwned')")
    assert not outside.exists()
    expected = "inner" if inner else "outer"   # outer still holds alone
    assert f"LUMEN2-BLOCK[{expected}]" in out
    assert expected in _layers(bucket)


@pytest.mark.parametrize("inner", [True, False])
def test_code_cannot_modify_real_target(fixture_copy, inner):
    target = fixture_copy("off_by_one")
    original = (target / "summing.py").read_text(encoding="utf-8")
    bucket = BoundBucket()
    with Workspace(target, bucket=bucket, inner_enabled=inner) as ws:
        ws.run_python(f"open({str(target / 'summing.py')!r}, 'a').write('# x')")
    assert (target / "summing.py").read_text(encoding="utf-8") == original


def test_outer_integrity_check_reverts_a_bypass(fixture_copy):
    """Even if both guards were bypassed (e.g. native code), the outer layer
    notices the real target changed outside export and reverts it."""
    target = fixture_copy("off_by_one")
    original = (target / "summing.py").read_text(encoding="utf-8")
    bucket = BoundBucket()
    with Workspace(target, bucket=bucket) as ws:
        (target / "summing.py").write_text("tampered", encoding="utf-8")
        (target / "dropped.py").write_text("evil", encoding="utf-8")
        ws.run_python("print('hi')")
    assert (target / "summing.py").read_text(encoding="utf-8") == original
    assert not (target / "dropped.py").exists()
    assert any("reverted" in b["detail"] and b["layer"] == "outer"
               for b in bucket.breaches)


def test_inner_layer_confines_writes_to_its_own_copy(fixture_copy):
    """With the inner layer on, even the outer scratch copy is off limits:
    each run gets its own throwaway copy."""
    target = fixture_copy("off_by_one")
    bucket = BoundBucket()
    with Workspace(target, bucket=bucket) as ws:
        out = ws.run_python(
            f"open({str(ws.root / 'summing.py')!r}, 'w').write('x')")
        assert "LUMEN2-BLOCK[inner]" in out
        assert "x" != (ws.root / "summing.py").read_text(encoding="utf-8")
        # relative writes land in the run's own copy and vanish
        ws.run_python("open('note.txt', 'w').write('temp')")
        assert not (ws.root / "note.txt").exists()


@pytest.mark.parametrize("inner", [True, False])
def test_network_is_blocked(fixture_copy, inner):
    target = fixture_copy("off_by_one")
    bucket = BoundBucket()
    with Workspace(target, bucket=bucket, inner_enabled=inner) as ws:
        out = ws.run_python(
            "import socket\n"
            "s = socket.socket()\n"
            "s.settimeout(2)\n"
            "s.connect(('93.184.215.14', 80))\n"
            "print('CONNECTED')")
    assert "CONNECTED" not in out
    assert "network access" in out


def test_inner_blocks_process_spawn(fixture_copy):
    target = fixture_copy("off_by_one")
    with Workspace(target, bucket=BoundBucket()) as ws:
        out = ws.run_python("import os; os.system('echo spawned')")
    assert "stdout:\nspawned" not in out
    assert "LUMEN2-BLOCK[inner]: process spawn" in out


def test_secrets_are_not_visible_to_executed_code(fixture_copy, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    target = fixture_copy("off_by_one")
    with Workspace(target, bucket=BoundBucket()) as ws:
        out = ws.run_python("import os; print(os.environ.get('OPENAI_API_KEY'))")
    assert "sk-should-not-leak" not in out


def test_timeout_kills_runaway_code(fixture_copy):
    target = fixture_copy("off_by_one")
    bucket = BoundBucket(Limits(command_timeout=2))
    with Workspace(target, bucket=bucket) as ws:
        out = ws.run_python("while True: pass")
    assert "timed out" in out
    assert any("timed out" in b["detail"] for b in bucket.breaches)


def test_agent_cannot_edit_tests_bucket_and_critic_both_block(fixture_copy):
    target = fixture_copy("off_by_one")
    test_file = next(p for p in target.iterdir() if p.name.startswith("test_"))
    before = test_file.read_text(encoding="utf-8")
    backend = ScriptedBackend([
        Action("edit_file", {"path": test_file.name, "content": "def test_x():\n    pass\n"}),
        Action("withhold", {"reason": "blocked"})])
    report = run_fix(target, backend)
    assert test_file.read_text(encoding="utf-8") == before
    kinds = {b["kind"] for b in report["kernel"]["bucket_breaches"]}
    assert "read-only" in kinds
    assert report["kernel"]["stats"].get("cheat:edit-tests") == 1


def test_agent_cannot_edit_outside_workspace(fixture_copy, tmp_path):
    target = fixture_copy("off_by_one")
    victim = tmp_path / "victim.py"
    backend = ScriptedBackend([
        Action("edit_file", {"path": str(victim), "content": "evil"}),
        Action("edit_file", {"path": "../victim.py", "content": "evil"}),
        Action("withhold", {"reason": "blocked"})])
    report = run_fix(target, backend)
    assert not victim.exists() and not (target.parent / "victim.py").exists()
    kinds = [b["kind"] for b in report["kernel"]["bucket_breaches"]]
    assert kinds.count("path-outside") == 2


def test_export_rechecks_every_file_against_the_bucket(fixture_copy):
    """Outer layer: even a diff that got into the scratch copy is re-checked
    file by file before it may leave."""
    target = fixture_copy("off_by_one")
    test_file = next(p for p in target.iterdir() if p.name.startswith("test_"))
    before = test_file.read_text(encoding="utf-8")
    bucket = BoundBucket()
    with Workspace(target, bucket=bucket) as ws:
        (ws.root / test_file.name).write_text("tampered", encoding="utf-8")
        (ws.root / "summing.py").write_text("ok = 1\n", encoding="utf-8")
        applied = ws.commit_to_target()
    assert applied == ["summing.py"]
    assert test_file.read_text(encoding="utf-8") == before
    assert any(b["kind"] == "read-only" for b in bucket.breaches)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits")
def test_rlimit_file_size(fixture_copy):
    target = fixture_copy("off_by_one")
    bucket = BoundBucket(Limits(file_size_mb=1))
    with Workspace(target, bucket=bucket) as ws:
        out = ws.run_python("open('big.bin','wb').write(b'0' * (5 * 1024 * 1024))\nprint('WROTE')")
    assert "WROTE" not in out
