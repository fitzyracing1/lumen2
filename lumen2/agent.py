"""The Lumen 2 agent loop: a model picks tools, the kernel gates every step.

fix task:  work in a scratch copy of the target; tools are read/list/search,
           run_tests, run_python, edit_file, finish, withhold. finish triggers
           verification (fresh test run + final cheat scan); only a verified
           finish copies changes back to the real target.

ask task:  an empty scratch workspace; tools are run_python, finish, withhold.
           finish carries the answer and an honest confidence; the kernel
           converts low confidence into an explicit "I don't know".
"""

from __future__ import annotations

import time
from pathlib import Path

from lumen2.attempts import approx_tokens
from lumen2.actions import (Action, ALL_TOOLS, EDIT_FILE, FINISH, LIST_FILES,
                            READ_FILE, RUN_PYTHON, RUN_TESTS, SEARCH, WITHHOLD)
from lumen2.backends import Backend
from lumen2.bucket import BoundBucket
from lumen2.kernel import Kernel
from lumen2.memory import Memory
from lumen2.workspace import Workspace

FIX_TOOLS = ALL_TOOLS
ASK_TOOLS = (RUN_PYTHON, FINISH, WITHHOLD)
MAX_CONSECUTIVE_INVALID = 4
IDK = "I don't know"


