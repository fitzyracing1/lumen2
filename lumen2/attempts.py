"""Outer-layer attempt memory (idea: Joshua Almeida).

The inner sandbox is wiped between attempts. The OUTER layer keeps a tiny,
in-process log of failed attempts (rejected finishes, red test runs after an
edit, critic-blocked edits) and hands a concise summary of the most recent
failure(s) to the next proposal / critic round: a "second attack" informed by
the first.

Safety properties:
- It lives only in this Python object, owned by the OuterSandbox. It is never
  written into the scratch workspace, the inner run copies, or the env, so the
  untrusted code under test can never read it.
- Bounded: only the last ``max_entries`` failures, each and in total
  size-capped; the rendered size is charged against the BoundBucket token
  budget every time it is put into a prompt. Lumen 1's unbounded memory made
  the model over-cautious; this is deliberately short-lived and small.
"""

from __future__ import annotations

MAX_ENTRIES = 2
MAX_ENTRY_CHARS = 280
MAX_TOTAL_CHARS = 600


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def approx_tokens(text: str) -> int:
    return (len(text) + 3) // 4


class AttemptMemory:
    def __init__(self, enabled: bool = True, max_entries: int = MAX_ENTRIES,
                 max_entry_chars: int = MAX_ENTRY_CHARS,
                 max_total_chars: int = MAX_TOTAL_CHARS):
        self.enabled = enabled
        self.max_entries = max_entries
        self.max_entry_chars = max_entry_chars
        self.max_total_chars = max_total_chars
        self.entries: list[dict] = []
        self.recorded = 0

    def record(self, kind: str, what: str, why: str) -> None:
        if not self.enabled:
            return
        self.recorded += 1
        entry = {"n": self.recorded, "kind": kind,
                 "text": _clip(f"{what} -> {why}", self.max_entry_chars)}
        self.entries.append(entry)
        del self.entries[:-self.max_entries]

    def summary(self) -> str:
        if not self.enabled or not self.entries:
            return ""
        lines = [f"attempt {e['n']} failed ({e['kind']}): {e['text']}"
                 for e in reversed(self.entries)]   # newest first
        out = ""
        for line in lines:
            cand = (out + "\n" + line) if out else line
            if len(cand) > self.max_total_chars:
                if not out:
                    out = _clip(line, self.max_total_chars)
                break
            out = cand
        return out
