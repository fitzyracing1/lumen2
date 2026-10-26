"""The lumen2 command line.

    lumen2 fix <dir>        fix the failing tests in a directory
    lumen2 ask "question"   answer one question with verified confidence
    lumen2 chat             interactive chat (default xai:grok-4.6)
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
                             "openai:<model> | xai:<model> | url:<base_url>::<model>")
    parser.add_argument("--api-key-env", default=None, metavar="NAME",
                        help="read the API key from this env var (never printed)")
    parser.add_argument("--base-url", default=None,
                        help="override the backend base URL")
    parser.add_argument("--memory", default=None, metavar="PATH",
                        help="persistent lesson store (JSON file)")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="write the full JSON report here")
    parser.add_argument("--verbose", action="store_true",
                        help="print each step as it happens")
    add_bucket_args(parser)


def add_bucket_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bucket", default=None, metavar="FILE",
                        help="BoundBucket limits override (.toml or .json)")
    parser.add_argument("--container", default=None,
                        choices=("off", "auto", "docker", "podman"),
                        help="run tests in a container as the outer layer "
                             "(default: bucket setting, off)")
    parser.add_argument("--no-attempt-memory", action="store_true",
                        help="disable outer-layer memory of failed attempts")
    parser.add_argument("--attacker-model", default=None, metavar="SPEC",
                        help="attacker that writes extra tests before every "
                             "commit (default: xai:grok-4.6, or stub when "
                             "--model is stub; same spec syntax as --model)")
    parser.add_argument("--no-attacker", action="store_true",
                        help="disable the attacker stage")
    parser.add_argument("--no-inner-sandbox", action="store_true",
                        help="disable the inner sandbox layer (testing only)")


def build_attacker(args, model_name: str):
    """The attacker for fix tasks, or None with --no-attacker.
    Raises BackendError on a bad spec / missing key."""
    import os
    if getattr(args, "no_attacker", False):
        return None
    from lumen2.attacker import from_spec
    spec = (args.attacker_model or os.environ.get("LUMEN2_ATTACKER_MODEL")
            or ("stub" if model_name == "stub" else "xai:grok-4.6"))
    return from_spec(spec, base_url=None,
                     api_key_env=args.api_key_env if spec.startswith(
                         model_name.split(":")[0] + ":") else None)


def build_bucket(args, model_name: str):
    from lumen2.bucket import BoundBucket
    return BoundBucket.from_file(args.bucket, model=model_name,
                                 container=args.container)


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

    p_chat = sub.add_parser("chat", help="interactive chat (default model "
                                         "xai:grok-4.6)")
    p_chat.add_argument("--project", default=None, metavar="DIR",
                        help="directory chat may change (via the fixer)")
    p_chat.add_argument("--max-steps", type=int, default=10,
                        help="steps per question")
    p_chat.add_argument("--max-fix-steps", type=int, default=30,
                        help="steps per code-change turn")
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

    if args.command == "chat" and not args.model:
        import os
        args.model = os.environ.get("LUMEN2_MODEL") or "xai:grok-4.6"
    try:
        backend = from_spec(args.model, base_url=args.base_url,
                           api_key_env=args.api_key_env)
    except BackendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    memory = Memory(args.memory) if args.memory else None
    try:
        attacker = (build_attacker(args, backend.name)
                    if args.command in ("fix", "chat") else None)
    except BackendError as exc:
        print(f"error: attacker: {exc} (or pass --no-attacker)", file=sys.stderr)
        return 2

    if args.command == "fix":
        report = run_fix(args.target, backend, memory=memory,
                         max_steps=args.max_steps,
                         test_timeout=args.test_timeout, verbose=args.verbose,
                         bucket=build_bucket(args, backend.name),
                         inner_enabled=not args.no_inner_sandbox,
                         attempt_memory=not args.no_attempt_memory,
                         attacker=attacker, announce=print)
        _emit(report, args.out)
        print(_fix_banner(report))
        return 0 if report["outcome"] == "committed" else 1

    if args.command == "ask":
        report = run_ask(args.question, backend, memory=memory,
                         max_steps=args.max_steps, verbose=args.verbose,
                         bucket=build_bucket(args, backend.name),
                         inner_enabled=not args.no_inner_sandbox,
                         announce=print)
        _emit(report, args.out)
        print(_ask_banner(report))
        return 0

    if args.command == "chat":
        from lumen2.chat import ChatSession, chat_loop
        session = ChatSession(
            backend, project=args.project,
            bucket_factory=lambda: build_bucket(args, backend.name),
            memory=memory, max_steps_ask=args.max_steps,
            max_steps_fix=args.max_fix_steps,
            inner_enabled=not args.no_inner_sandbox, verbose=args.verbose,
            attacker=attacker)
        return chat_loop(session)
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
    breaches = stats.get("bucket_breaches", 0)
    if report["outcome"] == "committed":
        head = (f"FIXED: {report.get('verification', '')} — changed "
                f"{', '.join(report.get('files_changed', []))}")
    else:
        head = f"WITHHELD: {report.get('reason', '')}"
    return (f"{head}\n(steps={report['steps']} tokens={report['tokens']} "
            f"time={report['seconds']}s cheats_caught={cheats} "
            f"bucket_breaches={breaches} "
            f"attacker_blocks={stats.get('attacker_blocked', 0)} "
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