class Agent:

    def __init__(self, backend: Backend, workspace: Workspace,
                 task: str = "fix", question: str = "",
                 memory: Memory | None = None, policy: dict | None = None,
                 verbose: bool = False, bucket: BoundBucket | None = None):
        self.backend = backend
        self.workspace = workspace
        self.task = task
        self.question = question
        self.memory = memory
        self.verbose = verbose
        mission = (f"answer the question: {question}" if task == "ask"
                   else f"make the test suite pass in "
                        f"{workspace.target.name if workspace.target else 'the workspace'}")
        self.kernel = Kernel(mission, workspace, policy=policy,
                             model_critic=self._model_critic)
        # The bound bucket: hard limits around everything below.
        self.bucket = (bucket or getattr(workspace, "bucket", None)
                       or BoundBucket(model=backend.name))
        if self.bucket.model == "stub":
            self.bucket.model = backend.name
        if not self.bucket.allowed_roots:
            self.bucket.confine_to(workspace.root)
        sandbox = getattr(workspace, "sandbox", None)
        if getattr(workspace, "bucket", None) is None:
            workspace.bucket = self.bucket
        if sandbox is not None and sandbox.bucket is None:
            sandbox.bucket = self.bucket
            sandbox.inner.bucket = self.bucket
        self.kernel.bucket = self.bucket
        self.bucket.on_breach = self._on_breach
        self.bucket.on_event = self.kernel.log
        for old in self.bucket.breaches:   # breaches before the agent existed
            self._on_breach(old)
        # start from the backend's current counters: a backend may be reused
        # across runs (the benchmark does), and only this run's spend counts
        self._tokens_base = backend.tokens_used
        self._charged = (backend.tokens_used,
                         getattr(backend, "prompt_tokens_used", 0),
                         getattr(backend, "completion_tokens_used", 0))
        if task == "fix":
            self.kernel.believe("fixable", "the failing tests can be fixed "
                                           "by editing the source", 0.6)
            self.kernel.believe("understood", "the cause of the failure is "
                                              "understood", 0.3)
            self.kernel.believe("verified", "the current workspace passes "
                                            "the tests", 0.1)
            self.kernel.push_goal("make the test suite pass")
        else:
            self.kernel.believe("verified", "the answer has been checked by "
                                            "running code", 0.1)
        self.history: list[dict] = []
        self.last_rejected: str | None = None
        self._read_paths: set[str] = set()
        self._baseline_failure = ""
        self._last_tests_obj = None

    # -- plumbing -----------------------------------------------------------------

    def _on_breach(self, record: dict) -> None:
        self.kernel.tally("bucket_breaches")
        self.kernel.log("bucket-breach", f"[{record['layer']}] "
                                         f"{record['kind']}: {record['detail']}")

    def _charge(self) -> None:
        """Bill the bucket for whatever the backend spent since last time."""
        b = self.backend
        total = b.tokens_used
        prompt = getattr(b, "prompt_tokens_used", 0)
        completion = getattr(b, "completion_tokens_used", 0)
        t0, p0, c0 = self._charged
        dp, dc = prompt - p0, completion - c0
        if dp + dc < total - t0:   # backend without a usage split
            dp += (total - t0) - (dp + dc)
        self.bucket.charge(dp, dc, paid=bool(getattr(b, "paid", False)))
        self._charged = (total, prompt, completion)

    def _model_critic(self, proposal: dict) -> dict | None:
        if self.bucket.authorize_model_call():
            return None   # over budget: critic is advisory, verification stands
        try:
            return self.backend.critique(proposal, self._context())
        finally:
            self._charge()

    def _say(self, text: str) -> None:
        if self.verbose:
            print(f"[lumen2] {text}", flush=True)

    def _context(self) -> dict:
        kernel = self.kernel
        lessons: list[str] = []
        if self.memory is not None:
            failure = (kernel.last_tests.output if kernel.last_tests
                       else self._baseline_failure) or self.question
            lessons = self.memory.relevant(failure)
        attempts = getattr(getattr(self.workspace, "sandbox", None),
                           "attempts", None)
        previous = attempts.summary() if attempts is not None else ""
        if previous:
            self.bucket.charge_context(approx_tokens(previous))
        return {
            "previous_attempts": previous,
            "task": self.task,
            "mission": kernel.mission,
            "question": self.question,
            "goals": kernel.open_goals(),
            "beliefs": {k: {"claim": b.claim,
                            "confidence": round(b.confidence, 3)}
                        for k, b in kernel.beliefs.items()},
            "budgets": kernel.budgets_left(),
            "last_tests": (kernel.last_tests.summary()
                           if kernel.last_tests else None),
            "lessons": lessons,
            "explore_hint": kernel.policy.get("explore_hint", False),
            "history": self.history,
            "last_rejected": self.last_rejected,
        }

    def _remember_failure(self, kind: str, what: str, why: str) -> None:
        attempts = getattr(getattr(self.workspace, "sandbox", None),
                           "attempts", None)
        if attempts is not None and self.task == "fix":
            attempts.record(kind, what, why)

    def _edit_desc(self) -> str:
        changed = sorted(self.workspace.changed_files())
        return "edited " + (", ".join(changed) if changed else "nothing")

    def _record(self, action: Action, result: str) -> None:
        self.history.append({"action": action.public(), "result": result})

    # -- tool execution ------------------------------------------------------------

    def _execute(self, action: Action) -> str:
        ws = self.workspace
        if action.tool == READ_FILE:
            return ws.read_file(action.args.get("path", ""))
        if action.tool == LIST_FILES:
            return ws.list_files()
        if action.tool == SEARCH:
            return ws.search(action.args.get("pattern", ""))
        if action.tool == RUN_PYTHON:
            return ws.run_python(action.args.get("code", ""))
        if action.tool == EDIT_FILE:
            return ws.write_file(action.args.get("path", ""),
                                 action.args.get("content", ""))
        if action.tool == RUN_TESTS:
            tests = ws.run_tests()
            head = "ALL TESTS PASS" if tests.green else "TESTS FAILING"
            if not self._baseline_failure and not tests.green:
                self._baseline_failure = tests.output
            self._last_tests_obj = tests
            return f"{head}: {tests.summary()}\n{tests.output}"
        raise AssertionError(f"unexpected tool {action.tool}")

    # -- the loop ---------------------------------------------------------------------

    def run(self, max_steps: int | None = None) -> dict:
        kernel = self.kernel
        if max_steps is not None:
            kernel.policy["max_steps"] = max_steps
        self.backend.start_task()
        tools = ASK_TOOLS if self.task == "ask" else FIX_TOOLS
        invalid_streak = 0
        outcome: dict | None = None
        started = time.monotonic()

        while outcome is None:
            breach = self.bucket.take_step()
            if breach:
                kernel.log("withhold", f"bucket: {breach['detail']}")
                outcome = self._withheld(f"bucket breach: {breach['detail']}")
                break
            reason = kernel.budget_exceeded()
            if reason:
                kernel.log("withhold", reason)
                outcome = self._withheld(f"budget exhausted: {reason}")
                break
            kernel.maybe_revise_policy()
            kernel.step += 1
            breach = self.bucket.authorize_model_call()
            if breach:
                outcome = self._withheld(f"bucket breach: {breach['detail']}")
                break
            action = self.backend.next_action(
                self._context(), tools, kernel.policy["temperature"])
            self._charge()
            kernel.tokens_used = max(
                kernel.tokens_used,
                self.backend.tokens_used - self._tokens_base)
            if action is None:
                invalid_streak += 1
                kernel.tally("invalid_actions")
                kernel.log("invalid", f"unusable model output "
                                      f"(streak {invalid_streak})")
                self.last_rejected = ("your previous reply was not a valid "
                                      "single JSON tool action; reply with "
                                      "exactly one JSON object")
                if invalid_streak >= MAX_CONSECUTIVE_INVALID:
                    outcome = self._withheld(
                        "the model kept producing unusable output; "
                        "withholding instead of guessing")
                continue
            invalid_streak = 0
            self._say(f"step {kernel.step}: {action.tool} "
                      f"{action.args.get('path', action.args.get('pattern', ''))}")

            breach = self.bucket.check_action(action.tool, action.args)
            if breach:
                feedback = (f"BLOCKED by the bound bucket ({breach['kind']}): "
                            f"{breach['detail']}")
                self.last_rejected = feedback
                self._record(action, feedback)
                if action.tool == EDIT_FILE and breach["kind"] in (
                        "read-only", "path-outside"):
                    # Let the attack bot classify it too (cheat accounting);
                    # the action stays blocked either way.
                    kernel.gate(action)
                kernel.note_progress(False)
                if breach["kind"] == "confidence-floor":
                    outcome = self._withheld(
                        f"confidence below the bucket floor: {breach['detail']}")
                continue

            decision = kernel.gate(action)
            if not decision.allowed:
                feedback = decision.feedback()
                self.last_rejected = feedback
                self._record(action, feedback)
                if action.tool in (EDIT_FILE, FINISH):
                    self._remember_failure(
                        "kernel-rejected",
                        f"{action.tool} {action.args.get('path', '')}".strip(),
                        "; ".join(decision.issues[:2]) or feedback)
                if decision.cheat and self.memory is not None:
                    self.memory.record(
                        "cheat-caught", self._baseline_failure,
                        f"critic caught {decision.cheat}: "
                        + "; ".join(decision.issues[:2]))
                kernel.note_progress(False)
                continue
            self.last_rejected = None

            if action.tool == WITHHOLD:
                kernel.log("withhold", action.args.get("reason", ""))
                outcome = self._withheld(action.args.get("reason", ""),
                                         chosen=True)
                break
            if action.tool == FINISH:
                outcome = self._finish(action)
                if outcome is None:
                    kernel.note_progress(False)
                continue

            self._last_tests_obj = None
            result = self._execute(action)
            tests = self._last_tests_obj
            kernel.observe(action, result, tests)
            if (tests is not None and not tests.green
                    and self.workspace.changed_files()):
                self._remember_failure("tests-red", self._edit_desc(),
                                       tests.summary() + " "
                                       + self._fail_head(tests.output))
            progress = self._made_progress(action, result, tests)
            kernel.note_progress(progress)
            self._record(action, result)

        outcome.update({
            "steps": kernel.step,
            "tokens": kernel.tokens_used,
            "seconds": round(time.monotonic() - started, 1),
            "backend": self.backend.name,
            "kernel": kernel.snapshot(),
            "trace": kernel.trace,
        })
        if self.memory is not None:
            self.memory.end_run()
        return outcome

    @staticmethod
    def _fail_head(output: str) -> str:
        keep = [l for l in output.splitlines()
                if l.startswith(("E ", "FAILED")) or "Error" in l]
        return " | ".join(keep[:3])

    def _made_progress(self, action: Action, result: str, tests) -> bool:
        if action.tool == RUN_TESTS and tests is not None:
            return True  # fresh evidence either way; stuckness is about repeats
        if action.tool == READ_FILE:
            path = action.args.get("path", "")
            if path not in self._read_paths and not result.startswith("ERROR"):
                self._read_paths.add(path)
                return True
            return False
        if action.tool == EDIT_FILE:
            return True
        return action.tool in (LIST_FILES, SEARCH) and "no matches" not in result

    # -- outcomes ----------------------------------------------------------------------

    def _finish(self, action: Action) -> dict | None:
        kernel = self.kernel
        confidence = float(action.args.get("confidence", 0.0))
        summary = action.args.get("summary", "")
        if self.task == "fix":
            ok, message = kernel.verify_fix()
            if not ok:
                kernel.tally("finish_rejected")
                kernel.log("finish-reject", message)
                self._remember_failure("finish-rejected", self._edit_desc(),
                                       message)
                feedback = (f"finish rejected: {message}. Keep working, "
                            f"or withhold if the task is impossible.")
                self.last_rejected = feedback
                self._record(action, feedback)
                return None
            applied = self.workspace.commit_to_target()
            kernel.complete_goal("make the test suite pass")
            kernel.log("commit", f"applied {len(applied)} file(s): "
                                 + ", ".join(applied))
            if self.memory is not None:
                changed = ", ".join(sorted(self.workspace.changed_files()))
                self.memory.record("fixed", self._baseline_failure,
                                   f"fixed by editing {changed}: {summary}"[:240])
            return {"outcome": "committed", "summary": summary,
                    "confidence": confidence, "files_changed": applied,
                    "verification": message}
        # ask task
        answer = action.args.get("answer", "") or summary
        threshold = kernel.policy["min_confidence_to_answer"]
        verified = kernel.belief("verified")
        # Execution-backed answers earn a small confidence bonus; unverified
        # claims stand on the model's honesty alone.
        effective = min(1.0, confidence + (0.1 if verified > 0.6 else 0.0))
        if effective < threshold:
            kernel.log("withhold", f"confidence {confidence:.2f} < {threshold}")
            return {"outcome": "withheld", "answer": IDK,
                    "summary": f"confidence {confidence:.2f} is below the "
                               f"bar ({threshold}); refusing to guess",
                    "confidence": confidence}
        kernel.log("answer", f"confidence={confidence:.2f}")
        return {"outcome": "answered", "answer": answer, "summary": summary,
                "confidence": confidence,
                "verified_by_execution": verified > 0.5}

    def _withheld(self, reason: str, chosen: bool = False) -> dict:
        if self.memory is not None and self.task == "fix" and self._baseline_failure:
            kind = "withheld" if chosen else "dead-end"
            self.memory.record(kind, self._baseline_failure,
                               f"run ended without a fix: {reason}"[:240])
        result = {"outcome": "withheld", "reason": reason,
                  "withhold_chosen": chosen}
        if self.task == "ask":
            result["answer"] = IDK
        return result


