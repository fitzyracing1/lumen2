"""v1-vs-v2 comparison runner.

Runs the v1 benchmark (from a checkout of github.com/fitzyracing1/lumen,
branch cursor/lumen-bench-memory-a399) and the v2 benchmark on the 13 shared
fixtures with the same model, then writes a side-by-side report.

    python -m lumen2.bench.compare --model ollama:qwen2.5-coder:1.5b \
        [--v1 /path/to/lumen-v1]   (cloned automatically when omitted)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lumen2.backends import from_spec
from lumen2.bench import RESULTS_DIR, run_benchmark

V1_REPO = "https://github.com/fitzyracing1/lumen"
V1_BRANCH = "cursor/lumen-bench-memory-a399"


def ensure_v1(path: str | None) -> Path:
    if path:
        repo = Path(path).resolve()
        if not (repo / "lumen_bench").is_dir():
            raise SystemExit(f"{repo} does not look like a v1 checkout "
                             f"(no lumen_bench/)")
        return repo
    dest = Path(tempfile.gettempdir()) / "lumen-v1-for-compare"
    if (dest / "lumen_bench").is_dir():
        return dest
    print(f"cloning {V1_REPO} ({V1_BRANCH}) into {dest} ...", flush=True)
    subprocess.run(["git", "clone", "--depth", "1", "-b", V1_BRANCH,
                    V1_REPO, str(dest)], check=True)
    return dest


def v1_model_args(spec: str, base_url: str | None) -> tuple[list[str], dict]:
    """Map a v2 model spec onto v1's --model syntax + env."""
    env = dict(os.environ)
    if spec in ("stub", ""):
        return ["--model", "stub"], env
    if spec == "amalia":
        spec = "ollama:" + os.environ.get("AMALIA_MODEL", "amalia:latest")
        base_url = base_url or os.environ.get("AMALIA_BASE_URL",
                                              "http://127.0.0.1:11434/v1")
    if spec.startswith("ollama:"):
        model = spec.split(":", 1)[1]
        url = base_url or os.environ.get("OLLAMA_BASE_URL",
                                         "http://127.0.0.1:11434/v1")
        return ["--model", f"openai:{model}", "--base-url", url], env
    if spec.startswith("openai:"):
        model = spec.split(":", 1)[1]
        url = base_url or os.environ.get("OPENAI_BASE_URL",
                                         "https://api.openai.com/v1")
        return ["--model", f"openai:{model}", "--base-url", url], env
    raise SystemExit(f"cannot map model spec {spec!r} onto the v1 benchmark")


def run_v1(v1_repo: Path, spec: str, base_url: str | None,
           max_cycles: int) -> dict:
    model_args, env = v1_model_args(spec, base_url)
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "v1-report"
        cmd = [sys.executable, "-m", "lumen_bench", *model_args,
               "--max-cycles", str(max_cycles), "--out", str(prefix)]
        print("v1:", " ".join(cmd), flush=True)
        proc = subprocess.run(cmd, cwd=str(v1_repo), env=env)
        if proc.returncode != 0:
            raise SystemExit(f"v1 benchmark failed (exit {proc.returncode})")
        return json.loads(prefix.with_suffix(".json").read_text(
            encoding="utf-8"))


def side_by_side(v1: dict, v2: dict, model: str) -> str:
    v1_by_name = {r["fixture"]: r for r in v1["results"]}
    s1, s2 = v1["summary"], v2["summary"]
    lines = [
        f"# Lumen v1 vs v2 — {model}",
        "",
        f"- date: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"- shared fixtures: the 13 v1 bugs (12 fixable + 1 impossible)",
        f"- v1: whole-patch kernel, max cycles {v1.get('max_cycles')}"
        f" · v2: tool-using agent, max steps {v2.get('max_steps')}",
        "",
        "|  | v1 | v2 |",
        "|---|---|---|",
        f"| fixed (of {s2['fixable_base']} fixable) | {s1['fixed']} | "
        f"{s2['fixed_base']} |",
        f"| wrong fixes vs hidden tests | {s1['wrong_fixes']} | "
        f"{s2['wrong_fixes']} |",
        f"| impossible fixture handled | {s1['impossible_handled']} | "
        f"{s2['impossible_handled']} |",
        f"| cheats caught | {s1['cheats_caught']} | {s2['cheats_caught']} |",
        f"| wall time (s) | {s1['total_seconds']} | {s2['total_seconds']} |",
        "",
        "| fixture | v1 outcome | v2 outcome | v2 steps |",
        "|---|---|---|---|",
    ]
    for r2 in v2["results"]:
        r1 = v1_by_name.get(r2["fixture"])
        lines.append(f"| {r2['fixture']} | {r1['outcome'] if r1 else '—'} | "
                     f"{r2['outcome']} | {r2['steps']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lumen2.bench.compare",
        description="Run v1 and v2 on the 13 shared fixtures and write a "
                    "side-by-side report.")
    parser.add_argument("--model", default="stub")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--v1", default=None,
                        help="path to a v1 checkout (cloned when omitted)")
    parser.add_argument("--v1-max-cycles", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--out", default=None, metavar="PREFIX")
    args = parser.parse_args(argv)

    v1_repo = ensure_v1(args.v1)
    v1_report = run_v1(v1_repo, args.model, args.base_url, args.v1_max_cycles)

    backend = from_spec(args.model, base_url=args.base_url)
    from lumen2.bench import discover_fixtures
    shared = [f["name"] for f in discover_fixtures()
              if f["meta"].get("source") == "v1"]
    v2_report = run_benchmark(backend, max_steps=args.max_steps, only=shared,
                              progress=lambda m: print(m, flush=True))

    markdown = side_by_side(v1_report, v2_report, backend.name)
    slug = re.sub(r"[^a-z0-9.]+", "-", backend.name.lower()).strip("-")
    prefix = Path(args.out) if args.out else RESULTS_DIR / f"compare-{slug}"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(
        {"v1": v1_report, "v2": v2_report}, indent=2) + "\n", encoding="utf-8")
    prefix.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    print(f"wrote {prefix.with_suffix('.json')} and {prefix.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
