"""The lumen2 command line.

    lumen2 fix <dir>        fix the failing tests in a directory
    lumen2 ask "question"   answer one question with verified confidence
    lumen2 chat             interactive ask loop
    lumen2 bench ...        run the bug benchmark (see lumen2 bench --help)

Model selection: --model or LUMEN2_MODEL. Default is the offline stub.
"""

from __future__ import annotations

import argparse
import json
import sys


def _utf8_console() -> None:
    """UTF-8 output on every platform, PowerShell and cmd.exe included."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=None,
                        help="stub (default) | ollama:<model> | amalia | "
                             "openai:<model> | url:<base_url>::<model>")
    parser.add_argument("--base-url", default=None,
                        help="override the backend base URL")
    parser.add_argument("--memory", default=None, metavar="PATH",
                        help="persistent lesson store (JSON file)")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="write the full JSON report here")
    parser.add_argument("--verbose", action="store_true",
                        help="print each step as it happens")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(
        prog="lumen2",
        description="Lumen 2 — a kernel that makes a tool-using agent check "
                    "its own work before it acts.")
    sub = parser.add_subparsers(dest="command")

    p_fix = sub.add_parser("fix", help="fix the failing tests in a directory")
    p_fix.add_argument("target", help="directory with a failing test suite")
    p_fix.add_argument("--max-steps", type=int, default=30)
    p_fix.add_argument("--test-timeout", type=float, default=60.0)
    _add_common(p_fix)

    p_ask = sub.add_parser("ask", help="answer one question")
    p_ask.add_argument("question")
    p_ask.add_argument("--max-steps", type=int, default=10)
    _add_common(p_ask)

    p_chat = sub.add_parser("chat", help="interactive ask loop")
    p_chat.add_argument("--max-steps", type=int, default=10)
    _add_common(p_chat)

    p_forget = sub.add_parser("forget", help="wipe a memory store")
    p_forget.add_argument("memory_path")

    sub.add_parser("bench", add_help=False,
                   help="run the benchmark (lumen2 bench --help)")

    args, extra = parser.parse_known_args(argv)

    if args.command == "bench":
        from lumen2.bench.__main__ import main as bench_main
        return bench_main(extra)
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "forget":
        from lumen2.memory import Memory
        Memory(args.memory_path).reset()
        print(f"forgot {args.memory_path}")
        return 0

    from lumen2.agent import run_ask, run_fix
    from lumen2.backends import BackendError, from_spec
    from lumen2.memory import Memory

    try:
        backend = from_spec(args.model, base_url=args.base_url)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    memory = Memory(args.memory) if args.memory else None

    if args.command == "fix":
        report = run_fix(args.target, backend, memory=memory,
                         max_steps=args.max_steps,
                         test_timeout=args.test_timeout, verbose=args.verbose)
        _emit(report, args.out)
        print(_fix_banner(report))
        return 0 if report["outcome"] == "committed" else 1

    if args.command == "ask":
        report = run_ask(args.question, backend, memory=memory,
                         max_steps=args.max_steps, verbose=args.verbose)
        _emit(report, args.out)
        print(_ask_banner(report))
        return 0

    if args.command == "chat":
        print("lumen2 chat — ask anything; blank line or 'exit' quits.")
        while True:
            try:
                question = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not question or question.lower() in ("exit", "quit"):
                return 0
            report = run_ask(question, backend, memory=memory,
                             max_steps=args.max_steps, verbose=args.verbose)
            print(_ask_banner(report))
    return 2


def _emit(report: dict, out: str | None) -> None:
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"wrote {out}")


def _fix_banner(report: dict) -> str:
    kernel = report.get("kernel", {})
    stats = kernel.get("stats", {})
    cheats = sum(v for k, v in stats.items() if k.startswith("cheat:"))
    if report["outcome"] == "committed":
        head = (f"FIXED: {report.get('verification', '')} — changed "
                f"{', '.join(report.get('files_changed', []))}")
    else:
        head = f"WITHHELD: {report.get('reason', '')}"
    return (f"{head}\n(steps={report['steps']} tokens={report['tokens']} "
            f"time={report['seconds']}s cheats_caught={cheats} "
            f"backend={report['backend']})")


def _ask_banner(report: dict) -> str:
    answer = report.get("answer", "")
    conf = report.get("confidence")
    why = report.get("summary") or report.get("reason", "")
    conf_text = f" (confidence {conf:.2f})" if isinstance(conf, float) else ""
    verified = (" [verified by running code]"
                if report.get("verified_by_execution") else "")
    return f"lumen2> {answer}{conf_text}{verified}\n        why: {why}"


if __name__ == "__main__":
    raise SystemExit(main())
