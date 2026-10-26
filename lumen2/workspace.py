"""A scratch workspace the agent is confined to.

The agent never touches the real target directory. It works in a throwaway
copy; only a verified finish copies changes back. Every path is validated to
stay inside the workspace, test files are read-only, and every subprocess has
a hard timeout. Everything here is pathlib + UTF-8 so it behaves the same on
Windows and Linux.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IGNORED_DIRS = ("__pycache__", ".pytest_cache", ".git", ".venv",
                "node_modules", ".mypy_cache", ".lumen2-tmp")
MAX_FILE_CHARS = 40_000
MAX_OUTPUT_CHARS = 6_000
MAX_SEARCH_HITS = 50


def safe_join(base: Path, rel: str) -> Path | None:
    """Resolve `rel` inside `base`. None for anything that escapes."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    rel = rel.strip()
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return None
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if not parts or ".." in parts or any(p in (".",) for p in parts):
        return None
    base_resolved = base.resolve()
    full = (base_resolved / Path(*parts)).resolve()
    if full == base_resolved or not full.is_relative_to(base_resolved):
        return None
    return full


def is_test_path(rel: str) -> bool:
    """True for anything that belongs to the test suite. Read-only."""
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    for part in parts:
        if part in ("tests", "test", "conftest.py"):
            return True
        if part.startswith("test_") or part.endswith("_test.py"):
            return True
    return False


@dataclass
class TestResult:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    exit_code: int = -1
    output: str = ""
    timed_out: bool = False

    @property
    def failing(self) -> int:
        return self.failed + self.errors

    @property
    def total(self) -> int:
        return self.passed + self.failing

    @property
    def green(self) -> bool:
        return (not self.timed_out and self.exit_code == 0
                and self.passed > 0 and self.failing == 0)

    def summary(self) -> str:
        if self.timed_out:
            return "TIMED OUT"
        return f"passed={self.passed} failed={self.failed} errors={self.errors}"


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n... [truncated] ...\n" + text[-limit // 2:]


def parse_pytest(output: str, exit_code: int, timed_out: bool = False) -> TestResult:
    def count(pattern: str) -> int:
        m = re.search(pattern, output)
        return int(m.group(1)) if m else 0
    return TestResult(passed=count(r"(\d+) passed"),
                      failed=count(r"(\d+) failed"),
                      errors=count(r"(\d+) errors?"),
                      exit_code=exit_code, output=_truncate(output),
                      timed_out=timed_out)


