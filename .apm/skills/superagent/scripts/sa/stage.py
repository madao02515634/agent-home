"""Stage lifecycle: brief skeleton, verify gate and reset stop-loss."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from .constants import DEFAULT_MAX_STAGE_RESETS, PLACEHOLDER_RE
from .store import Repo, emit, iso_str, run_cmd, utc_now
from .watch import check_report_marker


def cmd_stage_new(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    repo.load_config()
    stage = args.stage.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", stage):
        print(f"Error: invalid stage name '{stage}'", file=sys.stderr)
        return 1
    sdir = repo.stage_dir(stage)
    logs = sdir / "logs"
    for d in (sdir, logs, sdir / "watchdog"):
        d.mkdir(parents=True, exist_ok=True)
    base_ref = args.base
    if not base_ref:
        git_res = run_cmd(["git", "-C", str(repo.project), "rev-parse", "--short", "HEAD"], timeout=5.0)
        base_ref = git_res["stdout"].strip() if git_res["ok"] else "HEAD"
    brief = sdir / "brief.md"
    if not brief.exists() or args.force:
        brief.write_text(
            f"# 階段 {stage}：實作 Brief\n\n"
            f"## Mandate（目標與邊界）\n- **目標**：{args.goal}\n- **起點（base）**：`{base_ref}`\n"
            f"- **專案目錄**：`{repo.project}`\n- **日誌目錄**：`{logs}`（每份 `.log` 最後一行必須寫 `exit=<結束碼>`）\n\n"
            f"## 不變量與邊界條件\n- （由 Router / Brief Worker 補齊不變量、允許修改範圍與禁止碰觸範圍）\n\n"
            # One brief serves several roles, so the report path lives in each worker's prompt, not here.
            "## 報告\n- 報告路徑和回報指令寫在各 worker 的 prompt 檔（dispatch 依角色各產生一份）。"
            "報告最後一行單獨寫完成標記，例如 `IMPL DONE`。\n",
            encoding="utf-8")
    with repo.lock():
        state = repo.load_state()
        prior = (state.get("stages") or {}).get(stage) or {}
        if str(prior.get("status", "")).startswith("halted"):
            print(f"Error: stage '{stage}' is halted; Owner must rule via `reset-stage --owner-ack`.", file=sys.stderr)
            return 1
        state["active_stage"] = stage
        state.setdefault("stages", {})[stage] = {
            # Recreating a stage must not clear its reset count, or the total stop-loss can be bypassed.
            **{k: prior[k] for k in ("reset_count", "reset_history") if k in prior},
            "stage": stage, "goal": args.goal, "base": base_ref, "round": 1, "status": "briefing",
            "stage_dir": str(sdir), "logs_dir": str(logs), "brief_path": str(brief),
            "created_at": iso_str(utc_now()),
        }
        repo.save_state(state)
    return emit(args, {"stage": state["stages"][stage]}, [f"Stage '{stage}' ready (base={base_ref}).", f"Brief: {brief}"])



def cmd_verify_gate(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    report = Path(args.report).resolve()
    logs_dir = Path(args.logs_dir).resolve() if args.logs_dir else repo.stage_dir(args.stage) / "logs"
    checks: list[dict[str, Any]] = []
    marker_ok, matched = check_report_marker(report, args.marker)
    checks.append({"name": "report_marker", "pass": marker_ok, "report": str(report), "matched": matched})
    hits = sorted(set(PLACEHOLDER_RE.findall(report.read_text(encoding="utf-8", errors="replace")))) if report.exists() else []
    checks.append({"name": "no_placeholders", "pass": report.exists() and not hits, "hits": hits})
    log_files = sorted(logs_dir.glob("*.log")) if logs_dir.exists() else []
    if not log_files and not args.allow_empty_logs:
        checks.append({"name": "logs_present", "pass": False, "logs_dir": str(logs_dir)})
    else:
        bad, checked = [], []
        for lf in log_files:
            lines = [ln.strip() for ln in lf.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            last = lines[-1] if lines else ""
            m = re.fullmatch(r"exit=(-?\d+)", last)
            is_mut = "mutation" in lf.name.lower()
            if not m:
                bad.append({"file": str(lf), "reason": "missing final exit=<code> line", "last_line": last})
                continue
            code = int(m.group(1))
            good = (code != 0) if is_mut else (code == 0)
            checked.append({"file": lf.name, "exit_code": code, "is_mutation": is_mut, "pass": good})
            if not good:
                bad.append({"file": str(lf), "exit_code": code, "is_mutation": is_mut})
        checks.append({"name": "logs_exit_codes", "pass": not bad, "checked": checked, "failures": bad})
    if args.commit:
        ok = run_cmd(["git", "-C", str(repo.project), "cat-file", "-e", f"{args.commit}^{{commit}}"], timeout=5.0)["ok"]
        checks.append({"name": "commit_exists", "pass": ok, "commit": args.commit})
    all_pass = all(c["pass"] for c in checks)
    lines = [f"Verify-gate for stage '{args.stage}': {'PASS' if all_pass else 'FAIL'}"]
    lines += [f"  - {c['name']}: {'ok' if c['pass'] else 'FAIL'}" for c in checks]
    return emit(args, {"pass": all_pass, "stage": args.stage, "checks": checks}, lines, exit_code=0 if all_pass else 1)



def cmd_reset_stage(args: argparse.Namespace) -> int:
    """Count a from-base redo of a stage; halt instead of redoing once the cap is hit."""
    repo = Repo(args.project)
    cap = int(repo.load_config().get("max_stage_resets", DEFAULT_MAX_STAGE_RESETS))
    now_iso = iso_str(utc_now())
    with repo.lock():
        state = repo.load_state()
        stage = (state.get("stages") or {}).get(args.stage)
        if not stage:
            print(f"Error: stage '{args.stage}' not found", file=sys.stderr)
            return 1
        history = stage.setdefault("reset_history", [])
        halted = str(stage.get("status", "")).startswith("halted")
        if args.owner_ack:
            if not halted:
                print(f"Error: stage '{args.stage}' is not halted", file=sys.stderr)
                return 1
            stage.update(reset_count=0, status="briefing", round=1)
            stage.pop("halt", None)
            history.append({"at": now_iso, "event": "owner_ack", "note": args.owner_ack})
            repo.save_state(state)
            return emit(args, {"ok": True, "stage": args.stage, "resumed": True},
                        [f"Stage '{args.stage}' resumed by Owner ruling; reset budget restored to {cap}."])
        if halted:
            return emit(args, {"ok": False, "error_code": "STAGE_HALTED", "halt": stage.get("halt")},
                        [f"Error: stage '{args.stage}' already halted."], exit_code=1)
        if not args.reason:
            print("Error: --reason is required (the structural brief gap behind the round-3 failure)", file=sys.stderr)
            return 1
        count = int(stage.get("reset_count", 0))
        if count >= cap:
            stage["status"] = "halted:owner_required"
            stage["halt"] = {"at": now_iso, "reset_count": count, "cap": cap, "last_reason": args.reason}
            history.append({"at": now_iso, "event": "halt", "note": args.reason})
            repo.save_state(state)
            return emit(args, {"ok": False, "error_code": "STAGE_HALTED", "halt": stage["halt"], "reset_history": history},
                        [f"HALT: stage '{args.stage}' already reset {count}/{cap} times; escalate to Owner, "
                         "then resume via `reset-stage --owner-ack \"<ruling>\"`."], exit_code=2)
        stage.update(reset_count=count + 1, round=1, status="briefing")
        history.append({"at": now_iso, "event": "reset", "note": args.reason})
        repo.save_state(state)
    return emit(args, {"ok": True, "stage": args.stage, "reset_count": count + 1, "cap": cap, "reset_history": history},
                [f"Stage '{args.stage}' reset {count + 1}/{cap}; revise brief, drop old branch/worktree, redispatch."])
