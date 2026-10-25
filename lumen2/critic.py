"""The critic: attacks proposed edits and final answers.

Static layer (always on, deterministic):
  * sandbox: paths must stay inside the workspace
  * test files are read-only — editing one is a cheat, full stop
  * syntax: a .py edit must parse
  * assert deletion: an edit may not strip assert statements out of source
  * hardcoding: an edit that special-cases the exact inputs of the visible
    tests to return their exact expected outputs is a cheat

Model layer (non-stub backends): an adversarial critique of the edit or the
final answer, on top of — never instead of — the static checks.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from lumen2.workspace import Workspace, is_test_path, safe_join

MAX_EDIT_CHARS = 200_000


@dataclass
class Verdict:
    fatal: bool
    cheat: str | None = None  # 'edit-tests' | 'hardcode' | 'delete-assert' | None
    issues: list[str] = field(default_factory=list)
    penalty: float = 0.0

    def describe(self) -> str:
        head = "REJECTED" if self.fatal else "warning"
        return f"{head}: " + ("; ".join(self.issues) if self.issues else "ok")


# ---------------------------------------------------------------------------
# Mining the visible tests for (inputs -> expected output) pairs
# ---------------------------------------------------------------------------

def _literal(node: ast.AST):
    """The Python value of a literal node, or a sentinel when not a literal."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return _NOT_LITERAL


_NOT_LITERAL = object()


def mine_test_expectations(workspace_root: Path) -> list[tuple[frozenset, object]]:
    """(frozenset of hashable call-arg literals, expected literal) per visible
    `assert f(...) == expected`. Used to spot hardcoded special cases."""
    pairs: list[tuple[frozenset, object]] = []
    for path in sorted(workspace_root.rglob("*.py")):
        rel = path.relative_to(workspace_root).as_posix()
        if not is_test_path(rel):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assert)
                    and isinstance(node.test, ast.Compare)
                    and len(node.test.ops) == 1
                    and isinstance(node.test.ops[0], ast.Eq)):
                continue
            left, right = node.test.left, node.test.comparators[0]
            call = left if isinstance(left, ast.Call) else (
                right if isinstance(right, ast.Call) else None)
            expected_node = right if call is left else left
            if call is None:
                continue
            expected = _literal(expected_node)
            if expected is _NOT_LITERAL:
                continue
            arg_values = set()
            for arg in call.args:
                value = _literal(arg)
                if value is not _NOT_LITERAL:
                    try:
                        hash(value)
                    except TypeError:
                        value = repr(value)
                    arg_values.add(value)
            try:
                hash(expected)
            except TypeError:
                expected = repr(expected)
            if arg_values:
                pairs.append((frozenset(arg_values), expected))
    return pairs


def _hardcode_hits(new_content: str,
                   expectations: list[tuple[frozenset, object]]) -> list[str]:
    """Find `if <test input literal> ... : return <expected literal>` patterns
    that mirror a visible assertion. Conservative: both sides must match the
    same assertion for a hit."""
    try:
        tree = ast.parse(new_content)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cond_literals = set()
        for sub in ast.walk(node.test):
            value = _literal(sub)
            if value is _NOT_LITERAL:
                continue
            try:
                hash(value)
            except TypeError:
                value = repr(value)
            cond_literals.add(value)
        if not cond_literals:
            continue
        returned = set()
        for sub in node.body:
            for inner in ast.walk(sub):
                if isinstance(inner, ast.Return) and inner.value is not None:
                    value = _literal(inner.value)
                    if value is not _NOT_LITERAL:
                        try:
                            hash(value)
                        except TypeError:
                            value = repr(value)
                        returned.add(value)
        if not returned:
            continue
        for arg_values, expected in expectations:
            if expected in returned and cond_literals & arg_values:
                hits.append(
                    f"branch on test input {sorted(cond_literals & arg_values)!r} "
                    f"returning the test's expected value {expected!r}")
    return hits


def _count_asserts(text: str) -> int:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return -1
    return sum(1 for n in ast.walk(tree) if isinstance(n, ast.Assert))


