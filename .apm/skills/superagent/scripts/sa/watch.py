"""Watchdog tick, stall/hang classification and forensic snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adaptors import ADAPTORS
from .ade import ensure_watcher, notify, probe_worker
from .board import cli_cmd, post_message
from .constants import DEFAULT_WATCHDOG, ROUTER, WATCHER
from .store import Repo, atomic_write_json, emit, iso_str, parse_iso, utc_now


def check_report_marker(report_path: Path, expected_marker: str | None) -> tuple[bool, str | None]:
    if not expected_marker or not report_path.is_file():
        return False, None
    text = report_path.read_text(encoding="utf-8", errors="replace")
    markers = [m.strip() for m in expected_marker.split("|") if m.strip()]
    for ln in reversed([ln.strip() for ln in text.splitlines() if ln.strip()][-20:]):
        if ln in markers:
            return True, ln
    return False, None



def collect_logs_snapshot(watch_logs: list[str], stage_logs_dir: Path | None) -> tuple[list[dict[str, Any]], str]:
    targets: list[Path] = []
    seen: set[str] = set()
    for p in watch_logs:
        if p not in seen:
            seen.add(p)
            targets.append(Path(p))
    if stage_logs_dir and stage_logs_dir.exists():
        for lf in sorted(stage_logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]:
            if str(lf) not in seen:
                seen.add(str(lf))
                targets.append(lf)
    entries, sig_parts = [], []
    for path in targets:
        if path.is_file():
            st = path.stat()
            entries.append({"path": str(path), "exists": True, "size": st.st_size,
                            "mtime": iso_str(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc))})
            sig_parts.append(f"{path}:{int(st.st_mtime)}:{st.st_size}")
        else:
            entries.append({"path": str(path), "exists": False, "size": 0, "mtime": None})
            sig_parts.append(f"{path}:missing")
    return entries, hashlib.sha256("|".join(sorted(sig_parts)).encode("utf-8")).hexdigest()[:16]



def tail_file(path: Path, n: int = 20) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:] if path.is_file() else []
    except OSError:
        return []



def _alert(w: dict[str, Any], sig: str, now: datetime, wd: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Returns (should_notify, muted, suppressed) and updates the worker's alert counters."""
    if w.get("last_alert_sig") != sig:
        w["alert_count"], w["last_alert_at"] = 0, None
    last = parse_iso(w.get("last_alert_at"))
    since_last = int((now - last).total_seconds()) if last else int(wd["repeat_cooldown_sec"]) + 1
    muted = int(w.get("alert_count", 0)) >= int(wd["max_alerts"])
    suppressed = not muted and since_last < int(wd["repeat_cooldown_sec"])
    if not muted and not suppressed:
        w["alert_count"] = int(w.get("alert_count", 0)) + 1
        w["last_alert_sig"], w["last_alert_at"] = sig, iso_str(now)
        return True, muted, suppressed
    return False, muted, suppressed



