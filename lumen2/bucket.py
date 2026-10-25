"""BoundBucket: the hard outer limits every action must pass through.

The thesis calls Lumen 2 "a bound bucket using AI to drive an attack bot
that derives its info from an LLM". This module is the bound bucket. It is
deliberately dumb and declarative: a set of hard limits, checked before
every model call, every tool call, every executed command, every edit, and
every export back to the real target. Nothing here is negotiable by the
model, the critic, or the kernel's own policy revision:

* limits are declared up front (defaults, optionally overridden by
  ``--bucket file.toml|file.json``) and printed at start;
* a limit that is hit blocks the action and records a ``bucket-breach``
  event (with the layer ``bucket``) in the kernel trace;
* limits can be *tightened* mid-run but never loosened.

Hard-budget breaches (steps, model calls, tokens, cost, wall-clock) end the
run with a withhold. Action-level breaches (a path outside the scratch
workspace, an edit to a read-only test, a disallowed tool, an oversized
edit, a finish below the confidence floor) block that one action; after
``max_breaches`` of them the agent withholds.
"""

from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path, PurePosixPath

ALL_TOOLS_DEFAULT = ("read_file", "list_files", "search", "run_tests",
                     "run_python", "edit_file", "finish", "withhold")

# USD per 1M tokens (input, output). Unknown paid models use the most
# expensive row so the cost cap errs on the side of stopping early.
PRICES = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    # xAI (docs.x.ai/developers/pricing, Oct 2026, short-context rates)
    "grok-4.7": (2.00, 6.00),
    "grok-4.6": (2.00, 6.00),
    "grok-4.5": (2.00, 6.00),
    "grok-4.3": (1.25, 2.50),
    "grok-4.20-0309-non-reasoning": (1.25, 2.50),
    "grok-4.20-0309-reasoning": (1.25, 2.50),
}
UNKNOWN_PAID_PRICE = (5.00, 15.00)

# Which direction is "tighter" for each numeric limit.
_LOWER_IS_TIGHTER = ("max_steps", "max_model_calls", "max_tokens",
                     "max_seconds", "command_timeout", "max_cost_usd",
                     "max_policy_revisions", "max_edit_chars", "max_breaches",
                     "mem_mb", "cpu_seconds", "file_size_mb", "max_procs")
_HIGHER_IS_TIGHTER = ("confidence_floor",)


class BucketBreach(Exception):
    """Raised by ``BoundBucket.require``; carries the breach record."""

    def __init__(self, record: dict):
        super().__init__(record["detail"])
        self.record = record


@dataclass
class Limits:
    max_steps: int = 60
    max_model_calls: int = 150
    max_tokens: int = 400_000
    max_seconds: float = 1800.0
    command_timeout: float = 120.0
    max_cost_usd: float = 1.00
    network: bool = False                 # network for sandboxed code
    max_policy_revisions: int = 2
    max_edit_chars: int = 60_000
    confidence_floor: float = 0.3         # below this, only withhold
    max_breaches: int = 3                 # action-level breaches before withhold
    allowed_tools: tuple = ALL_TOOLS_DEFAULT
    read_only: tuple = ("tests/*", "test/*", "test_*", "*/test_*",
                        "*_test.py", "conftest.py", "*/conftest.py")
    # Inner-sandbox resource limits (rlimits on POSIX).
    mem_mb: int = 2048
    cpu_seconds: int = 120
    file_size_mb: int = 64
    max_procs: int = 256
    container: str = "off"                # off | auto | docker | podman
    container_image: str = "python:3.12-slim"


