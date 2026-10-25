"""The deterministic offline stub backend.

No network, no randomness. For fix tasks it behaves like a disciplined but
unimaginative engineer: run the tests, read the source, try one small
bug-class rewrite at a time (operator swaps, off-by-one range ends, slice
starts), keep what the tests say is better, revert what is not, finish when
green, withhold when the catalog is exhausted. Tests and CI run on it.

The point is not that the stub is smart — it is that the same kernel
discipline wraps it and every real model identically.
"""

from __future__ import annotations

import ast
import re

from lumen2.actions import (Action, EDIT_FILE, FINISH, LIST_FILES, READ_FILE,
                            RUN_PYTHON, RUN_TESTS, WITHHOLD)
from lumen2.backends import Backend
from lumen2.workspace import is_test_path

_SWAPS = [("+", "-"), ("-", "+"), ("*", "+"), ("<=", "<"), ("<", "<="),
          (">=", ">"), (">", ">=")]
MAX_READS = 8
MAX_MUTATIONS = 60


def _parse_counts(result: str) -> tuple[int, int, bool]:
    """(passed, failing, green) from a run_tests result string."""
    def count(pattern: str) -> int:
        m = re.search(pattern, result)
        return int(m.group(1)) if m else 0
    passed = count(r"passed=(\d+)")
    failing = count(r"failed=(\d+)") + count(r"errors=(\d+)")
    green = "ALL TESTS PASS" in result
    return passed, failing, green


def _replace_nth(text: str, old: str, new: str, n: int) -> str:
    start = 0
    for _ in range(n):
        i = text.find(old, start)
        if i == -1:
            return text
        start = i + len(old)
    i = text.find(old, start)
    if i == -1:
        return text
    return text[:i] + new + text[i + len(old):]


def _mutations(path: str, content: str) -> list[tuple[str, str]]:
    """[(description, new_content)] of parseable single rewrites."""
    out: list[tuple[str, str]] = []

    def add(desc: str, mutated: str) -> None:
        if mutated == content or len(out) >= MAX_MUTATIONS:
            return
        try:
            ast.parse(mutated)
        except SyntaxError:
            return
        out.append((f"{path}:{desc}", mutated))

    for old, new in _SWAPS:
        for occ in range(min(content.count(old), 3)):
            add(f"{old}->{new}@{occ}", _replace_nth(content, old, new, occ))
    # Off-by-one at a range end: range(a, n) -> range(a, n + 1)
    for m in re.finditer(r"range\(([^()]+?),\s*([A-Za-z_]\w*)\)", content):
        mutated = (content[:m.start()]
                   + f"range({m.group(1)}, {m.group(2)} + 1)"
                   + content[m.end():])
        add(f"range-end+1@{m.start()}", mutated)
    # Slice that drops the first element: [1: -> [:
    for occ in range(min(content.count("[1:"), 3)):
        add(f"slice-start@{occ}", _replace_nth(content, "[1:", "[:", occ))
    # Numeric literal constants: try the nearby literal in the visible tests?
    # No — the stub does not peek at tests. Honest catalog only.
    return out


