"""The attacker stage ("give it grok to attack" — Joshua Almeida).

Runs after the kernel's own verification (visible tests green, cheat scan
clean) and BEFORE any commit. A separate attacker model reads the task, the
proposed diff and the visible tests, and writes extra pytest tests that try
to break the fix: edge cases, the spec's intent, what the visible tests miss.

Safety and honesty properties:
- Generated tests live only in memory and are written only into the inner
  sandbox's throwaway run copy (``OuterSandbox.run(extra_files=...)``). They
  never touch the scratch workspace, so they can never be in the exported
  diff, and the visible tests stay read-only.
- Bad attacker tests are filtered. Each failing test is classified:
    * it errors/fails the same way on the original code AND looks broken
      (collection error, import/name/syntax/type error at setup)  -> dropped
    * it PASSES on the original code but fails on the patch        -> kept
      (a regression the fix introduced; no judgment needed)
    * otherwise the critic/judge decides whether the failure reflects the
      spec; tests it calls wrong are dropped.
- Every attacker and judge call goes through ``BoundBucket.authorize_model_call``
  and is charged (tokens and cost, at the attacker model's price). Over
  budget, the stage is skipped and that is recorded.
"""

from __future__ import annotations

import difflib
import json
import re
import sys

from lumen2.workspace import PYTEST_ARGV, is_test_path

ATTACK_FILE = "test_lumen2_attack.py"
MAX_TEST_CHARS = 8_000
MAX_GENERATIONS = 2          # attacker model calls per run
MAX_JUDGE_CALLS = 3          # judge calls per run
_BROKEN = re.compile(r"\b(ImportError|ModuleNotFoundError|NameError|SyntaxError|"
                     r"IndentationError|fixture '.*' not found|AttributeError: module)\b")

_ATTACK_SYSTEM = """You are Lumen 2's attacker. A proposed bug fix already passes the visible tests. Your job: write EXTRA pytest tests that would expose the fix if it is wrong or incomplete.

Target: the intended behavior described by the task, docstrings, names and the visible tests — edge cases, boundary values, other call paths, other files with the same bug, things the visible tests miss. In particular:
- Test EVERY function the diff touches directly against its own docstring/contract (units, scale, return type), not only through the visible tests' entry points. A classic wrong fix makes the visible tests pass by changing a helper's documented contract (e.g. what a value means) instead of fixing the caller that misuses it; other callers rely on that contract.
- Test the callers of changed functions too, across files. Do NOT invent requirements the spec does not imply; a test that asserts made-up behavior is worse than none. Import the code exactly the way the visible tests do. At most 6 small, deterministic test functions, no network, no file writes, no sleeping.

Respond with EXACTLY ONE JSON object and nothing else:
{"tests": "<full python source of one pytest file>", "rationale": "<one line>"}"""

_JUDGE_SYSTEM = """You are Lumen 2's critic judging an attacker-written test. The test fails against a proposed fix. Decide whether the failure reflects the real spec (the task, docstrings, visible tests) — i.e. the fix is genuinely wrong — or whether the test itself is wrong (asserts behavior the spec does not imply, has a bug, wrong expected value).

Respond with EXACTLY ONE JSON object and nothing else:
{"valid": true/false, "reason": "<one line>"}"""


def _extract_json(text):
    from lumen2.backends.openai_compat import extract_json
    return extract_json(text)


class Attacker:
    """Interface. ``attack`` returns pytest source (or ''), ``judge`` returns
    (valid, reason) or None when unavailable."""
    name = "base"
    paid = False

    def attack(self, brief: dict) -> str:
        raise NotImplementedError

    def judge(self, brief: dict, test_name: str, test_source: str,
              failure: str) -> tuple[bool, str] | None:
        raise NotImplementedError

    def usage(self) -> tuple[int, int]:
        """(prompt_tokens, completion_tokens) so far."""
        return 0, 0


class StubAttacker(Attacker):
    """Deterministic, offline. By default writes a harmless smoke test that
    imports every changed module; tests pass their own sources/verdicts."""
    name = "stub"

    def __init__(self, tests: list[str] | str | None = None,
                 verdict: bool = True):
        self.scripted = [tests] if isinstance(tests, str) else list(tests or [])
        self.verdict = verdict
        self.calls = 0
        self.judge_calls = 0
        self.briefs: list[dict] = []

    def attack(self, brief: dict) -> str:
        self.calls += 1
        self.briefs.append(brief)
        if self.scripted:
            return self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]
        mods = [p[:-3].replace("/", ".") for p in brief.get("changed", [])
                if p.endswith(".py") and not is_test_path(p)]
        body = "".join(f"\ndef test_import_{i}():\n    import {m}\n"
                       for i, m in enumerate(mods))
        return ("import importlib\n" + body) if body else ""

    def judge(self, brief, test_name, test_source, failure):
        self.judge_calls += 1
        return self.verdict, "stub verdict"