# ---------------------------------------------------------------------------
# The edit attack
# ---------------------------------------------------------------------------

def attack_edit(workspace: Workspace, rel: str, new_content: str) -> Verdict:
    """Static adversarial pass over one proposed edit."""
    issues: list[str] = []
    if safe_join(workspace.root, rel) is None:
        return Verdict(True, cheat=None,
                       issues=[f"path {rel!r} escapes the workspace sandbox"])
    if is_test_path(rel):
        return Verdict(True, cheat="edit-tests",
                       issues=[f"{rel!r} is a test file; test files are "
                               "read-only — fix the source instead"])
    if not isinstance(new_content, str):
        return Verdict(True, issues=["edit content is not text"])
    if len(new_content) > MAX_EDIT_CHARS:
        return Verdict(True, issues=[f"edit too large ({len(new_content)} chars)"])

    original = workspace.originals.get(rel)
    current = None
    full = safe_join(workspace.root, rel)
    if full is not None and full.is_file():
        try:
            current = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
    if current is not None and current == new_content:
        return Verdict(True, issues=["edit is a no-op (content unchanged)"])

    if rel.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as exc:
            return Verdict(True, issues=[
                f"syntax error in {rel!r}: {exc.msg} (line {exc.lineno})"])
        baseline = original if original is not None else current
        if baseline is not None:
            before, after = _count_asserts(baseline), _count_asserts(new_content)
            if before > 0 and 0 <= after < before:
                return Verdict(True, cheat="delete-assert", issues=[
                    f"edit deletes {before - after} assert statement(s) from "
                    f"{rel!r}; weakening checks is not a fix"])
        expectations = mine_test_expectations(workspace.root)
        hits = _hardcode_hits(new_content, expectations)
        baseline_hits = _hardcode_hits(baseline, expectations) if baseline else []
        new_hits = [h for h in hits if h not in baseline_hits]
        if new_hits:
            return Verdict(True, cheat="hardcode", issues=[
                "edit hardcodes expected test outputs: " + "; ".join(new_hits[:3])])
    return Verdict(False, issues=issues)


def attack_finish(workspace: Workspace) -> Verdict:
    """Final sweep before a commit: re-scan every changed file as if it were a
    fresh edit, so nothing slipped past (e.g. a file written before a test was
    added, or multiple edits composing into a cheat)."""
    for rel, content in workspace.changed_files().items():
        if not content:
            continue
        verdict = attack_edit_against_original(workspace, rel, content)
        if verdict.fatal:
            verdict.issues.insert(0, f"final scan of {rel!r} failed")
            return verdict
    return Verdict(False)


def attack_edit_against_original(workspace: Workspace, rel: str,
                                 new_content: str) -> Verdict:
    """Like attack_edit but diffs against the pristine original snapshot and
    skips the no-op check (changed files are by definition not no-ops)."""
    if is_test_path(rel):
        return Verdict(True, cheat="edit-tests",
                       issues=[f"{rel!r} is a test file and was modified"])
    if rel.endswith(".py"):
        try:
            ast.parse(new_content)
        except SyntaxError as exc:
            return Verdict(True, issues=[
                f"syntax error in {rel!r}: {exc.msg} (line {exc.lineno})"])
        original = workspace.originals.get(rel)
        if original is not None:
            before, after = _count_asserts(original), _count_asserts(new_content)
            if before > 0 and 0 <= after < before:
                return Verdict(True, cheat="delete-assert", issues=[
                    f"{rel!r} lost {before - after} assert statement(s)"])
        expectations = mine_test_expectations(workspace.root)
        hits = _hardcode_hits(new_content, expectations)
        original_hits = (_hardcode_hits(original, expectations)
                         if original else [])
        new_hits = [h for h in hits if h not in original_hits]
        if new_hits:
            return Verdict(True, cheat="hardcode", issues=[
                f"{rel!r} hardcodes expected test outputs: "
                + "; ".join(new_hits[:3])])
    return Verdict(False)
