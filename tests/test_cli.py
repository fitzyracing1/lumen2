import json
import subprocess
import sys

from lumen2.cli import main


def test_fix_command(fixture_copy, tmp_path, capsys):
    target = fixture_copy("off_by_one")
    out = tmp_path / "report.json"
    code = main(["fix", str(target), "--out", str(out)])
    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["outcome"] == "committed"
    assert "FIXED" in capsys.readouterr().out


def test_fix_command_exit_1_on_withhold(fixture_copy, capsys):
    target = fixture_copy("impossible")
    assert main(["fix", str(target)]) == 1
    assert "WITHHELD" in capsys.readouterr().out


def test_ask_command(capsys):
    assert main(["ask", "what is 6 * 7?"]) == 0
    out = capsys.readouterr().out
    assert "42" in out and "confidence" in out


def test_forget_command(tmp_path, capsys):
    mem = tmp_path / "mem.json"
    mem.write_text('{"version": 2, "lessons": [], "runs": 0}', encoding="utf-8")
    assert main(["forget", str(mem)]) == 0
    assert not mem.exists()


def test_module_entrypoint_utf8():
    # Must not crash on Windows-ish consoles; force a strict ASCII stdout.
    proc = subprocess.run(
        [sys.executable, "-m", "lumen2", "ask", "what is 2 + 2?"],
        capture_output=True, text=True, encoding="ascii", errors="replace",
        env={"PYTHONIOENCODING": "ascii", "PATH": __import__("os").environ["PATH"],
             "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
        timeout=120)
    assert proc.returncode == 0
    assert "4" in proc.stdout
