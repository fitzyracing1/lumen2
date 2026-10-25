"""CLI for the Lumen 2 bug benchmark: python -m lumen2.bench --help"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from lumen2.backends import BackendError, from_spec
from lumen2.bench import (RESULTS_DIR, format_markdown, preflight,
                          run_benchmark)
from lumen2.memory import Memory


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="lumen2 bench",
        description="Run the Lumen 2 agent on the bug fixtures and report "
                    "fixed rate, wrong fixes vs hidden tests, cheats caught, "
                    "withholds, steps, tokens, and time.")
    parser.add_argument("--model", default="stub",
                        help="stub (default) | ollama:<model> | amalia | "
                             "openai:<model> | url:<base_url>::<model>")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--fixtures", default=None,
                        help="comma-separated fixture names (default: all)")
    parser.add_argument("--difficulty", choices=("base", "hard"), default=None,
                        help="run only base (v1-ported) or hard (v2) fixtures")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--test-timeout", type=float, default=60.0)
    parser.add_argument("--memory", default=None, metavar="PATH",
                        help="shared lesson store across fixtures (default: off)")
    parser.add_argument("--reset-memory", action="store_true")
    parser.add_argument("--out", default=None, metavar="PREFIX",
                        help="write PREFIX.json and PREFIX.md "
                             "(default: bench/results/<model>[-mem])")
    parser.add_argument("--check-only", action="store_true",
                        help="only check backend reachability, then exit")
    args = parser.parse_args(argv)

    try:
        backend = from_spec(args.model, base_url=args.base_url)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ok, message = preflight(backend)
    print(message, flush=True)
    if not ok:
        return 2
    if args.check_only:
        return 0

    memory = None
    if args.memory:
        memory = Memory(args.memory)
        if args.reset_memory:
            memory.reset()
            print(f"memory reset: {args.memory}")
    elif args.reset_memory:
        parser.error("--reset-memory requires --memory PATH")

    only = ([f.strip() for f in args.fixtures.split(",")]
            if args.fixtures else None)
    if args.difficulty:
        from lumen2.bench import discover_fixtures
        only = [f["name"] for f in discover_fixtures(only=only)
                if f["meta"].get("difficulty", "base") == args.difficulty]

    report = run_benchmark(backend, max_steps=args.max_steps,
                           test_timeout=args.test_timeout, memory=memory,
                           only=only,
                           progress=lambda msg: print(msg, flush=True))

    if args.out:
        prefix = Path(args.out)
    else:
        slug = re.sub(r"[^a-z0-9.]+", "-", report["model"].lower()).strip("-")
        prefix = RESULTS_DIR / (slug + ("-mem" if memory else ""))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    markdown = format_markdown(report)
    prefix.with_suffix(".json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    prefix.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    print(f"wrote {prefix.with_suffix('.json')} and {prefix.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