PYTEST_ARGV = ["-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]


def run_pytest(directory: Path, timeout: float) -> TestResult:
    """Run pytest in `directory` with a hard timeout. Never raises."""
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short",
           "-p", "no:cacheprovider"]
    env = _utf8_env()
    try:
        proc = subprocess.run(cmd, cwd=str(directory), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        partial = f"{_as_text(exc.stdout)}\n{_as_text(exc.stderr)}"
        return TestResult(output=_truncate(partial.strip()), timed_out=True)
    except OSError as exc:
        return TestResult(output=f"could not run pytest: {exc}")
    output = f"{proc.stdout}\n{proc.stderr}".strip()

    def count(pattern: str) -> int:
        m = re.search(pattern, output)
        return int(m.group(1)) if m else 0

    return TestResult(
        passed=count(r"(\d+) passed"),
        failed=count(r"(\d+) failed"),
        errors=count(r"(\d+) errors?"),
        exit_code=proc.returncode,
        output=_truncate(output),
    )


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _utf8_env() -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return env


class Workspace:
    """Scratch copy of a target directory, with sandboxed tool operations."""

    def __init__(self, target: Path | None, test_timeout: float = 60.0,
                 python_timeout: float = 20.0, bucket=None,
                 inner_enabled: bool = True, attempt_memory: bool = True):
        self._tmp = Path(tempfile.mkdtemp(prefix="lumen2-ws-"))
        self.root = self._tmp / "work"
        self.target = Path(target).resolve() if target is not None else None
        if self.target is not None:
            if not self.target.is_dir():
                raise ValueError(f"target is not a directory: {self.target}")
            shutil.copytree(self.target, self.root,
                            ignore=shutil.ignore_patterns(*IGNORED_DIRS))
        else:
            self.root.mkdir(parents=True)
        self.test_timeout = test_timeout
        self.python_timeout = python_timeout
        # Snapshot so cheats and commits are computed against the true original.
        self.originals: dict[str, str] = {
            rel: text for rel, text in self._iter_texts()}
        from lumen2.sandbox import InnerSandbox, OuterSandbox
        self.bucket = bucket
        if bucket is not None:
            bucket.confine_to(self.root)
        self.sandbox = OuterSandbox(self.root, self.target, bucket,
                                    InnerSandbox(bucket, enabled=inner_enabled),
                                    sandbox_dir=self._tmp,
                                    attempt_memory=attempt_memory)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- file plumbing --------------------------------------------------------

    def _iter_texts(self):
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            rel = path.relative_to(self.root).as_posix()
            try:
                yield rel, path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

    def list_files(self) -> str:
        rows = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in IGNORED_DIRS for part in path.parts):
                continue
            rel = path.relative_to(self.root).as_posix()
            tag = "  [test file: read-only]" if is_test_path(rel) else ""
            rows.append(f"{rel} ({path.stat().st_size} bytes){tag}")
        return "\n".join(rows) if rows else "(workspace is empty)"

    def read_file(self, rel: str) -> str:
        full = safe_join(self.root, rel)
        if full is None:
            return f"ERROR: path {rel!r} is outside the workspace"
        if not full.is_file():
            return f"ERROR: no such file: {rel!r}"
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"ERROR: could not read {rel!r}: {exc}"
        if len(text) > MAX_FILE_CHARS:
            return text[:MAX_FILE_CHARS] + f"\n... [truncated, {len(text)} chars total]"
        return text if text else "(empty file)"

    def search(self, pattern: str) -> str:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for rel, text in self._iter_texts():
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        hits.append("... [more hits truncated]")
                        return "\n".join(hits)
        return "\n".join(hits) if hits else f"(no matches for {pattern!r})"

    def write_file(self, rel: str, content: str) -> str:
        """Raw write. Callers must have passed the kernel gate first."""
        full = safe_join(self.root, rel)
        if full is None:
            return f"ERROR: path {rel!r} is outside the workspace"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
        return f"wrote {rel} ({len(content)} chars)"

    # -- execution -------------------------------------------------------------

    def run_tests(self) -> TestResult:
        res = self.sandbox.run([sys.executable] + PYTEST_ARGV,
                               timeout=self.test_timeout, kind="tests")
        output = f"{res.stdout}\n{res.stderr}".strip()
        if res.blocks:
            output += "\n" + "\n".join(
                f"LUMEN2-BLOCK[{b['layer']}]: {b['detail']}" for b in res.blocks)
        return parse_pytest(output, res.returncode, res.timed_out)

    def run_python(self, code: str) -> str:
        if not isinstance(code, str) or not code.strip():
            return "ERROR: empty code"
        res = self.sandbox.run([sys.executable, "-X", "utf8", "-c", code],
                               timeout=self.python_timeout, kind="python")
        if res.timed_out:
            return f"ERROR: timed out after {self.python_timeout}s"
        pieces = []
        if res.stdout.strip():
            pieces.append(f"stdout:\n{res.stdout.strip()}")
        if res.stderr.strip():
            pieces.append(f"stderr:\n{res.stderr.strip()}")
        for b in res.blocks:
            pieces.append(f"LUMEN2-BLOCK[{b['layer']}]: {b['detail']}")
        pieces.append(f"exit code: {res.returncode}")
        return _truncate("\n".join(pieces))

    def original_root(self) -> Path:
        """A pristine copy of the original tree, outside the scratch root
        (used to check attacker tests against the unpatched code)."""
        orig = self._tmp / "original"
        if not orig.is_dir():
            shutil.copytree(self.target, orig,
                            ignore=shutil.ignore_patterns(*IGNORED_DIRS)) \
                if self.target is not None else orig.mkdir()
        return orig

    # -- diffing and commit ------------------------------------------------------

    def changed_files(self) -> dict[str, str]:
        """rel -> new content for every file that differs from the original."""
        current = dict(self._iter_texts())
        changed = {}
        for rel, text in current.items():
            if self.originals.get(rel) != text:
                changed[rel] = text
        for rel in self.originals:
            if rel not in current:
                changed[rel] = ""  # deleted; commit writes empty marker-free skip
        return changed

    def commit_to_target(self) -> list[str]:
        """Export the verified diff to the real target via the outer sandbox."""
        return self.sandbox.export(self.changed_files())