# ---------------------------------------------------------------------------
# Entry points used by the CLI and the benchmark
# ---------------------------------------------------------------------------

def run_fix(target: str | Path, backend: Backend,
            memory: Memory | None = None, max_steps: int = 30,
            test_timeout: float = 60.0, policy: dict | None = None,
            verbose: bool = False, bucket: BoundBucket | None = None,
            inner_enabled: bool = True, announce=None,
            request: str = "", attempt_memory: bool = True) -> dict:
    bucket = bucket or BoundBucket(model=backend.name)
    with Workspace(Path(target), test_timeout=test_timeout, bucket=bucket,
                   inner_enabled=inner_enabled,
                   attempt_memory=attempt_memory) as ws:
        if announce:
            announce(bucket.describe())
            announce(ws.sandbox.describe())
        agent = Agent(backend, ws, task="fix", question=request,
                      memory=memory,
                      policy=policy, verbose=verbose, bucket=bucket)
        report = agent.run(max_steps=max_steps)
    report["target"] = str(target)
    return report


def run_ask(question: str, backend: Backend, memory: Memory | None = None,
            max_steps: int = 10, policy: dict | None = None,
            verbose: bool = False, bucket: BoundBucket | None = None,
            inner_enabled: bool = True, announce=None) -> dict:
    bucket = bucket or BoundBucket(model=backend.name)
    with Workspace(None, bucket=bucket, inner_enabled=inner_enabled) as ws:
        if announce:
            announce(bucket.describe())
            announce(ws.sandbox.describe())
        agent = Agent(backend, ws, task="ask", question=question,
                      memory=memory, policy=policy, verbose=verbose,
                      bucket=bucket)
        report = agent.run(max_steps=max_steps)
    report["question"] = question
    return report
