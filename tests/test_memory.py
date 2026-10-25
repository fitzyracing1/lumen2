from lumen2.agent import run_fix
from lumen2.backends.stub import StubBackend
from lumen2.memory import Memory, signature


def test_signature_extracts_error_names_and_identifiers():
    sig = signature("E   AssertionError: assert sum_to(5) == 15\n"
                    "summing.py TypeError in render")
    assert "AssertionError" in sig
    assert "TypeError" in sig
    assert "sum_to" in sig


def test_record_and_relevant_retrieval(tmp_path):
    mem = Memory(tmp_path / "mem.json")
    mem.record("fixed", "AssertionError sum_to range off by one",
               "fixed by extending the range end")
    mem.record("dead-end", "KeyError profile city missing",
               "swapping operators did nothing")
    hits = mem.relevant("assert sum_to(2) == 3  range")
    assert hits and "range end" in hits[0]
    # The irrelevant lesson is not ranked first.
    assert "operators" not in hits[0]


def test_memory_persists_and_resets(tmp_path):
    path = tmp_path / "mem.json"
    mem = Memory(path)
    mem.record("fixed", "ValueError parse_port", "added the range check")
    mem.end_run()
    again = Memory(path)
    assert again.data["runs"] == 1
    assert len(again.data["lessons"]) == 1
    again.reset()
    assert not path.exists()
    assert Memory(path).data["lessons"] == []


def test_corrupt_memory_file_is_empty_memory(tmp_path):
    path = tmp_path / "mem.json"
    path.write_text("{not json", encoding="utf-8")
    assert Memory(path).data["lessons"] == []


def test_memory_stores_no_disposition(tmp_path):
    """v2 memory may store lessons only — never beliefs, policy, or
    thresholds (the v1 failure mode where memory froze the kernel)."""
    mem = Memory(tmp_path / "mem.json")
    mem.record("fixed", "failure text", "a lesson")
    mem.end_run()
    assert set(mem.data.keys()) == {"version", "lessons", "runs"}


def test_memory_does_not_suppress_action(fixture_copy, tmp_path):
    """After recording failures, a run with memory still acts and still
    fixes a fixable bug."""
    mem = Memory(tmp_path / "mem.json")
    for i in range(10):
        mem.record("dead-end", f"AssertionError sum_to case {i}",
                   f"candidate rewrite {i} failed the tests")
    target = fixture_copy("off_by_one")
    report = run_fix(target, StubBackend(), memory=mem, max_steps=25)
    assert report["outcome"] == "committed"
