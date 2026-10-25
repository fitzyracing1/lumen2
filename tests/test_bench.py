"""Benchmark machinery + fixture integrity (every fixture must really be
broken, and every v2 fixture's canonical solution must really fix it)."""

import shutil
import subprocess
import sys

import pytest

from lumen2.backends.stub import StubBackend
from lumen2.bench import (FIXTURES_DIR, discover_fixtures, run_fixture,
                          summarize)


def test_discovery_finds_all_fixtures():
    fixtures = discover_fixtures()
    assert len(fixtures) == 23
    assert sum(1 for f in fixtures if not f["meta"]["expect_fix"]) == 1
    assert sum(1 for f in fixtures if f["meta"].get("difficulty") == "hard") == 10


def _pytest_ok(directory) -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=str(directory), capture_output=True, text=True, timeout=120)
    return proc.returncode == 0


@pytest.mark.parametrize("fixture", [f["name"] for f in discover_fixtures()])
def test_fixture_target_really_fails(fixture, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(FIXTURES_DIR / fixture / "target", work)
    assert not _pytest_ok(work), f"{fixture}: target tests already pass"


@pytest.mark.parametrize("fixture", [
    f["name"] for f in discover_fixtures()
    if (FIXTURES_DIR / f["name"] / "solution").is_dir()])
def test_fixture_solution_passes_visible_and_hidden(fixture, tmp_path):
    base = FIXTURES_DIR / fixture
    work = tmp_path / "w"
    shutil.copytree(base / "target", work)
    for f in (base / "solution").rglob("*"):
        if f.is_file():
            dest = work / f.relative_to(base / "solution")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, dest)
    for h in (base / "hidden").iterdir():
        shutil.copy(h, work / h.name)
    assert _pytest_ok(work), f"{fixture}: canonical solution does not pass"


def test_run_fixture_stub_fixes_wrong_operator():
    fixtures = discover_fixtures(only=["wrong_operator"])
    result = run_fixture(fixtures[0], StubBackend(), max_steps=25,
                         test_timeout=60.0, memory=None)
    assert result["outcome"] == "fixed"
    assert result["hidden_ok"] is True


def test_run_fixture_stub_withholds_on_impossible():
    fixtures = discover_fixtures(only=["impossible"])
    result = run_fixture(fixtures[0], StubBackend(), max_steps=25,
                         test_timeout=60.0, memory=None)
    assert result["outcome"] == "withheld-correct"
    assert result["correct"]


def test_summarize():
    results = [
        {"expect_fix": True, "outcome": "fixed", "difficulty": "base",
         "wrong_fix": False, "withheld": False, "correct": True,
         "cheats_caught": 1, "steps": 6, "tokens": 10, "seconds": 1.0},
        {"expect_fix": True, "outcome": "unsolved", "difficulty": "hard",
         "wrong_fix": False, "withheld": True, "correct": False,
         "cheats_caught": 0, "steps": 9, "tokens": 20, "seconds": 2.0},
        {"expect_fix": False, "outcome": "withheld-correct",
         "difficulty": "base", "wrong_fix": False, "withheld": True,
         "correct": True, "cheats_caught": 0, "steps": 4, "tokens": 5,
         "seconds": 0.5},
    ]
    s = summarize(results)
    assert s["fixed"] == 1 and s["fixable"] == 2
    assert s["impossible_handled"] is True
    assert s["cheats_caught"] == 1
    assert s["withholds"] == 2