class ModelAttacker(Attacker):
    """Attacker + judge on any OpenAI-compatible backend (default xai:grok-4.6)."""

    def __init__(self, backend):
        self.backend = backend
        self.name = backend.name
        self.paid = bool(getattr(backend, "paid", False))

    def usage(self):
        b = self.backend
        p = getattr(b, "prompt_tokens_used", 0)
        c = getattr(b, "completion_tokens_used", 0)
        if p + c < b.tokens_used:
            p += b.tokens_used - (p + c)
        return p, c

    def attack(self, brief: dict) -> str:
        msg = self.backend.chat([
            {"role": "system", "content": _ATTACK_SYSTEM},
            {"role": "user", "content": json.dumps(brief, indent=1)[:24_000]}],
            temperature=0.2)
        if msg is None:
            return ""
        obj = _extract_json(msg.get("content"))
        src = obj.get("tests", "") if isinstance(obj, dict) else ""
        if not src:   # tolerate a fenced code block instead of JSON
            m = re.search(r"```(?:python)?\n(.*?)```", str(msg.get("content")), re.S)
            src = m.group(1) if m else ""
        return str(src)[:MAX_TEST_CHARS]

    def judge(self, brief, test_name, test_source, failure):
        state = {"task": brief.get("task"), "diff": brief.get("diff"),
                 "visible_tests": brief.get("visible_tests"),
                 "test_name": test_name, "attacker_tests": test_source,
                 "failure_on_patched_code": failure[:3000]}
        msg = self.backend.chat([
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": json.dumps(state, indent=1)[:20_000]}],
            temperature=0.0)
        obj = _extract_json(msg.get("content")) if msg else None
        if not isinstance(obj, dict) or "valid" not in obj:
            return None
        return bool(obj["valid"]), str(obj.get("reason", ""))[:200]


def from_spec(spec: str | None, base_url=None, api_key_env=None) -> Attacker:
    spec = (spec or "xai:grok-4.6").strip()
    if spec in ("stub", "offline"):
        return StubAttacker()
    from lumen2.backends import from_spec as backend_from_spec
    return ModelAttacker(backend_from_spec(spec, base_url=base_url,
                                           api_key_env=api_key_env))


def _failed_tests(output: str) -> dict[str, str]:
    """test name -> one-line reason, from pytest -rfE output."""
    out = {}
    for line in output.splitlines():
        m = re.match(r"^(FAILED|ERROR) (\S+?)(?: - (.*))?$", line.strip())
        if m and ATTACK_FILE in m.group(2):
            name = m.group(2).split("::")[-1] if "::" in m.group(2) else "<collection>"
            out[name] = f"{m.group(1)} {m.group(3) or ''}".strip()
    return out


def _test_names(src: str) -> list[str]:
    return re.findall(r"^\s*def (test_\w+)\s*\(", src, re.M)


class AttackStage:
    """Per-run attacker state, owned by the Agent (outer layer only)."""

    def __init__(self, attacker: Attacker, bucket, log=None):
        self.attacker = attacker
        self.bucket = bucket
        self.log = log or (lambda *a: None)
        self.generations = 0
        self.judge_calls = 0
        self.kept_source = ""          # tests that survived filtering
        self.stats = {"attacker_calls": 0, "attacker_blocks": 0,
                      "attacker_tests_run": 0, "attacker_tests_dropped": 0,
                      "attacker_skipped": 0}
        self._charged = attacker.usage()

    # -- bucket plumbing -----------------------------------------------------
    def _authorize(self) -> bool:
        if self.bucket is None:
            return True
        if self.bucket.authorize_model_call():
            return False
        self.bucket.attacker_calls += 1
        return True

    def _charge(self) -> None:
        p, c = self.attacker.usage()
        p0, c0 = self._charged
        self._charged = (p, c)
        if self.bucket is not None:
            self.bucket.charge(p - p0, c - c0, paid=self.attacker.paid,
                               model=self.attacker.name)

    # -- the stage -------------------------------------------------------------
    def brief(self, workspace, task: str) -> dict:
        changed = workspace.changed_files()
        diff = "".join(
            "".join(difflib.unified_diff(
                workspace.originals.get(rel, "").splitlines(True),
                text.splitlines(True), f"a/{rel}", f"b/{rel}"))
            for rel, text in sorted(changed.items()))
        visible = {rel: t[:6000] for rel, t in workspace.originals.items()
                   if is_test_path(rel) and rel.endswith(".py")}
        sources = {rel: t[:6000] for rel, t in workspace._iter_texts()
                   if rel.endswith(".py") and not is_test_path(rel)}
        original = {rel: workspace.originals[rel][:6000] for rel in changed
                    if rel in workspace.originals and rel.endswith(".py")}
        return {"task": task, "diff": diff[:12_000], "changed": sorted(changed),
                "original_sources_of_changed_files": original,
                "visible_tests": visible, "patched_sources": sources}

    def _run(self, workspace, source: str, original: bool = False):
        argv = [sys.executable] + PYTEST_ARGV + ["-rfE", ATTACK_FILE]
        kwargs = {"extra_files": {ATTACK_FILE: source}}
        if original:
            kwargs["source_root"] = workspace.original_root()
        res = workspace.sandbox.run(argv, timeout=workspace.test_timeout,
                                    kind="tests", **kwargs)
        out = f"{res.stdout}\n{res.stderr}"
        return res, out

    def check(self, workspace, task: str) -> dict:
        """{"blocked": bool, "failures": {name: reason}, "note": str}"""
        source = self.kept_source
        if self.generations < MAX_GENERATIONS:
            if self._authorize():
                self.generations += 1
                self.stats["attacker_calls"] += 1
                try:
                    fresh = self.attacker.attack(self.brief(workspace, task))
                finally:
                    self._charge()
                if fresh.strip():
                    try:
                        compile(fresh, ATTACK_FILE, "exec")
                    except SyntaxError:
                        self.log("attack-drop", "attacker tests do not parse")
                        self.stats["attacker_tests_dropped"] += 1
                        fresh = ""
                if fresh.strip():
                    # earlier kept tests ride along, renamed to avoid clashes
                    prior = re.sub(r"^def (test_\w+)",
                                   rf"def \1_r{self.generations}",
                                   self.kept_source, flags=re.M)
                    source = (fresh + "\n\n" + prior) if prior else fresh
            else:
                self.stats["attacker_skipped"] += 1
                self.log("attack-skip", "bucket refused the attacker call")
        if not source.strip():
            return {"blocked": False, "failures": {}, "note": "no attacker tests"}

        names = _test_names(source)
        self.stats["attacker_tests_run"] += len(names)
        res, out = self._run(workspace, source)
        failures = _failed_tests(out)
        if res.returncode == 0 and not failures:
            self.kept_source = source
            self.log("attack-pass", f"{len(names)} attacker test(s) pass")
            return {"blocked": False, "failures": {}, "note":
                    f"{len(names)} attacker test(s) pass"}
        if not failures:  # nonzero exit but nothing parsed: collection failure
            failures = {"<collection>": out.strip().splitlines()[-1:][0]
                        if out.strip() else "pytest failed"}

        # Guard against bad tests: compare with the original code.
        _, out_orig = self._run(workspace, source, original=True)
        orig_fail = _failed_tests(out_orig)
        valid: dict[str, str] = {}
        for name, why in failures.items():
            if name == "<collection>" or (name in orig_fail and _BROKEN.search(
                    why + " " + orig_fail.get(name, ""))):
                self.stats["attacker_tests_dropped"] += 1
                self.log("attack-drop", f"{name}: broken test ({why[:80]})")
                continue
            if name not in orig_fail:   # passes on original, fails on patch
                valid[name] = f"regression vs original code: {why}"
                continue
            verdict = None
            if self.judge_calls < MAX_JUDGE_CALLS and self._authorize():
                self.judge_calls += 1
                self.stats["attacker_calls"] += 1
                try:
                    verdict = self.attacker.judge(
                        self.brief(workspace, task), name, source,
                        _excerpt(out, name))
                finally:
                    self._charge()
            if verdict is None or verdict[0]:
                valid[name] = why + (f" (judge: {verdict[1]})" if verdict else "")
            else:
                self.stats["attacker_tests_dropped"] += 1
                self.log("attack-drop", f"{name}: judge says test is wrong "
                                        f"({verdict[1][:80]})")
        source = _drop_tests(source, set(failures) - set(valid))
        self.kept_source = source
        if not valid:
            return {"blocked": False, "failures": {},
                    "note": "attacker failures all judged invalid"}
        self.stats["attacker_blocks"] += 1
        return {"blocked": True, "failures": valid, "note": _excerpt(out, "")}


def _excerpt(output: str, name: str) -> str:
    lines = output.splitlines()
    keep = [l for l in lines if (name and name in l) or l.startswith("E ")]
    return "\n".join(keep[:12])[:1500]


def _drop_tests(source: str, names: set[str]) -> str:
    """Remove top-level test functions by name (keeps the rest)."""
    if not names:
        return source
    out, skip = [], False
    for line in source.splitlines(True):
        m = re.match(r"^def (test_\w+)\s*\(", line)
        if m:
            skip = m.group(1) in names
        elif line and not line[0].isspace() and line.strip() and not line.startswith("@"):
            skip = False
        if not skip:
            out.append(line)
    return "".join(out)
