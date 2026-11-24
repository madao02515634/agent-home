"""The board: posts, push notices and the worker instructions that point at them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ade import notify
from .constants import ROUTER, WATCHER
from .store import SCRIPT, Repo, home_env_argv, iso_str, utc_now


def member_endpoint(members: dict[str, Any], name: str) -> tuple[str | None, str | None]:
    if name == ROUTER:
        r = members.get("router") or {}
        return r.get("ade"), r.get("ref")
    w = (members.get("workers") or {}).get(name) or {}
    return w.get("ade"), w.get("backend_ref")



def cli_cmd(sub: str, repo: Repo, *rest: str) -> str:
    """The exact command line a worker or the Router runs; absolute paths so any cwd works."""
    parts = [*home_env_argv(), "uv", "run", str(SCRIPT), sub, "--project", str(repo.project), *rest]
    return " ".join(_shq(p) for p in parts)



def _shq(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z0-9_./:=@%+,-]+", s) else "'" + s.replace("'", "'\"'\"'") + "'"



def notice_text(repo: Repo, post: dict[str, Any], reader: str) -> str:
    first = (post.get("text") or "").strip().splitlines()[0:1]
    head = (first[0] if first else "")[:140]
    return (f"[superagent {repo.key}] #{post['seq']} {post['from']} → {','.join(post['to'])} ({post['kind']}): {head} "
            f"— 讀全文：{cli_cmd('read', repo, '--as', reader)}")



def post_message(repo: Repo, sender: str, to: list[str], kind: str, text: str, *,
                 extra: dict[str, Any] | None = None, push: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Append one post, then push a short notice to each recipient that has a push channel."""
    with repo.lock():
        members = repo.load_members()
        workers = members.get("workers") or {}
        known = {ROUTER, *workers}
        if "all" in to:
            to = sorted(n for n, w in workers.items() if w.get("status") in ("active", "ready") and n != sender)
        unknown = [n for n in to if n not in known]
        if unknown:
            raise ValueError(f"unknown recipient(s): {', '.join(unknown)}")
        if sender not in known and sender != WATCHER:
            raise ValueError(f"unknown sender: {sender}")
        post = repo.append_board({"from": sender, "to": to, "kind": kind, "text": text,
                                  "time": iso_str(utc_now()), **(extra or {})})
        if sender in workers:
            w = workers[sender]
            w["last_post_seq"] = post["seq"]
            if kind == "done" and w.get("status") == "active":
                w["status"] = "ready"
                w["done_seq"] = post["seq"]
                w["completed_at"] = post["time"]
        repo.save_members(members)
    results = {}
    if push:
        for name in to:
            ade, ref = member_endpoint(members, name)
            results[name] = notify(ade, ref, notice_text(repo, post, name), dry_run=dry_run)
        with repo.lock():
            members = repo.load_members()
            for name, res in results.items():
                if res.get("sent"):
                    rec = members["router"] if name == ROUTER else members["workers"][name]
                    rec["last_notified_seq"] = max(int(rec.get("last_notified_seq", 0)), post["seq"])
            repo.save_members(members)
    return {"post": post, "notified": results}



def worker_instructions(repo: Repo, wid: str, role: str, brief: Path, report: Path) -> str:
    post_done = cli_cmd("post", repo, "--as", wid, "--to", ROUTER, "--kind", "done", "--file", str(report))
    post_ask = cli_cmd("post", repo, "--as", wid, "--to", ROUTER, "--kind", "ask", "--text", "<問題>")
    read = cli_cmd("read", repo, "--as", wid)
    return (
        f"# superagent worker「{wid}」\n\n"
        f"你是 superagent 的 worker，角色 `{role}`，專案 `{repo.project}`。\n\n"
        f"1. 讀 `{brief}` 並照做。\n"
        f"2. 完成後把報告寫到 `{report}`，再執行下面這行回報，只執行一次：\n\n"
        f"   ```\n   {post_done}\n   ```\n\n"
        f"3. 需要 Router 決定才能繼續時，執行下面這行，然後結束回合等待；收到通知後再讀回覆：\n\n"
        f"   ```\n   {post_ask}\n   {read}\n   ```\n\n"
        f"4. 被通知有新訊息時，先執行 `read` 再繼續。不要直接對使用者說話；交接都經過這兩個指令。\n"
    )
