"""Outer-layer attempt memory (Joshua Almeida's idea): the next round sees a
bounded summary of the previous failure; the code under test never does."""

from lumen2.actions import Action
from lumen2.agent import Agent, run_fix
from lumen2.attempts import AttemptMemory, approx_tokens
from lumen2.backends import Backend
from lumen2.backends.openai_compat import OpenAICompatBackend
from lumen2.bucket import BoundBucket
from lumen2.workspace import Workspace

BAD = "def total(n):\n    return 0  # WRONG-ATTEMPT-MARKER\n"


class Recorder(Backend):
    """Scripted stub that records every context it is shown."""
    name = "stub"

    def __init__(self, actions):
        self.actions = list(actions)
        self.contexts = []
        self.critic_contexts = []

    def start_task(self):
        pass

    def critique(self, proposal, context):
        self.critic_contexts.append(context)
        return {"fatal": False, "issues": []}

    def next_action(self, context, tools, temperature):
        self.contexts.append(context)
        if not self.actions:
            return Action("withhold", {"reason": "script done"})
        return self.actions.pop(0)


def _bad_then_probe(path, probe_code):
    return [
        Action("read_file", {"path": path}),
        Action("edit_file", {"path": path, "content": BAD}),
        Action("finish", {"summary": "try 1", "confidence": 0.9}),
        Action("run_python", {"code": probe_code}),
        Action("withhold", {"reason": "done"}),
    ]


def _target_file(target):
    return next(p.name for p in target.glob("*.py")
                if not p.name.startswith("test_"))


def test_summary_reaches_next_round_prompt(fixture_copy):
    target = fixture_copy("off_by_one")
    path = _target_file(target)
    backend = Recorder(_bad_then_probe(path, "print(1)"))
    run_fix(target, backend, max_steps=10)
    before = [c["previous_attempts"] for c in backend.contexts[:3]]
    after = backend.contexts[3]["previous_attempts"]
    assert all(not p for p in before)
    assert "attempt" in after and "finish-rejected" in after
    assert path in after
    # and it renders into the real model prompt
    rendered = OpenAICompatBackend("http://x.invalid/v1", "m")._render_state(
        backend.contexts[3])
    assert "PREVIOUS FAILED ATTEMPT" in rendered


def test_disabled_flag_keeps_prompt_clean(fixture_copy):
    target = fixture_copy("off_by_one")
    path = _target_file(target)
    backend = Recorder(_bad_then_probe(path, "print(1)"))
    run_fix(target, backend, max_steps=10, attempt_memory=False)
    assert all(not c["previous_attempts"] for c in backend.contexts)


def test_inner_sandbox_cannot_read_history(fixture_copy):
    target = fixture_copy("off_by_one")
    path = _target_file(target)
    probe = (
        "import os, pathlib\n"
        "hits = []\n"
        "for base in {os.getcwd(), str(pathlib.Path.cwd().parent)}:\n"
        "    for p in pathlib.Path(base).rglob('*'):\n"
        "        try:\n"
        "            if p.is_file() and 'finish-rejected' in p.read_text(errors='ignore'):\n"
        "                hits.append(str(p))\n"
        "        except Exception:\n"
        "            pass\n"
        "env = any('finish-rejected' in v or 'attempt 1 failed' in v\n"
        "          for v in os.environ.values())\n"
        "print('HITS', hits, 'ENV', env)\n")
    backend = Recorder(_bad_then_probe(path, probe))
    with Workspace(target) as ws:
        agent = Agent(backend, ws, task="fix")
        agent.run(max_steps=10)
        run = [h for h in agent.history if h["action"]["tool"] == "run_python"]
        assert "finish-rejected" in backend.contexts[3]["previous_attempts"]
        assert run and "HITS []" in run[0]["result"]
        assert "ENV False" in run[0]["result"]
        # nothing in the scratch dir mentions the attempt log
        for p in ws._tmp.rglob("*"):
            if p.is_file():
                assert "finish-rejected" not in p.read_text(errors="ignore")


def test_size_cap_holds():
    mem = AttemptMemory()
    for i in range(20):
        mem.record("tests-red", f"edit {i} " + "x" * 2000, "y" * 2000)
    s = mem.summary()
    assert len(mem.entries) == 2
    assert len(s) <= mem.max_total_chars
    assert "attempt 20" in s and "attempt 1 " not in s
    assert AttemptMemory(enabled=False).summary() == ""


def test_bucket_accounting_includes_summary(fixture_copy):
    target = fixture_copy("off_by_one")
    path = _target_file(target)
    backend = Recorder(_bad_then_probe(path, "print(1)"))
    bucket = BoundBucket(model="stub")
    run_fix(target, backend, max_steps=10, bucket=bucket)
    expected = sum(approx_tokens(c["previous_attempts"])
                   for c in backend.contexts + backend.critic_contexts
                   if c["previous_attempts"])
    assert bucket.attempt_memory_tokens > 0
    assert bucket.attempt_memory_tokens == expected
    assert bucket.usage()["attempt_memory_tokens"] == expected
    # it is reserved against the token cap
    bucket.limits.max_tokens = bucket.tokens + bucket.attempt_memory_tokens
    assert bucket.authorize_model_call() is not None
