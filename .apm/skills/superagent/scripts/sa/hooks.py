"""Stop hook for workers: block ending a turn while the board expects something from the worker.

agy swarm does this inside the harness; here the worker's CLI calls `hook-stop` at turn end (claude via --settings).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .board import cli_cmd
from .constants import ROUTER
from .store import Repo, atomic_write_json

# Posts that hand the worker work; after one of these the worker owes a post (done or ask) before stopping.
NEEDS_REPLY = ("brief", "answer")


def stop_decision(repo: Repo, wid: str) -> str | None:
    """The reason to block, or None to let the turn end. Caller holds the lock."""
    members = repo.load_members()
    w = (members.get("workers") or {}).get(wid)
    if not w or w.get("status") != "active":
        return None
    posts = repo.read_board()
    head = posts[-1]["seq"] if posts else 0
    last_read, last_post = int(w.get("last_read_seq", 0)), int(w.get("last_post_seq", 0))
    unread = [p["seq"] for p in posts if p["seq"] > last_read and wid in p.get("to", [])]
    owed = [p["seq"] for p in posts if wid in p.get("to", []) and p.get("kind") in NEEDS_REPLY and p["seq"] > last_post]
    if not unread and not owed:
        return None
    # Block once per board state, like swarm's last_park_blocked_seq; a repeat stop falls through to watch.
    key = f"{head}:{last_post}:{last_read}"
    if w.get("last_stop_block_key") == key:
        return None
    w["last_stop_block_key"] = key
    repo.save_members(members)
    if unread:
        return f"[superagent] board 上有給你的新訊息 #{', #'.join(map(str, unread))}，先讀再決定：{cli_cmd('read', repo, '--as', wid)}"
    report = w.get("report_path", "<報告>")
    return ("[superagent] 你還沒在 board 回報。完成了就執行："
            f"{cli_cmd('post', repo, '--as', wid, '--to', ROUTER, '--kind', 'done', '--file', report)}；"
            f"需要 Router 決定才能繼續就用 --kind ask 說明，然後再結束。")


def cmd_hook_stop(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        sys.stdin.read()  # the CLI's hook payload; the decision only needs the board
    # A hook error must never trap the worker: fail open and leave it to watch.
    try:
        repo = Repo(args.project)
        with repo.lock():
            reason = stop_decision(repo, args.as_)
    except Exception as exc:  # noqa: BLE001
        print(f"superagent hook-stop: {exc}", file=sys.stderr)
        return 0
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    return 0


def write_claude_settings(repo: Repo, wid: str, path: Path) -> Path:
    settings: dict[str, Any] = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": cli_cmd("hook-stop", repo, "--as", wid), "timeout": 30}]}]}}
    atomic_write_json(path, settings)
    return path
