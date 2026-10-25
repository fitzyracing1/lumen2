"""Memory across runs — lessons only, retrieved by relevance.

v1's memory persisted belief priors and policy between runs, and a bad streak
made the kernel so cautious it stopped acting entirely (0/12 on the bench).
v2 memory deliberately stores only *information*, never *disposition*:

  * concrete lessons — what fixed which kind of failure, what was a dead end,
    what the critic caught — tagged with a keyword signature of the failure
  * retrieval by keyword overlap with the current failure, so a run only sees
    lessons about failures that look like its own
  * lessons are fed into the prompt as context; they never change thresholds,
    budgets, or beliefs, so memory cannot suppress action

Whether this helps is an empirical question; the benchmark's --memory flag
measures it instead of assuming it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

MEMORY_VERSION = 2
MAX_LESSONS = 200
MAX_FED = 5
MAX_DETAIL_CHARS = 240

_STOPWORDS = {"the", "a", "an", "in", "of", "to", "and", "is", "for", "on",
              "assert", "def", "test", "tests", "line", "error", "failed",
              "self", "none", "true", "false", "import", "from", "return"}


def signature(text: str) -> list[str]:
    """Keyword signature of a failure: exception names, identifiers, stems."""
    if not text:
        return []
    keywords: list[str] = []
    for m in re.finditer(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)*Error|AssertionError)\b", text):
        keywords.append(m.group(1))
    for m in re.finditer(r"\b([a-z_][a-z0-9_]{3,})\b", text.lower()):
        word = m.group(1)
        if word not in _STOPWORDS:
            keywords.append(word)
    seen: set[str] = set()
    out = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            out.append(k)
        if len(out) >= 24:
            break
    return out


class Memory:
    """A small local JSON store. Missing or corrupt files mean empty memory."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self._load()

    def _empty(self) -> dict:
        return {"version": MEMORY_VERSION, "lessons": [], "runs": 0}

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty()
        if (not isinstance(data, dict)
                or data.get("version") != MEMORY_VERSION
                or not isinstance(data.get("lessons"), list)):
            return self._empty()
        data.setdefault("runs", 0)
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def reset(self) -> None:
        self.data = self._empty()
        try:
            self.path.unlink()
        except OSError:
            pass

    # -- writing lessons -----------------------------------------------------------

    def record(self, kind: str, failure_text: str, detail: str) -> None:
        detail = " ".join(str(detail).split())[:MAX_DETAIL_CHARS]
        if not detail:
            return
        sig = signature(failure_text)
        for lesson in self.data["lessons"]:
            if lesson["kind"] == kind and lesson["detail"] == detail:
                lesson["count"] = int(lesson.get("count", 1)) + 1
                merged = list(dict.fromkeys(lesson.get("sig", []) + sig))[:24]
                lesson["sig"] = merged
                return
        self.data["lessons"].append(
            {"kind": kind, "detail": detail, "sig": sig, "count": 1})
        if len(self.data["lessons"]) > MAX_LESSONS:
            weakest = min(range(len(self.data["lessons"])),
                          key=lambda i: (self.data["lessons"][i].get("count", 1), -i))
            self.data["lessons"].pop(weakest)

    def end_run(self) -> None:
        self.data["runs"] = int(self.data.get("runs", 0)) + 1
        self.save()

    # -- retrieval -------------------------------------------------------------------

    def relevant(self, failure_text: str, limit: int = MAX_FED) -> list[str]:
        """Lessons whose signature overlaps the current failure, best first."""
        current = set(signature(failure_text))
        scored = []
        for lesson in self.data["lessons"]:
            overlap = len(current & set(lesson.get("sig", [])))
            if overlap > 0 or not current:
                scored.append((overlap, int(lesson.get("count", 1)), lesson))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [f"[{l['kind']} x{l.get('count', 1)}] {l['detail']}"
                for _, _, l in scored[:limit]]
