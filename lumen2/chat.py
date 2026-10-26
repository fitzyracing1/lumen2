"""Interactive chat on top of the kernel.

Every turn is a normal kernel run inside a BoundBucket:

- a question goes through ``run_ask`` (answers checked by running code where
  possible, low confidence becomes "I don't know");
- a request for a code change (``/fix`` or a message that asks to fix/change
  code) goes through ``run_fix`` on the chat's project directory — scratch
  copy, nested sandbox, critic, fresh test verification, and only a verified
  diff reaches the project.

The whole session shares one budget: each turn's bucket is the configured
bucket tightened to what the session has left (steps, model calls, tokens,
cost, wall clock). When the session budget is spent, chat refuses further
turns instead of loosening anything.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from lumen2.bucket import BoundBucket

CHANGE_WORDS = re.compile(
    r"^\s*(please\s+)?(fix|change|edit|modify|refactor|implement|patch|"
    r"update|make the tests? pass|repair|correct)\b", re.I)
HELP = ("commands: /fix [request]  run the fixer on the project · "
        "/ask <question> · /budget · /help · exit")


def wants_change(message: str) -> bool:
    return bool(CHANGE_WORDS.search(message))


class ChatSession:
    def __init__(self, backend, project: str | Path | None = None,
                 bucket_factory=None, memory=None, max_steps_ask: int = 10,
                 max_steps_fix: int = 30, inner_enabled: bool = True,
                 verbose: bool = False, out=print, attacker=None):
        self.attacker = attacker
        self.backend = backend
        self.project = Path(project).resolve() if project else None
        self.bucket_factory = bucket_factory or (
            lambda: BoundBucket(model=backend.name))
        self.memory = memory
        self.max_steps_ask = max_steps_ask
        self.max_steps_fix = max_steps_fix
        self.inner_enabled = inner_enabled
        self.verbose = verbose
        self.out = out
        self.limits = self.bucket_factory().limits   # the session-wide caps
        self.spent = {"steps": 0, "model_calls": 0, "tokens": 0,
                      "cost_usd": 0.0}
        self.started = time.monotonic()
        self.turns: list[dict] = []
        self.breaches = 0

    # -- budget ------------------------------------------------------------

    def remaining(self) -> dict:
        lim = self.limits
        return {
            "max_steps": lim.max_steps - self.spent["steps"],
            "max_model_calls": lim.max_model_calls - self.spent["model_calls"],
            "max_tokens": lim.max_tokens - self.spent["tokens"],
            "max_cost_usd": round(lim.max_cost_usd - self.spent["cost_usd"], 6),
            "max_seconds": lim.max_seconds - (time.monotonic() - self.started),
        }

    def exhausted(self) -> str | None:
        for key, left in self.remaining().items():
            if left <= 0:
                return key
        return None

    def turn_bucket(self) -> BoundBucket:
        bucket = self.bucket_factory()
        refused = bucket.tighten(**self.remaining())
        assert not refused   # remaining <= configured, so this only tightens
        return bucket

    def _account(self, bucket: BoundBucket) -> None:
        u = bucket.usage()
        self.spent["steps"] += u["steps"]
        self.spent["model_calls"] += u["model_calls"]
        self.spent["tokens"] += u["tokens"]
        self.spent["cost_usd"] += u["cost_usd"]
        self.breaches += u["breaches"]

    # -- one turn ----------------------------------------------------------

    def handle(self, message: str) -> dict | None:
        """Process one message. Returns the kernel report (None for meta
        commands and refusals)."""
        from lumen2.agent import run_ask, run_fix
        from lumen2.cli import _ask_banner, _fix_banner
        message = message.strip()
        if message in ("/help", "help"):
            self.out(HELP)
            return None
        if message == "/budget":
            self.out(f"spent: {self.spent} · left: "
                     f"{ {k: round(v, 4) for k, v in self.remaining().items()} }"
                     f" · breaches: {self.breaches}")
            return None
        spent = self.exhausted()
        if spent:
            self.out(f"lumen2> session budget spent ({spent}); not starting "
                     "another turn. Start a new chat for a fresh budget.")
            return {"outcome": "refused", "reason": f"session {spent} spent"}

        if message.startswith("/ask "):
            kind, text = "ask", message[5:].strip()
        elif message.startswith("/fix"):
            kind, text = "fix", message[4:].strip()
        else:
            kind = "fix" if wants_change(message) else "ask"
            text = message

        bucket = self.turn_bucket()
        if kind == "fix":
            if self.project is None:
                self.out("lumen2> no project directory: start chat with "
                         "--project DIR to let me change code.")
                return {"outcome": "refused", "reason": "no project"}
            self.out(f"lumen2> treating this as a code change in "
                     f"{self.project} (kernel + bucket + sandbox; only a "
                     "verified diff is written)")
            report = run_fix(self.project, self.backend, memory=self.memory,
                             max_steps=self.max_steps_fix,
                             verbose=self.verbose, bucket=bucket,
                             inner_enabled=self.inner_enabled, request=text,
                             attacker=self.attacker)
            banner = _fix_banner(report)
        else:
            report = run_ask(text, self.backend, memory=self.memory,
                             max_steps=self.max_steps_ask,
                             verbose=self.verbose, bucket=bucket,
                             inner_enabled=self.inner_enabled)
            banner = _ask_banner(report)
        self._account(bucket)
        report["turn_kind"] = kind
        report["bucket_usage"] = bucket.usage()
        self.turns.append(report)
        self.out(banner)
        return report


def chat_loop(session: ChatSession, read=None) -> int:
    read = read or input
    session.out("lumen2 chat — backend " + session.backend.name
                + (f", project {session.project}" if session.project else
                   ", no project (questions only)")
                + ". /help for commands; blank line or 'exit' quits.")
    session.out(session.bucket_factory().describe() + "  [session-wide]")
    while True:
        try:
            message = read("you> ")
        except (EOFError, KeyboardInterrupt):
            session.out("")
            return 0
        if not message.strip() or message.strip().lower() in ("exit", "quit"):
            return 0
        session.handle(message)
