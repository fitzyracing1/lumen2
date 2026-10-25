"""The Lumen 2 bug benchmark.

Each fixture under bench/fixtures/ is a small repository with a real bug,
visible failing tests, and hidden held-out tests the agent never sees. After
a run, the final target is checked against visible + hidden tests, which
catches "fixes" that merely satisfy what the model could see.

13 fixtures are ported verbatim from v1 (source=v1, difficulty=base — the
shared yardstick, including one impossible fixture where withholding is the
correct outcome). 10 are new and harder (source=v2, difficulty=hard):
multi-file pipelines, misleading tracebacks, contracts that live in docs,
state leaks, and paired bugs.

Per fixture we report: outcome, hidden-test verdict, steps, tokens, wall
time, cheats caught by the kernel, and withholds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lumen2.agent import run_fix
from lumen2.backends import Backend, from_spec
from lumen2.backends.openai_compat import OpenAICompatBackend
from lumen2.memory import Memory

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = REPO_ROOT / "bench" / "fixtures"
RESULTS_DIR = REPO_ROOT / "bench" / "results"
HIDDEN_TIMEOUT = 60.0


def preflight(backend: Backend) -> tuple[bool, str]:
    """Cheap reachability check before spending a benchmark on a dead URL."""
    if not isinstance(backend, OpenAICompatBackend):
        return True, f"{backend.name}: offline backend, nothing to check"
    reply = backend.chat([
        {"role": "system", "content": "You are a health check."},
        {"role": "user", "content": "Reply with the single word: ok"}])
    if reply is None:
        return False, (f"{backend.name}: no reply from {backend.base_url} "
                       f"(last error: {backend.last_error})")
    return True, (f"{backend.name}: reachable at {backend.base_url} "
                  f"(replied {str(reply.get('content', ''))[:40]!r})")


def discover_fixtures(fixtures_dir: Path = FIXTURES_DIR,
                      only: list[str] | None = None) -> list[dict]:
    fixtures = []
    for path in sorted(fixtures_dir.iterdir()):
        if not (path / "target").is_dir() or not (path / "meta.json").is_file():
            continue
        if only and path.name not in only:
            continue
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        fixtures.append({"name": path.name, "path": path, "meta": meta})
    return fixtures


def _run_pytest_ok(directory: Path, timeout: float) -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=str(directory), capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def verify_hidden(final_target: Path, fixture_path: Path) -> bool:
    """Visible + hidden tests on the final state, in a throwaway copy."""
    tmp = Path(tempfile.mkdtemp(prefix="lumen2-hidden-"))
    try:
        work = tmp / "work"
        shutil.copytree(final_target, work,
                        ignore=shutil.ignore_patterns("__pycache__",
                                                      ".pytest_cache"))
        for hidden in sorted((fixture_path / "hidden").iterdir()):
            shutil.copy(hidden, work / hidden.name)
        return _run_pytest_ok(work, HIDDEN_TIMEOUT)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_fixture(fixture: dict, backend: Backend, max_steps: int,
                test_timeout: float, memory: Memory | None,
                bucket_factory=None) -> dict:
    expect_fix = bool(fixture["meta"].get("expect_fix", True))
    tmp = Path(tempfile.mkdtemp(prefix="lumen2-bench-"))
    started = time.monotonic()
    tokens_before = backend.tokens_used
    try:
        target = tmp / "target"
        shutil.copytree(fixture["path"] / "target", target)
        bucket = bucket_factory() if bucket_factory else None
        report = run_fix(target, backend, memory=memory, max_steps=max_steps,
                         test_timeout=test_timeout, bucket=bucket)
        committed = report["outcome"] == "committed"
        hidden_ok = (verify_hidden(target, fixture["path"])
                     if committed else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    stats = report["kernel"]["stats"]
    cheats = {k.split(":", 1)[1]: v for k, v in stats.items()
              if k.startswith("cheat:")}
    truly_fixed = bool(committed and hidden_ok)
    wrong_fix = bool(committed and not hidden_ok)
    if expect_fix:
        outcome = ("fixed" if truly_fixed
                   else "wrong-fix" if wrong_fix
                   else "unsolved")
        correct = truly_fixed
    else:
        outcome = "withheld-correct" if not committed else "committed-on-impossible"
        correct = not committed
    return {
        "fixture": fixture["name"],
        "category": fixture["meta"].get("category", ""),
        "difficulty": fixture["meta"].get("difficulty", "base"),
        "source": fixture["meta"].get("source", "v2"),
        "expect_fix": expect_fix,
        "outcome": outcome,
        "correct": correct,
        "committed": committed,
        "hidden_ok": hidden_ok,
        "wrong_fix": wrong_fix,
        "withheld": report["outcome"] == "withheld",
        "withhold_chosen": bool(report.get("withhold_chosen")),
        "steps": report["steps"],
        "tokens": max(0, backend.tokens_used - tokens_before),
        "cheats_caught": sum(cheats.values()),
        "cheat_kinds": cheats,
        "finish_rejected": stats.get("finish_rejected", 0),
        "edits_rejected": stats.get("edits_rejected", 0),
        "invalid_actions": stats.get("invalid_actions", 0),
        "policy_revisions": report["kernel"]["revisions_used"],
        "bucket_breaches": stats.get("bucket_breaches", 0),
        "breach_layers": sorted({b["layer"] for b in
                                 report["kernel"].get("bucket_breaches", [])}),
        "cost_usd": (report["kernel"].get("bucket") or {}).get("cost_usd", 0.0),
        "seconds": round(time.monotonic() - started, 1),
    }


def run_benchmark(backend: Backend, max_steps: int = 25,
                  test_timeout: float = 60.0, memory: Memory | None = None,
                  only: list[str] | None = None,
                  fixtures_dir: Path = FIXTURES_DIR, progress=None,
                  bucket_factory=None) -> dict:
    fixtures = discover_fixtures(fixtures_dir, only)
    if not fixtures:
        raise SystemExit(f"no fixtures found in {fixtures_dir}")
    results = []
    for fixture in fixtures:
        if progress:
            progress(f"running {fixture['name']} ...")
        result = run_fixture(fixture, backend, max_steps, test_timeout, memory,
                             bucket_factory)
        if progress:
            progress(f"  {fixture['name']}: {result['outcome']} "
                     f"(steps={result['steps']}, {result['seconds']}s)")
        results.append(result)
    return {
        "benchmark": "lumen2_bench",
        "model": backend.name,
        "memory": str(memory.path) if memory is not None else None,
        "max_steps": max_steps,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
        "summary": summarize(results),
    }


def summarize(results: list[dict]) -> dict:
    fixable = [r for r in results if r["expect_fix"]]
    impossible = [r for r in results if not r["expect_fix"]]
    fixed = [r for r in fixable if r["outcome"] == "fixed"]
    base = [r for r in fixable if r["difficulty"] == "base"]
    hard = [r for r in fixable if r["difficulty"] == "hard"]
    steps = [r["steps"] for r in fixed]
    return {
        "fixtures": len(results),
        "fixable": len(fixable),
        "fixed": len(fixed),
        "fixed_rate": round(len(fixed) / len(fixable), 3) if fixable else None,
        "fixed_base": sum(1 for r in base if r["outcome"] == "fixed"),
        "fixable_base": len(base),
        "fixed_hard": sum(1 for r in hard if r["outcome"] == "fixed"),
        "fixable_hard": len(hard),
        "wrong_fixes": sum(1 for r in results if r["wrong_fix"]),
        "cheats_caught": sum(r["cheats_caught"] for r in results),
        "withholds": sum(1 for r in results if r["withheld"]),
        "correct_withholds": sum(1 for r in results
                                 if r["outcome"] == "withheld-correct"),
        "bucket_breaches": sum(r.get("bucket_breaches", 0) for r in results),
        "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in results), 4),
        "impossible_handled": (all(r["correct"] for r in impossible)
                               if impossible else None),
        "correct_total": sum(1 for r in results if r["correct"]),
        "mean_steps_to_fix": round(sum(steps) / len(steps), 1) if steps else None,
        "total_tokens": sum(r["tokens"] for r in results),
        "total_seconds": round(sum(r["seconds"] for r in results), 1),
    }


def format_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# lumen2_bench — {report['model']}",
        "",
        f"- date: {report['timestamp']}",
        f"- memory: {report['memory'] or 'off (cold)'}",
        f"- max steps per fixture: {report['max_steps']}",
        "",
        f"**Fixed {s['fixed']}/{s['fixable']} fixable bugs** "
        f"(base: {s['fixed_base']}/{s['fixable_base']}, "
        f"hard: {s['fixed_hard']}/{s['fixable_hard']}) · "
        f"wrong fixes slipped through: {s['wrong_fixes']} · "
        f"cheats caught: {s['cheats_caught']} · "
        f"withholds: {s['withholds']} (correct: {s['correct_withholds']}) · "
        f"bucket breaches: {s['bucket_breaches']} · "
        f"est. cost: ${s['total_cost_usd']} · "
        f"impossible handled: {s['impossible_handled']} · "
        f"mean steps to fix: {s['mean_steps_to_fix'] or 'n/a'} · "
        f"tokens: {s['total_tokens']} · wall time: {s['total_seconds']}s",
        "",
        "| fixture | difficulty | outcome | steps | tokens | cheats caught | "
        "edits rejected | bucket breaches | hidden tests | seconds |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        hidden = ("green" if r["hidden_ok"] else "RED") \
            if r["hidden_ok"] is not None else "—"
        lines.append(
            f"| {r['fixture']} | {r['difficulty']} | {r['outcome']} | "
            f"{r['steps']} | {r['tokens']} | {r['cheats_caught']} | "
            f"{r['edits_rejected']} | {r.get('bucket_breaches', 0)} | "
            f"{hidden} | {r['seconds']} |")
    lines += [
        "",
        "Outcomes: `fixed` = visible and hidden tests green; `wrong-fix` = "
        "visible green but hidden held-out tests red; `unsolved` = withheld "
        "or never fixed; `withheld-correct` = correctly refused the "
        "impossible fixture; `committed-on-impossible` = committed where no "
        "correct fix exists.",
        "",
    ]
    return "\n".join(lines)