class BoundBucket:
    """Hard limits + accounting. One instance per run."""

    def __init__(self, limits: Limits | None = None, model: str = "stub"):
        self.limits = limits or Limits()
        self.model = model
        self.allowed_roots: list[Path] = []
        self.started = time.monotonic()
        self.steps = 0
        self.model_calls = 0
        self.tokens = 0
        self.cost_usd = 0.0
        self.revisions = 0
        self.attempt_memory_tokens = 0   # outer attempt summaries put in prompts
        self.breaches: list[dict] = []
        self.on_breach = None   # callable(record) -> None (kernel trace hook)
        self.on_event = None    # callable(event, detail)

    # -- construction ----------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path | None, model: str = "stub",
                  **overrides) -> "BoundBucket":
        limits = Limits()
        if path:
            data = load_limits_file(Path(path))
            known = {f.name for f in fields(Limits)}
            unknown = set(data) - known
            if unknown:
                raise ValueError(f"unknown bucket limits: {sorted(unknown)}")
            for key, value in data.items():
                if isinstance(getattr(limits, key), tuple):
                    value = tuple(value)
                setattr(limits, key, value)
        for key, value in overrides.items():
            if value is not None:
                setattr(limits, key, value)
        return cls(limits, model=model)

    def confine_to(self, root: Path) -> None:
        self.allowed_roots = [Path(root).resolve()]

    def describe(self) -> str:
        lim = asdict(self.limits)
        lim["allowed_tools"] = ",".join(lim["allowed_tools"])
        lim["read_only"] = ",".join(lim["read_only"])
        roots = ", ".join(str(r) for r in self.allowed_roots) or "(scratch workspace)"
        body = " ".join(f"{k}={v}" for k, v in lim.items())
        return f"[bucket] limits: {body} allowed_paths={roots}"

    def usage(self) -> dict:
        return {"steps": self.steps, "model_calls": self.model_calls,
                "tokens": self.tokens, "cost_usd": round(self.cost_usd, 5),
                "seconds": round(self.seconds_used, 1),
                "revisions": self.revisions,
                "attempt_memory_tokens": self.attempt_memory_tokens, "breaches": len(self.breaches)}

    # -- tighten, never loosen ---------------------------------------------------

    def tighten(self, **changes) -> list[str]:
        """Apply only the changes that make the bucket stricter. Returns the
        list of refused keys (attempts to loosen), each recorded as a breach."""
        refused = []
        for key, value in changes.items():
            current = getattr(self.limits, key)
            ok = False
            if key in _LOWER_IS_TIGHTER:
                ok = value <= current
            elif key in _HIGHER_IS_TIGHTER:
                ok = value >= current
            elif key == "network":
                ok = (not value) or current
            elif key in ("allowed_tools",):
                ok = set(value) <= set(current)
            elif key == "read_only":
                ok = set(current) <= set(value)
            if ok:
                setattr(self.limits, key, tuple(value)
                        if isinstance(current, tuple) else value)
                self._event("bucket-tighten", f"{key}: {current} -> {value}")
            else:
                refused.append(key)
                self.breach("bucket", "loosen",
                            f"refused to loosen {key} from {current} to {value}")
        return refused

    # -- recording -------------------------------------------------------------------

    def _event(self, event: str, detail: str) -> None:
        if self.on_event:
            self.on_event(event, detail)

    def breach(self, layer: str, kind: str, detail: str) -> dict:
        record = {"layer": layer, "kind": kind, "detail": detail[:300],
                  "at_step": self.steps}
        self.breaches.append(record)
        if self.on_breach:
            self.on_breach(record)
        return record

    @property
    def seconds_used(self) -> float:
        return time.monotonic() - self.started

    @property
    def action_breaches(self) -> int:
        return sum(1 for b in self.breaches if b["kind"] not in HARD_KINDS)

    # -- hard budgets -----------------------------------------------------------------

    def check_budgets(self) -> dict | None:
        """Before every step. A returned breach means: withhold now."""
        lim = self.limits
        if self.steps >= lim.max_steps:
            return self.breach("bucket", "steps",
                               f"max_steps {lim.max_steps} reached")
        if self.seconds_used >= lim.max_seconds:
            return self.breach("bucket", "wall-clock",
                               f"max_seconds {lim.max_seconds} reached")
        if self.model_calls >= lim.max_model_calls:
            return self.breach("bucket", "model-calls",
                               f"max_model_calls {lim.max_model_calls} reached")
        if self.tokens >= lim.max_tokens:
            return self.breach("bucket", "tokens",
                               f"max_tokens {lim.max_tokens} reached ({self.tokens})")
        if self.cost_usd >= lim.max_cost_usd:
            return self.breach("bucket", "cost",
                               f"max_cost_usd {lim.max_cost_usd} reached "
                               f"(${self.cost_usd:.4f})")
        if self.action_breaches >= lim.max_breaches:
            return self.breach("bucket", "too-many-breaches",
                               f"{self.action_breaches} action breaches")
        return None

    def take_step(self) -> dict | None:
        breach = self.check_budgets()
        if breach is None:
            self.steps += 1
        return breach

    def authorize_model_call(self) -> dict | None:
        lim = self.limits
        for kind, used, cap in (("model-calls", self.model_calls, lim.max_model_calls),
                                ("tokens", self.tokens + self.attempt_memory_tokens,
                                 lim.max_tokens)):
            if used >= cap:
                return self.breach("bucket", kind, f"{kind} cap {cap} reached")
        if self.cost_usd >= lim.max_cost_usd:
            return self.breach("bucket", "cost",
                               f"cost cap ${lim.max_cost_usd} reached")
        self.model_calls += 1
        return None

    def charge_context(self, tokens: int) -> None:
        """Account for outer-layer context (attempt memory) in prompts.

        Counted separately so the size is visible, and reserved against the
        token cap in ``authorize_model_call`` (the provider's own usage already
        bills the dollars, so this is not double-charged as cost)."""
        self.attempt_memory_tokens += max(0, int(tokens))

    def charge(self, prompt_tokens: int, completion_tokens: int,
               paid: bool) -> None:
        self.tokens += max(0, prompt_tokens) + max(0, completion_tokens)
        if paid:
            name = self.model.split(":", 1)[-1]
            price = PRICES.get(name, UNKNOWN_PAID_PRICE)
            self.cost_usd += (prompt_tokens * price[0]
                              + completion_tokens * price[1]) / 1e6

    def authorize_revision(self) -> dict | None:
        if self.revisions >= self.limits.max_policy_revisions:
            return self.breach("bucket", "policy-revisions",
                               f"max_policy_revisions "
                               f"{self.limits.max_policy_revisions} reached")
        self.revisions += 1
        return None

    # -- action-level checks ------------------------------------------------------------

    def is_read_only(self, rel: str) -> bool:
        rel = PurePosixPath(rel.replace("\\", "/")).as_posix()
        name = PurePosixPath(rel).name
        return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat)
                   for pat in self.limits.read_only)

    def path_allowed(self, path: str | Path) -> bool:
        if not self.allowed_roots:
            return False
        try:
            full = Path(path).resolve()
        except (OSError, ValueError):
            return False
        return any(full == r or full.is_relative_to(r) for r in self.allowed_roots)

    def check_path(self, rel: str, write: bool) -> dict | None:
        if not isinstance(rel, str) or not rel.strip():
            return self.breach("bucket", "path", "empty path")
        raw = rel.strip()
        root = self.allowed_roots[0] if self.allowed_roots else None
        candidate = Path(raw) if Path(raw).is_absolute() else (
            (root / raw) if root else Path(raw))
        if ".." in PurePosixPath(raw.replace("\\", "/")).parts \
                or raw.startswith(("/", "\\")) or (len(raw) > 1 and raw[1] == ":") \
                or not self.path_allowed(candidate):
            return self.breach("bucket", "path-outside",
                               f"{'write' if write else 'read'} outside the "
                               f"scratch workspace: {raw!r}")
        if write and self.is_read_only(raw):
            return self.breach("bucket", "read-only",
                               f"write to read-only path {raw!r}")
        return None

    def check_action(self, tool: str, args: dict) -> dict | None:
        """Every tool call. Returns a breach record, or None if allowed."""
        lim = self.limits
        if tool not in lim.allowed_tools:
            return self.breach("bucket", "tool", f"tool {tool!r} is not allowed")
        if tool in ("read_file", "edit_file"):
            hit = self.check_path(args.get("path", ""), write=tool == "edit_file")
            if hit:
                return hit
        if tool == "edit_file":
            size = len(str(args.get("content", "")))
            if size > lim.max_edit_chars:
                return self.breach("bucket", "edit-size",
                                   f"edit of {size} chars > max_edit_chars "
                                   f"{lim.max_edit_chars}")
        if tool == "finish":
            try:
                conf = float(args.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            if conf < lim.confidence_floor:
                return self.breach("bucket", "confidence-floor",
                                   f"finish at confidence {conf:.2f} < floor "
                                   f"{lim.confidence_floor}; only withhold is allowed")
        return None

    def check_export(self, rel: str, content: str) -> dict | None:
        """The outer sandbox asks before each file leaves for the real target."""
        hit = self.check_path(rel, write=True)
        if hit:
            return hit
        if len(content) > self.limits.max_edit_chars:
            return self.breach("bucket", "edit-size",
                               f"export of {rel} is {len(content)} chars")
        return None


HARD_KINDS = ("steps", "wall-clock", "model-calls", "tokens", "cost",
              "too-many-breaches", "policy-revisions", "loosen")


def load_limits_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".toml":
        import tomllib
        data = tomllib.loads(text)
        return data.get("bucket", data)
    data = json.loads(text)
    return data.get("bucket", data) if isinstance(data, dict) else {}
