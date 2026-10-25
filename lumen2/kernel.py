"""The Lumen 2 kernel: the discipline wrapped around every agent step.

The model proposes; the kernel disposes. Concretely:

  * a persistent goal stack that outlives any single model call
  * named beliefs with confidence, updated only by tool evidence
  * a gate in front of every action — risky actions (edits, finishing) are
    attacked by the critic before they are allowed to run
  * verification before commit: a fix is only applied to the real target
    after a fresh green test run plus a final cheat scan
  * withholding as a first-class outcome when confidence is low or the
    budget is spent
  * budgeted policy revision: the kernel may loosen its own exploration
    policy when it is stuck, a bounded number of times, inside clamps
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from lumen2 import critic as critic_mod
from lumen2.actions import Action, EDIT_FILE, FINISH, RUN_TESTS, WITHHOLD
from lumen2.workspace import TestResult, Workspace


@dataclass
class Belief:
    claim: str
    confidence: float
    evidence: list[str] = field(default_factory=list)

    def update(self, support: float, note: str) -> None:
        # Explicit logit nudge, clamped away from 0/1 so evidence always matters.
        prior = min(max(self.confidence, 1e-4), 1 - 1e-4)
        logit = math.log(prior / (1 - prior)) + support
        self.confidence = 1 / (1 + math.exp(-logit))
        self.evidence.append(note)
        if len(self.evidence) > 30:
            del self.evidence[0]


@dataclass
class Goal:
    name: str
    done: bool = False


@dataclass
class GateDecision:
    allowed: bool
    issues: list[str] = field(default_factory=list)
    cheat: str | None = None

    def feedback(self) -> str:
        return ("action rejected by the kernel: " + "; ".join(self.issues)
                if self.issues else "action rejected by the kernel")


DEFAULT_POLICY = {
    "max_steps": 30,
    "max_tokens": 200_000,
    "max_seconds": 1200.0,
    "min_confidence_to_answer": 0.6,   # ask mode: below this, say "I don't know"
    "stuck_steps": 6,                  # steps without new evidence before revision
    "revision_budget": 2,
    "temperature": 0.2,
    "explore_hint": False,
}

# Revision may move policy only inside these clamps. It can never disable
# verification or the critic — those are not policy.
POLICY_CLAMPS = {
    "temperature": (0.1, 0.8),
    "min_confidence_to_answer": (0.5, 0.9),
    "stuck_steps": (4, 12),
}


class Kernel:
    """Holds goals, beliefs, policy, budgets, trace, and the gate."""

    def __init__(self, mission: str, workspace: Workspace,
                 policy: dict | None = None, model_critic=None,
                 lessons: list[str] | None = None):
        self.mission = mission
        self.workspace = workspace
        self.policy = dict(DEFAULT_POLICY)
        if policy:
            self.policy.update(policy)
        self.model_critic = model_critic  # callable(proposal: dict) -> dict | None
        self.lessons = lessons or []
        self.goals: list[Goal] = [Goal(mission)]
        self.beliefs: dict[str, Belief] = {}
        self.step = 0
        self.tokens_used = 0
        self.started = time.monotonic()
        self.trace: list[dict] = []
        self.stats: dict[str, int] = {}
        self.revisions_used = 0
        self._stuck = 0
        self.last_tests: TestResult | None = None
        self.log("boot", mission)

    # -- bookkeeping -----------------------------------------------------------

    def log(self, event: str, detail: str = "") -> None:
        self.trace.append({"step": self.step, "event": event,
                           "detail": str(detail)[:500]})

    def tally(self, key: str) -> None:
        self.stats[key] = self.stats.get(key, 0) + 1

    def believe(self, key: str, claim: str, confidence: float) -> None:
        self.beliefs[key] = Belief(claim, confidence, ["prior"])

    def belief(self, key: str) -> float:
        b = self.beliefs.get(key)
        return b.confidence if b else 0.5

    def nudge(self, key: str, support: float, note: str) -> None:
        if key in self.beliefs:
            self.beliefs[key].update(support, f"s{self.step}: {note}")

    # -- goal stack --------------------------------------------------------------

    def push_goal(self, name: str) -> None:
        if not any(g.name == name and not g.done for g in self.goals):
            self.goals.append(Goal(name))
            self.log("goal-push", name)

    def complete_goal(self, name: str) -> None:
        for g in self.goals:
            if g.name == name and not g.done:
                g.done = True
                self.log("goal-done", name)

    def open_goals(self) -> list[str]:
        return [g.name for g in self.goals if not g.done]

    # -- budgets -----------------------------------------------------------------

    @property
    def seconds_used(self) -> float:
        return time.monotonic() - self.started

    def budget_exceeded(self) -> str | None:
        if self.step >= self.policy["max_steps"]:
            return f"step budget spent ({self.step}/{self.policy['max_steps']})"
        if self.tokens_used >= self.policy["max_tokens"]:
            return f"token budget spent ({self.tokens_used})"
        if self.seconds_used >= self.policy["max_seconds"]:
            return f"time budget spent ({self.seconds_used:.0f}s)"
        return None

    def budgets_left(self) -> dict:
        return {
            "steps_left": max(0, self.policy["max_steps"] - self.step),
            "tokens_left": max(0, self.policy["max_tokens"] - self.tokens_used),
            "seconds_left": max(0.0, round(self.policy["max_seconds"]
                                           - self.seconds_used, 1)),
        }

    # -- the gate ---------------------------------------------------------------

    def gate(self, action: Action) -> GateDecision:
        """Attack a proposed action before it is allowed to run."""
        if action.tool == EDIT_FILE:
            verdict = critic_mod.attack_edit(
                self.workspace, action.args.get("path", ""),
                action.args.get("content", ""))
            if verdict.fatal:
                if verdict.cheat:
                    self.tally(f"cheat:{verdict.cheat}")
                self.tally("edits_rejected")
                self.log("gate-reject", f"{action.tool}: {verdict.describe()}")
                self.nudge("understood", -0.2, "edit rejected by critic")
                return GateDecision(False, verdict.issues, verdict.cheat)
            decision = self._model_attack(action)
            if decision is not None:
                return decision
        if action.tool == FINISH:
            verdict = critic_mod.attack_finish(self.workspace)
            if verdict.fatal:
                if verdict.cheat:
                    self.tally(f"cheat:{verdict.cheat}")
                self.tally("finish_rejected")
                self.log("gate-reject", f"finish: {verdict.describe()}")
                return GateDecision(False, verdict.issues, verdict.cheat)
            decision = self._model_attack(action)
            if decision is not None:
                return decision
        return GateDecision(True)

    def _model_attack(self, action: Action) -> GateDecision | None:
        """Adversarial model critique on top of the static checks. Advisory
        when unreachable: verification — not the model critic — is what stands
        between an edit and a commit, so a flaky critic cannot deadlock the
        loop, and a reachable critic can still kill a bad proposal."""
        if self.model_critic is None:
            return None
        critique = self.model_critic(action.public())
        if critique is None:
            self.tally("critic_unavailable")
            self.log("critic", f"{action.tool}: critique unavailable (advisory)")
            return None
        if critique.get("fatal"):
            issues = [str(i)[:300] for i in critique.get("issues", [])][:6] \
                or ["model critic found the proposal unsound"]
            self.tally("critic_fatal")
            self.log("gate-reject", f"{action.tool} (model critic): "
                                    + "; ".join(issues))
            return GateDecision(False, issues)
        if critique.get("issues"):
            self.log("critic", f"{action.tool}: "
                               + "; ".join(str(i)[:200]
                                           for i in critique["issues"][:3]))
        return None

    # -- evidence --------------------------------------------------------------

    def observe(self, action: Action, result_text: str,
                tests: TestResult | None = None) -> None:
        """Update beliefs from tool evidence. Evidence only — never vibes."""
        self.log("act", f"{action.tool} {str(action.args.get('path', ''))[:80]}")
        if action.tool == RUN_TESTS and tests is not None:
            previous = self.last_tests
            self.last_tests = tests
            if tests.green:
                self.nudge("verified", 2.0, f"tests green ({tests.summary()})")
                self.nudge("fixable", 1.0, "tests green")
                self.complete_goal("make the test suite pass")
            elif previous is not None and not tests.timed_out:
                if tests.failing < previous.failing and tests.passed >= previous.passed:
                    self.nudge("fixable", 0.6, f"improved to {tests.summary()}")
                    self.nudge("understood", 0.4, "edit moved tests the right way")
                    self._stuck = 0
                elif tests.failing > previous.failing or tests.passed < previous.passed:
                    self.nudge("fixable", -0.4, f"regressed to {tests.summary()}")
                    self.nudge("verified", -1.0, "tests regressed")
            if tests.timed_out:
                self.nudge("fixable", -0.5, "test run timed out")
        elif action.tool == EDIT_FILE:
            self.nudge("verified", -1.5, "workspace changed; verification is stale")
        elif action.tool in ("read_file", "search", "run_python", "list_files"):
            if not result_text.startswith("ERROR"):
                self.nudge("understood", 0.12, f"{action.tool} returned evidence")
            else:
                self.nudge("understood", -0.05, f"{action.tool} errored")
            if (action.tool == "run_python" and "fixable" not in self.beliefs
                    and "exit code: 0" in result_text):
                # ask mode: the answer was checked by actually running code
                self.nudge("verified", 1.2, "verification code ran cleanly")

    def note_progress(self, made_progress: bool) -> None:
        self._stuck = 0 if made_progress else self._stuck + 1

    # -- verification before commit ------------------------------------------------

    def verify_fix(self) -> tuple[bool, str]:
        """The only road to a commit: fresh test run + final cheat scan."""
        verdict = critic_mod.attack_finish(self.workspace)
        if verdict.fatal:
            if verdict.cheat:
                self.tally(f"cheat:{verdict.cheat}")
            return False, "final scan: " + "; ".join(verdict.issues)
        tests = self.workspace.run_tests()
        self.last_tests = tests
        if not tests.green:
            self.nudge("verified", -1.5, f"verification run: {tests.summary()}")
            return False, f"verification test run is not green ({tests.summary()})"
        if not self.workspace.changed_files():
            return False, "nothing changed: there is no fix to commit"
        self.nudge("verified", 2.5, "verification run green")
        return True, f"verified: {tests.summary()}"

    # -- budgeted policy revision ----------------------------------------------------

    def maybe_revise_policy(self) -> bool:
        """When stuck, loosen exploration — inside clamps, inside the budget."""
        if self._stuck < self.policy["stuck_steps"]:
            return False
        if self.revisions_used >= self.policy["revision_budget"]:
            return False
        proposal = dict(self.policy)
        proposal["temperature"] = min(POLICY_CLAMPS["temperature"][1],
                                      self.policy["temperature"] + 0.25)
        proposal["explore_hint"] = True
        # The critic of the revision: it must stay inside the clamps and must
        # not touch verification or budgets.
        for key, (lo, hi) in POLICY_CLAMPS.items():
            if not (lo <= proposal[key] <= hi):
                return False
        for frozen in ("max_steps", "max_tokens", "max_seconds"):
            if proposal[frozen] != self.policy[frozen]:
                return False
        self.policy = proposal
        self.revisions_used += 1
        self._stuck = 0
        self.tally("policy_revisions")
        self.log("revise", f"temperature={proposal['temperature']:.2f} "
                           f"explore_hint=True "
                           f"({self.revisions_used}/{self.policy['revision_budget']})")
        return True

    # -- reporting -----------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "step": self.step,
            "tokens_used": self.tokens_used,
            "seconds_used": round(self.seconds_used, 1),
            "goals": [{"name": g.name, "done": g.done} for g in self.goals],
            "beliefs": {k: {"claim": b.claim,
                            "confidence": round(b.confidence, 3)}
                        for k, b in self.beliefs.items()},
            "policy": dict(self.policy),
            "stats": dict(self.stats),
            "revisions_used": self.revisions_used,
        }