class StubBackend(Backend):
    name = "stub"

    def __init__(self):
        self.start_task()

    def start_task(self) -> None:
        self._phase = "baseline"
        self._reads: list[str] = []
        self._files: dict[str, str] = {}
        self._queue: list[tuple[str, str, str]] = []  # (desc, path, content)
        self._tried: set[str] = set()
        self._pending: tuple[str, str, str] | None = None  # desc, path, prior
        self._best = (0, 10 ** 9)  # (passed, failing)

    def critique(self, proposal: dict, context: dict) -> dict | None:
        return {"fatal": False, "issues": []}

    # -- the state machine -------------------------------------------------------

    def next_action(self, context: dict, tools: tuple[str, ...],
                    temperature: float) -> Action | None:
        if context.get("task") == "ask":
            return self._ask_action(context)
        history = context.get("history", [])
        last = history[-1] if history else None
        rejected = context.get("last_rejected")

        if rejected and self._pending is not None:
            # The kernel killed our edit before it ran; move on.
            self._pending = None
            return self._next_mutation()

        if self._phase == "baseline":
            self._phase = "post-baseline"
            return Action(RUN_TESTS, why="establish the failing baseline")

        if last and last["action"]["tool"] == RUN_TESTS:
            passed, failing, green = _parse_counts(last["result"])
            if green:
                return Action(FINISH, {
                    "summary": "tests pass after a minimal source rewrite",
                    "confidence": 0.9}, why="verified green")
            if self._pending is not None:
                desc, path, prior = self._pending
                self._pending = None
                if failing < self._best[1] and passed >= self._best[0]:
                    # Keep the improvement and rebuild candidates on top of it.
                    self._best = (passed, failing)
                    self._rebuild_queue()
                    return self._next_mutation()
                # Revert, then try the next candidate.
                self._queue.insert(0, ("revert", path, prior))
                return self._next_mutation()
            self._best = (passed, failing)
            if self._phase == "post-baseline":
                self._phase = "explore"
                return Action(LIST_FILES, why="see what is in the workspace")

        if last and last["action"]["tool"] == LIST_FILES:
            for line in last["result"].splitlines():
                name = line.split(" (")[0].strip()
                if (name.endswith(".py") and not is_test_path(name)
                        and len(self._reads) < MAX_READS):
                    self._reads.append(name)
            if not self._reads:
                return Action(WITHHOLD, {"reason": "no editable source files"},
                              why="nothing to fix")
            return Action(READ_FILE, {"path": self._reads[0]},
                          why="read the source under test")

        if last and last["action"]["tool"] == READ_FILE:
            path = last["action"]["args"].get("path", "")
            if not last["result"].startswith("ERROR"):
                self._files[path] = last["result"]
            remaining = [p for p in self._reads if p not in self._files]
            if remaining:
                return Action(READ_FILE, {"path": remaining[0]},
                              why="read the source under test")
            self._rebuild_queue()
            return self._next_mutation()

        if last and last["action"]["tool"] == EDIT_FILE:
            return Action(RUN_TESTS, why="measure the edit against the tests")

        # Anything unexpected: withhold rather than loop.
        return Action(WITHHOLD, {"reason": "stub reached an unknown state"},
                      why="deterministic catalog cannot continue")

    def _rebuild_queue(self) -> None:
        self._queue = []
        for path in sorted(self._files):
            for desc, mutated in _mutations(path, self._files[path]):
                if desc not in self._tried:
                    self._queue.append((desc, path, mutated))

    def _next_mutation(self) -> Action:
        while self._queue:
            desc, path, content = self._queue.pop(0)
            if desc == "revert":
                self._files[path] = content
                return Action(EDIT_FILE, {"path": path, "content": content},
                              why="revert: the tests said no")
            self._tried.add(desc)
            if content == self._files.get(path):
                continue
            prior = self._files.get(path, "")
            self._files[path] = content
            self._pending = (desc, path, prior)
            return Action(EDIT_FILE, {"path": path, "content": content},
                          why=f"try candidate rewrite {desc}")
        return Action(WITHHOLD,
                      {"reason": "every candidate rewrite failed the tests "
                                 "or the catalog is exhausted"},
                      why="refusing to guess")

    # -- ask mode ----------------------------------------------------------------

    def _ask_action(self, context: dict) -> Action:
        history = context.get("history", [])
        question = str(context.get("question", ""))
        if history and history[-1]["action"]["tool"] == RUN_PYTHON:
            result = history[-1]["result"]
            m = re.search(r"stdout:\n(.+)", result)
            if m:
                value = m.group(1).strip()
                return Action(FINISH, {
                    "summary": f"computed it by running Python: {value}",
                    "answer": value, "confidence": 0.93},
                    why="verified by execution")
            return Action(FINISH, {"summary": "could not verify",
                                   "answer": "I don't know",
                                   "confidence": 0.1}, why="verification failed")
        expr = re.search(r"[-+*/().\d\s%]{3,}", question)
        if expr and re.search(r"\d", expr.group(0)):
            code = f"print({expr.group(0).strip()})"
            return Action(RUN_PYTHON, {"code": code},
                          why="verify by computing instead of recalling")
        return Action(FINISH, {"summary": "no way to verify this offline",
                               "answer": "I don't know",
                               "confidence": 0.1},
                      why="the stub cannot verify free-form claims")
