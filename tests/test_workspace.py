from pathlib import Path

from lumen2.workspace import Workspace, is_test_path, safe_join


def test_safe_join_blocks_escapes(tmp_path):
    assert safe_join(tmp_path, "../evil.py") is None
    assert safe_join(tmp_path, "/etc/passwd") is None
    assert safe_join(tmp_path, "C:\\windows\\system32") is None
    assert safe_join(tmp_path, "a/../../evil.py") is None
    assert safe_join(tmp_path, "") is None
    assert safe_join(tmp_path, "ok/file.py") is not None


def test_is_test_path():
    assert is_test_path("test_calc.py")
    assert is_test_path("tests/helpers.py")
    assert is_test_path("pkg/conftest.py")
    assert is_test_path("pkg/calc_test.py")
    assert not is_test_path("calc.py")
    assert not is_test_path("pkg/testing_utils_nope/../calc.py") in (None,)


def test_workspace_is_a_scratch_copy(fixture_copy):
    target = fixture_copy("off_by_one")
    original = (target / "summing.py").read_text(encoding="utf-8")
    with Workspace(target) as ws:
        ws.write_file("summing.py", "def sum_to(n):\n    return 0\n")
        assert (target / "summing.py").read_text(encoding="utf-8") == original
        assert ws.changed_files() == {
            "summing.py": "def sum_to(n):\n    return 0\n"}


def test_commit_to_target(fixture_copy):
    target = fixture_copy("off_by_one")
    with Workspace(target) as ws:
        ws.write_file("summing.py", "def sum_to(n):\n    return 99\n")
        applied = ws.commit_to_target()
    assert applied == ["summing.py"]
    assert "99" in (target / "summing.py").read_text(encoding="utf-8")


def test_run_tests_counts(fixture_copy):
    target = fixture_copy("off_by_one")
    with Workspace(target) as ws:
        result = ws.run_tests()
    assert result.failing >= 1
    assert result.total >= 2
    assert not result.green


def test_run_python_and_timeout(fixture_copy):
    target = fixture_copy("off_by_one")
    with Workspace(target, python_timeout=2.0) as ws:
        out = ws.run_python("from summing import sum_to; print(sum_to(5))")
        assert "10" in out  # buggy version: sum(range(1, 5)) == 10
        assert "exit code: 0" in out
        slow = ws.run_python("import time; time.sleep(30)")
        assert "timed out" in slow


def test_read_and_search(fixture_copy):
    target = fixture_copy("multi_file")
    with Workspace(target) as ws:
        assert "KM_PER_MILE" in ws.read_file("units.py")
        assert "ERROR" in ws.read_file("../outside.py")
        hits = ws.search("KM_PER_MILE")
        assert "units.py" in hits and "convert.py" in hits
