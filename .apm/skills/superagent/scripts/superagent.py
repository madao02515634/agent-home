#!/usr/bin/env python3
"""Superagent: board-based Router-Worker orchestration over herdr-core, Orca or native subagents.

Stdlib-only Python 3. Always invoke via `uv run`:
    uv run <skill-dir>/scripts/superagent.py <subcommand> --project <repo> ...

All state lives in <superagent home>/.superagent/<owner>-<repo>/ (home = the git
repo holding this script, or $SUPERAGENT_HOME), never inside the target project.
"""


from __future__ import annotations

import argparse
import sys
from typing import Any

from sa.adaptors import ADAPTORS
from sa.commands import cmd_close_worker, cmd_dispatch, cmd_init, cmd_post, cmd_read, cmd_set_role, cmd_status
from sa.constants import CLI_CHOICES, EFFORT_CHOICES, POST_KINDS, ROLE_KINDS
from sa.hooks import cmd_hook_stop
from sa.stage import cmd_reset_stage, cmd_stage_new, cmd_verify_gate
from sa.watch import cmd_diagnose, cmd_watch, cmd_watch_start

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="superagent: board-based Router-Worker orchestration")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, func: Any, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--project", required=True, help="Target project path (its origin decides .superagent/<owner>-<repo>)")
        sp.add_argument("--output", help="Also write the full JSON result here")
        sp.set_defaults(func=func)
        return sp

    sp = add("init", cmd_init, "Create or load .superagent/<repo>/ and attach the Router endpoint")
    sp.add_argument("--use-defaults", action="store_true", help="Create the config from defaults when none exists")

    sp = add("stage-new", cmd_stage_new, "Create a stage folder and brief skeleton")
    sp.add_argument("--stage", required=True)
    sp.add_argument("--goal", required=True)
    sp.add_argument("--base")
    sp.add_argument("--force", action="store_true")

    sp = add("dispatch", cmd_dispatch, "Launch a worker on its role's ADE and register it")
    sp.add_argument("--stage", required=True)
    sp.add_argument("--role", required=True, help="A role defined in config (see set-role)")
    sp.add_argument("--ade", choices=list(ADAPTORS), help="Override the role's ADE")
    sp.add_argument("--cli", choices=CLI_CHOICES)
    sp.add_argument("--model")
    sp.add_argument("--effort", choices=EFFORT_CHOICES)
    sp.add_argument("--allow-cli-default", action="store_true")
    sp.add_argument("--name", help="Worker id (default <role>-<stage>)")
    sp.add_argument("--brief", help="Default: the stage brief.md")
    sp.add_argument("--report", help="Default: <stage>/report-<role>.md")
    sp.add_argument("--marker", help="Fallback completion marker in the report ('|' for alternatives)")
    sp.add_argument("--worktree")
    sp.add_argument("--create-worktree-branch")
    sp.add_argument("--base")
    sp.add_argument("--watch-log", action="append")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("post", cmd_post, "Append a board post and push a notice to each recipient")
    sp.add_argument("--as", dest="as_", required=True, help="Sender: router or a worker id")
    sp.add_argument("--to", required=True, help="Comma-separated: router, worker ids, or all")
    sp.add_argument("--kind", choices=POST_KINDS, default="note")
    sp.add_argument("--text")
    sp.add_argument("--file")
    sp.add_argument("--no-push", action="store_true")
    sp.add_argument("--dry-run", action="store_true", help="Record the post but only simulate notifications")

    sp = add("read", cmd_read, "Show unread posts for a member and advance its cursor")
    sp.add_argument("--as", dest="as_", required=True)
    sp.add_argument("--since", type=int)
    sp.add_argument("--all", action="store_true", help="Workers: include posts not addressed to them")
    sp.add_argument("--peek", action="store_true", help="Do not advance the read cursor")

    sp = add("hook-stop", cmd_hook_stop, "Stop hook for a worker's CLI: block turn end while the board expects a post")
    sp.add_argument("--as", dest="as_", required=True)

    sp = add("watch", cmd_watch, "Run one watchdog tick, or keep ticking with --loop")
    sp.add_argument("--auto-nudge", action="store_true")
    sp.add_argument("--loop", action="store_true", help="Tick every poll_interval_sec until dormant")
    sp.add_argument("--now", help="Override UTC ISO timestamp (testing)")
    sp.add_argument("--mock-probe", help="Mock probe JSON (testing)")

    sp = add("watch-start", cmd_watch_start, "Start the hc background watch loop if the Router is on hc")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("diagnose", cmd_diagnose, "Deep-inspect one worker (screen, logs, process tree)")
    sp.add_argument("--worker", required=True)

    sp = add("verify-gate", cmd_verify_gate, "Check report marker, placeholders, log exit codes and commit")
    sp.add_argument("--stage", required=True)
    sp.add_argument("--report", required=True)
    sp.add_argument("--marker", required=True)
    sp.add_argument("--commit")
    sp.add_argument("--logs-dir")
    sp.add_argument("--allow-empty-logs", action="store_true")

    add("status", cmd_status, "Summarize stages, workers, board head and Router unread")

    sp = add("set-role", cmd_set_role, "Create or update a role; unset fields come from its kind")
    sp.add_argument("--role", required=True, help="Any name, e.g. impl, migration, perf-review")
    sp.add_argument("--kind", choices=ROLE_KINDS, help="Required when creating a role")
    sp.add_argument("--purpose", help="One line on what this role does, for status and the Owner")
    sp.add_argument("--confirm", action="store_true", help="The Owner accepted these values; required to create a role")
    sp.add_argument("--ade", choices=list(ADAPTORS))
    sp.add_argument("--cli", choices=CLI_CHOICES)
    sp.add_argument("--model")
    sp.add_argument("--clear-model", action="store_true")
    sp.add_argument("--effort", choices=EFFORT_CHOICES)

    sp = add("reset-stage", cmd_reset_stage, "Record a from-base redo; halt for the Owner at max_stage_resets")
    sp.add_argument("--stage", required=True)
    sp.add_argument("--reason")
    sp.add_argument("--owner-ack")

    sp = add("close-worker", cmd_close_worker, "Stop a worker on its ADE and mark it closed")
    sp.add_argument("--worker", required=True)
    sp.add_argument("--remove-worktree", action="store_true")
    sp.add_argument("--skip-backend", action="store_true")
    return p



def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
