# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI entry point.

Subcommands:
    serve     -- run the FastAPI app via uvicorn on 127.0.0.1:HOOK_PORT
    migrate   -- apply / verify schema migrations
    sessions  -- list recent sessions
    inspect   -- render a single session as a tree
    insights  -- cross-session analytics over the last N days
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")


def cmd_serve(args: argparse.Namespace) -> None:
    port = int(os.getenv("HOOK_PORT", "8787"))
    log_level = os.getenv("LOG_LEVEL", "info")
    uvicorn.run(
        "cc_logger.app:app",
        host="127.0.0.1",
        port=port,
        log_level=log_level,
        reload=args.reload,
    )


_MIGRATIONS = [
    ("schema", "001_initial_schema.py"),   # tables + indexes
    ("views", "002_views.py"),             # vw_* analytics views
    ("messages", "003_messages.py"),       # narration table (needed by `inspect`)
]


def cmd_migrate(args: argparse.Namespace) -> None:
    import subprocess

    # By default apply/verify ALL migrations in order. The full set is required:
    # `inspect` and the conformance queries read the `messages` table (003) and
    # the `vw_*` views (002), so running only 001 leaves the headline features
    # broken. `--only` runs a single step; `--views` is kept as a legacy alias.
    if args.only:
        steps = [s for s in _MIGRATIONS if s[0] == args.only]
    elif args.views:
        steps = [s for s in _MIGRATIONS if s[0] == "views"]
    else:
        steps = _MIGRATIONS

    action = "--apply" if args.apply else ("--verify" if args.verify else None)
    for label, script_name in steps:
        script = _ROOT / "migrations" / script_name
        print(f"→ migrate [{label}] {script_name}")
        cmd = [sys.executable, str(script)]
        if action:
            cmd.append(action)
        subprocess.run(cmd, check=True)


def cmd_sessions(args: argparse.Namespace) -> None:
    from . import insights
    insights.run_sessions(args.limit)


def cmd_inspect(args: argparse.Namespace) -> None:
    from . import inspect as inspect_mod
    inspect_mod.run(args.session_id)


def cmd_insights(args: argparse.Namespace) -> None:
    from . import insights
    insights.run_insights(args.days)


def main() -> None:
    parser = argparse.ArgumentParser(prog="cc-logger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the FastAPI service")
    p_serve.add_argument("--reload", action="store_true", help="auto-reload on code change (dev)")
    p_serve.set_defaults(func=cmd_serve)

    p_mig = sub.add_parser("migrate", help="apply/verify schema (all migrations by default)")
    g = p_mig.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    p_mig.add_argument("--only", choices=[s[0] for s in _MIGRATIONS],
                       help="run a single migration step (schema|views|messages)")
    p_mig.add_argument("--views", action="store_true",
                       help="legacy alias for --only views")
    p_mig.set_defaults(func=cmd_migrate)

    p_ses = sub.add_parser("sessions", help="list recent sessions")
    p_ses.add_argument("--limit", type=int, default=20)
    p_ses.set_defaults(func=cmd_sessions)

    p_ins = sub.add_parser("inspect", help="render a session as a tree")
    p_ins.add_argument("session_id")
    p_ins.set_defaults(func=cmd_inspect)

    p_in = sub.add_parser("insights", help="cross-session analytics")
    p_in.add_argument("--days", type=int, default=30)
    p_in.set_defaults(func=cmd_insights)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