def watch_tick(repo: Repo, args: argparse.Namespace) -> dict[str, Any]:
    cfg = repo.load_config()
    wd = {**DEFAULT_WATCHDOG, **(cfg.get("watchdog") or {})}
    now = utc_now(getattr(args, "now", None))
    now_iso = iso_str(now)
    mock = json.loads(Path(args.mock_probe).read_text(encoding="utf-8")) if getattr(args, "mock_probe", None) else None
    events: list[dict[str, Any]] = []
    marker_posts: list[tuple[str, str]] = []
    nudges: list[tuple[dict[str, Any], str]] = []

    with repo.lock():
        members = repo.load_members()
        workers = members.get("workers") or {}
        for w in [w for w in workers.values() if w.get("status") == "active"]:
            wid, stage = w["id"], w["stage"]
            found, line = check_report_marker(Path(w["report_path"]), w.get("expected_marker"))
            if found:
                # The worker finished but skipped `post`: report for it so the Router's single inbox stays the board.
                marker_posts.append((wid, f"(watch) 報告已有完成標記 `{line}`，但 worker 沒有 post。報告：{w['report_path']}"))
                events.append({"worker_id": wid, "event": "MARKER_WITHOUT_POST", "tier": 0, "should_notify": True})
                continue
            if w.get("ade") in ADAPTORS and not ADAPTORS[w["ade"]].can_probe:
                continue  # nothing to probe: the harness reports a native subagent's end to the Router itself
            probe = probe_worker(w, mock)
            logs, log_sig = collect_logs_snapshot(w.get("watch_logs", []), repo.stage_dir(stage) / "logs")
            if not probe.get("backend_ok"):
                ok, muted, supp = _alert(w, f"{wid}:PROBE_ERROR", now, wd)
                events.append({"worker_id": wid, "event": "PROBE_ERROR", "tier": 2, "should_notify": ok, "error": probe.get("error")})
                continue
            prev = w.get("last_probe")
            cpu = float(probe["subtree_cpu_sec"])
            progressed = prev is None or probe["screen_hash"] != prev.get("screen_hash") or log_sig != prev.get("log_sig") \
                or cpu - float(prev.get("subtree_cpu_sec", 0.0)) >= float(wd["cpu_delta_epsilon_sec"])
            if progressed:
                w.update(last_progress_at=now_iso, nudged_at=None, alert_count=0, last_alert_sig=None, last_alert_at=None)
            interactive = probe.get("interactive_match")
            w["prompt_seen_since"] = (w.get("prompt_seen_since") or now_iso) if interactive else None
            w["last_probe"] = {"checked_at": now_iso, "backend_state": probe["backend_state"], "agent_status": probe["agent_status"],
                               "screen_hash": probe["screen_hash"], "log_sig": log_sig, "subtree_cpu_sec": cpu, "pid": probe.get("pid")}
            stalled = max(0, int((now - (parse_iso(w.get("last_progress_at")) or now)).total_seconds()))
            prompt_sec = max(0, int((now - parse_iso(w["prompt_seen_since"])).total_seconds())) if w.get("prompt_seen_since") else 0
            idle = probe["agent_status"] in ("done", "idle", "waiting")
            event, tier, extra = "HEALTHY", 0, {}
            if probe["backend_state"] == "finished":
                event, tier, extra = "WORKER_EXITED_NO_POST", 2, {"exit_code": probe.get("exit_code")}
            elif interactive and prompt_sec >= int(wd["prompt_stall_sec"]):
                event, tier, extra = "INTERACTIVE_BLOCK", 2, {"interactive_match": interactive, "prompt_stalled_sec": prompt_sec}
            elif idle and stalled >= int(wd["state_stall_sec"]):
                if not w.get("nudged_at") and args.auto_nudge:
                    w["nudged_at"] = now_iso
                    nudges.append((w, (f"[superagent watch] 你停下了，但還沒在 board 回報。完成了就執行：\n"
                                       f"{cli_cmd('post', repo, '--as', wid, '--to', ROUTER, '--kind', 'done', '--file', w['report_path'])}\n"
                                       f"卡住就用 --kind ask 說明；有新訊息先執行 {cli_cmd('read', repo, '--as', wid)}")))
                    event, tier = "AUTO_NUDGED", 1
                else:
                    event, tier, extra = "STOPPED_NO_POST", 1, {"already_nudged": bool(w.get("nudged_at"))}
            elif not idle and stalled >= int(wd["silent_hang_sec"]):
                event, tier = "SILENT_HANG", 3
            if event in ("HEALTHY", "AUTO_NUDGED"):
                events.append({"worker_id": wid, "event": event, "tier": tier, "should_notify": False, "stalled_seconds": stalled})
                continue
            ok, muted, supp = _alert(w, f"{wid}:{event}:{probe['screen_hash'][:12]}", now, wd)
            incident = None
            if ok:
                incident = repo.stage_dir(stage) / "watchdog" / f"{wid}_incident_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
                atomic_write_json(incident, {"timestamp": now_iso, "worker_id": wid, "stage": stage, "incident_type": event,
                                             "tier": tier, "alert_number": w["alert_count"], "stalled_seconds": stalled,
                                             "ade": w.get("ade"), "backend_ref": w.get("backend_ref"),
                                             "backend_status": {k: probe.get(k) for k in ("backend_state", "agent_status", "status_msg", "exit_code")},
                                             "screen_tail": probe["screen_lines"][-40:],
                                             "watched_logs": [{**le, "tail": tail_file(Path(le["path"]), 20)} for le in logs],
                                             "process_tree": probe["process_tree"], "extra": extra})
                w["last_incident_path"] = str(incident)
            events.append({"worker_id": wid, "event": event, "tier": tier, "should_notify": ok, "muted": muted,
                           "suppressed_by_cooldown": supp, "stalled_seconds": stalled,
                           "incident_path": str(incident) if incident else None, **extra})

        # A post for the Router that it has not read: push again once per seq (swarm monitor's "went to nobody").
        router = members.get("router") or {}
        posts = repo.read_board()
        pending = [p for p in posts if ROUTER in p.get("to", []) and p["seq"] > int(router.get("last_read_seq", 0))]
        # watch's own alerts are excluded, or each reminder would become the next unread post to remind about.
        stale = [p for p in pending if p["from"] != WATCHER
                 and (now - (parse_iso(p["time"]) or now)).total_seconds() >= int(wd["state_stall_sec"])]
        if stale and stale[-1]["seq"] > int(router.get("last_alerted_seq", 0)):
            router["last_alerted_seq"] = stale[-1]["seq"]
            events.append({"worker_id": ROUTER, "event": "ROUTER_UNREAD", "tier": 1, "should_notify": True,
                           "unread_seqs": [p["seq"] for p in pending]})
        repo.save_members(members)
        active = sum(1 for w in workers.values() if w.get("status") == "active")
        state = repo.load_state()
        state["idle_ticks"] = 0 if active else int(state.get("idle_ticks", 0)) + 1
        repo.save_state(state)

    for w, msg in nudges:
        res = notify(w.get("ade"), w.get("backend_ref"), msg, dry_run=bool(mock))
        for e in events:
            if e["worker_id"] == w["id"] and e["event"] == "AUTO_NUDGED":
                e["nudge_result"] = res
    for wid, text in marker_posts:
        post_message(repo, wid, [ROUTER], "done", text, extra={"by": WATCHER}, dry_run=bool(mock))
    alerts = [e for e in events if e.get("should_notify") and e["event"] != "MARKER_WITHOUT_POST"]
    if alerts:
        body = "；".join(f"{e['worker_id']}:{e['event']}" + (f" {e['incident_path']}" if e.get("incident_path") else "") for e in alerts)
        post_message(repo, WATCHER, [ROUTER], "alert", f"巡檢警報：{body}", dry_run=bool(mock))
    return {"checked_at": now_iso, "active_workers": active, "idle_ticks": state["idle_ticks"],
            "should_sleep": active == 0 and state["idle_ticks"] >= int(wd["dormant_idle_ticks"]),
            "notify_count": len([e for e in events if e.get("should_notify")]), "events": events}



def cmd_watch(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    while True:
        result = watch_tick(repo, args)
        lines = [f"Watch tick @ {result['checked_at']}: active={result['active_workers']}, alerts={result['notify_count']}, "
                 f"should_sleep={result['should_sleep']}"]
        lines += [f"  - [{e['event']}] {e['worker_id']} tier={e['tier']}" for e in result["events"] if e.get("should_notify")]
        if not args.loop or result["should_sleep"]:
            return emit(args, result, lines)
        print("\n".join(lines), flush=True)
        cfg = repo.load_config()
        time.sleep(int({**DEFAULT_WATCHDOG, **(cfg.get("watchdog") or {})}["poll_interval_sec"]))



def cmd_watch_start(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    res = ensure_watcher(repo, dry_run=args.dry_run)
    return emit(args, res, [f"Watcher: {res.get('reason') or ('started ' + str(res.get('name')))}"], exit_code=0)



def cmd_diagnose(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    w = (repo.load_members().get("workers") or {}).get(args.worker)
    if not w:
        print(f"Error: worker '{args.worker}' not found", file=sys.stderr)
        return 1
    probe = probe_worker(w)
    logs, log_sig = collect_logs_snapshot(w.get("watch_logs", []), repo.stage_dir(w["stage"]) / "logs")
    payload = {"timestamp": iso_str(utc_now()), "worker": w, "probe": probe, "log_sig": log_sig,
               "watched_logs": [{**le, "tail": tail_file(Path(le["path"]), 25)} for le in logs]}
    return emit(args, payload, [f"Diagnosed '{w['id']}' (ade={w.get('ade')}, ref={w.get('backend_ref')}, ok={probe.get('backend_ok')}).",
                                f"Process tree nodes: {len(probe.get('process_tree', []))}, CPU={probe.get('subtree_cpu_sec', 0.0)}s"])
