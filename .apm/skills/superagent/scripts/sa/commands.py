"""Member-facing subcommands: init, dispatch, post, read, status, set-role, close-worker."""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .adaptors import ADAPTORS, LaunchRequest, get_adaptor
from .ade import detect_router, ensure_watcher
from .board import post_message, worker_instructions
from .constants import DEFAULT_KINDS, DEFAULT_MAX_STAGE_RESETS, DEFAULT_WATCHDOG, ROLE_NAME_RE, ROUTER
from .hooks import write_claude_settings
from .store import Repo, atomic_write_json, emit, ensure_home_ignored, hc_name, iso_str, run_cmd, utc_now


def cmd_init(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    ignore = ensure_home_ignored(repo.home)
    if repo.config.exists():
        cfg = repo.load_config()
        config_status = "existing"
    elif not args.use_defaults:
        # The script cannot ask a person: hand the defaults to the Router, which asks the Owner and reruns.
        payload = {"initialized": False, "error_code": "CONFIG_MISSING", "config_path": str(repo.config),
                   "defaults_preview": {"kinds": DEFAULT_KINDS, "max_review_rounds": 3,
                                        "max_stage_resets": DEFAULT_MAX_STAGE_RESETS}}
        return emit(args, payload, [f"No config at {repo.config}. Ask the Owner to confirm the write/review kind defaults (ade/cli/model/effort), "
                                    "or rerun with --use-defaults; roles are created per task with `set-role`."], exit_code=3)
    else:
        cfg = {"version": 3, "repo_key": repo.key, "project_root": str(repo.project), "max_review_rounds": 3,
               "max_stage_resets": DEFAULT_MAX_STAGE_RESETS, "herdr_space": "router",
               "watchdog": dict(DEFAULT_WATCHDOG), "kinds": json.loads(json.dumps(DEFAULT_KINDS)), "roles": {}}
        config_status = "created_defaults"
    with repo.lock():
        if config_status == "created_defaults":
            atomic_write_json(repo.config, cfg)
        members = repo.load_members()
        # Re-running init re-attaches the Router: its terminal id changes after a restart.
        router = {**(members.get("router") or {"last_read_seq": 0, "last_notified_seq": 0, "last_alerted_seq": 0}),
                  **detect_router(), "attached_at": iso_str(utc_now())}
        members["router"] = router
        repo.save_members(members)
        if not repo.state.exists():
            repo.save_state(repo.load_state())
    repo.stages.mkdir(parents=True, exist_ok=True)
    unpinned = sorted(r for r, v in (cfg.get("roles") or {}).items()
                      if v.get("ade") in ADAPTORS and ADAPTORS[v["ade"]].needs_pinned_model
                      and not (v.get("model") and v.get("effort")))
    tools = {k: v for k, v in {**{n: a.bin() for n, a in ADAPTORS.items() if a.bin()}, "claude": shutil.which("claude"),
                               "codex": shutil.which("codex"), "agy": shutil.which("agy"), "uv": shutil.which("uv")}.items()}
    payload = {"initialized": True, "config_status": config_status, "repo_key": repo.key, "dir": str(repo.dir),
               "router": router, "gitignore": ignore, "unpinned_roles": unpinned, "detected_tools": tools, "config": cfg}
    summary = [f"superagent '{repo.key}': config {config_status} at {repo.dir}",
               f"Router endpoint: ade={router['ade']} ref={router.get('ref')}"
               + (" (no push: schedule `watch` and run `read --as router` yourself)" if router["ade"] == "none" else "")]
    if ignore.get("added"):
        summary.append(f"Added .superagent/ to {ignore['gitignore']}")
    if unpinned:
        summary.append(f"Unpinned roles (need model+effort via set-role): {', '.join(unpinned)}")
    return emit(args, payload, summary)



def cmd_dispatch(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    cfg = repo.load_config()
    role_cfg = (cfg.get("roles") or {}).get(args.role)
    if role_cfg is None:
        # Launch settings come from config only, never from what the Router remembers.
        return emit(args, {"ok": False, "error_code": "ROLE_UNDEFINED", "role": args.role,
                           "defined_roles": sorted(cfg.get("roles") or {})},
                    [f"Error: role '{args.role}' is not in config; create it with "
                     f"`set-role --role {args.role} --kind write|review` first."], exit_code=1)
    ade = args.ade or role_cfg.get("ade")
    cli = args.cli or role_cfg.get("cli")
    model = args.model if args.model is not None else role_cfg.get("model")
    effort = args.effort if args.effort is not None else role_cfg.get("effort")

    state = repo.load_state()
    stage_rec = (state.get("stages") or {}).get(args.stage)
    if not stage_rec:
        print(f"Error: stage '{args.stage}' not found; run stage-new first", file=sys.stderr)
        return 1
    if str(stage_rec.get("status", "")).startswith("halted"):
        return emit(args, {"ok": False, "error_code": "STAGE_HALTED", "stage": args.stage, "halt": stage_rec.get("halt")},
                    [f"Error: stage '{args.stage}' is halted; Owner must rule via `reset-stage --owner-ack`."], exit_code=1)
    try:
        adaptor = get_adaptor(ade)
    except ValueError as exc:
        return emit(args, {"ok": False, "error_code": "ADE_UNKNOWN", "ade": ade}, [f"Error: {exc}"], exit_code=1)
    unpinned = [k for k, v in (("model", model), ("effort", effort)) if not v]
    if adaptor.needs_pinned_model and unpinned and not args.allow_cli_default:
        return emit(args, {"ok": False, "error_code": "ROLE_UNPINNED", "role": args.role, "missing": unpinned},
                    [f"Error: role '{args.role}' has no {'/'.join(unpinned)}; run `set-role` or pass --allow-cli-default."],
                    exit_code=1)
    invalid = adaptor.validate(model, effort)
    if invalid:
        return emit(args, {"ok": False, "error_code": invalid, "ade": ade}, [f"Error: {invalid} ({ade})"], exit_code=1)

    sdir = repo.stage_dir(args.stage)
    brief = Path(args.brief).resolve() if args.brief else Path(stage_rec["brief_path"])
    report = Path(args.report).resolve() if args.report else sdir / f"report-{args.role}.md"
    if not brief.exists() and not args.dry_run:
        print(f"Error: brief file does not exist: {brief}", file=sys.stderr)
        return 1
    wid = hc_name(args.name or f"{args.role}-{args.stage}")
    members = repo.load_members()
    if wid in (members.get("workers") or {}) and members["workers"][wid].get("status") in ("active", "ready"):
        print(f"Error: worker '{wid}' is still {members['workers'][wid]['status']}; close it or pass --name", file=sys.stderr)
        return 1

    workdir = Path(args.worktree).resolve() if args.worktree else repo.project
    if args.create_worktree_branch and not args.dry_run and not adaptor.creates_worktree:
        if not args.worktree:
            print("Error: --create-worktree-branch requires --worktree <dir>", file=sys.stderr)
            return 1
        wt = run_cmd(["git", "-C", str(repo.project), "worktree", "add", "-b", args.create_worktree_branch,
                      str(workdir), args.base or "HEAD"], timeout=30.0)
        if not wt["ok"]:
            return emit(args, {"ok": False, "error_code": "WORKTREE_ERROR", "stderr": wt["stderr"]},
                        [f"Failed to create worktree: {wt['stderr'].strip()}"], exit_code=1)

    instructions = worker_instructions(repo, wid, args.role, brief, report)
    prompt_file = sdir / f"{wid}.prompt.md"
    if not args.dry_run:
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(instructions, encoding="utf-8")
    # Only claude takes a per-run Stop hook (codex needs a trusted hooks.json, agy has none); others rely on watch.
    stop_hook = adaptor.passes_cli_args and cli == "claude"
    settings_file = sdir / f"{wid}.settings.json" if stop_hook else None
    if settings_file and not args.dry_run:
        write_claude_settings(repo, wid, settings_file)
    space = cfg.get("herdr_space")
    if space == "router":
        space = (members.get("router") or {}).get("herdr_space")
    plan = adaptor.launch(LaunchRequest(wid=wid, role=args.role, stage=args.stage, workdir=workdir, prompt_file=prompt_file,
                                        instructions=instructions, cli=cli, model=model, effort=effort,
                                        extra_dirs=[repo.dir], herdr_space=space, branch=args.create_worktree_branch,
                                        settings_file=settings_file))
    command, native_spawn = plan.command, plan.native_spawn

    if args.dry_run:
        return emit(args, {"ok": True, "dry_run": True, "worker_id": wid, "ade": ade, "cli": cli, "model": model,
                           "effort": effort, "workdir": str(workdir), "command": command, "native_spawn": native_spawn,
                           "instructions": instructions}, [f"[dry-run] Would dispatch {wid} via {ade} ({cli})."])

    backend_ref = None
    if command:
        res = run_cmd(command, cwd=workdir, timeout=plan.timeout)
        if not res["ok"]:
            return emit(args, {"ok": False, "error_code": "BACKEND_ERROR", "command": command, "stderr": res["stderr"],
                               "stdout": res["stdout"]}, [f"Dispatch failed: {res['stderr'].strip() or res['stdout'].strip()}"],
                        exit_code=1)
        backend_ref = adaptor.parse_ref(res["stdout"], wid)

    now_iso = iso_str(utc_now())
    record = {"id": wid, "stage": args.stage, "role": args.role, "kind": role_cfg.get("kind"), "stop_hook": stop_hook, "ade": ade, "cli": cli, "model": model, "effort": effort,
              "backend_ref": backend_ref, "workdir": str(workdir), "brief_path": str(brief), "report_path": str(report),
              "prompt_path": str(prompt_file), "expected_marker": args.marker, "watch_logs": [str(Path(p).resolve()) for p in (args.watch_log or [])],
              "status": "active", "started_at": now_iso, "last_progress_at": now_iso, "last_read_seq": 0,
              "last_notified_seq": 0, "prompt_seen_since": None, "nudged_at": None, "last_probe": None,
              "alert_count": 0, "last_alert_sig": None, "last_alert_at": None, "last_incident_path": None}
    with repo.lock():
        members = repo.load_members()
        members.setdefault("workers", {})[wid] = record
        repo.save_members(members)
        state = repo.load_state()
        state["idle_ticks"] = 0
        state["stages"][args.stage]["status"] = f"running:{args.role}"
        repo.save_state(state)
    # The worker already has its instructions; this post is the board's record of the hand-off, so no push.
    brief_post = post_message(repo, ROUTER, [wid], "brief", f"{args.role} 交辦：{brief}（報告：{report}）", push=False)
    # The worker's cursor starts past its own brief, as in swarm.
    with repo.lock():
        members = repo.load_members()
        members["workers"][wid]["last_read_seq"] = brief_post["post"]["seq"]
        repo.save_members(members)
    watcher = ensure_watcher(repo) if adaptor.can_probe else {"started": False, "reason": f"{ade} worker is not probed"}
    summary = [f"Dispatched '{wid}' via {ade} (ref={backend_ref}, cli={cli}).", f"Prompt: {prompt_file}",
               f"Watcher: {watcher.get('reason') or ('started ' + str(watcher.get('name')))}"]
    if native_spawn:
        summary.append("native: spawn it now with your subagent tool using native_spawn.prompt from the output.")
    return emit(args, {"ok": True, "worker": record, "command": command, "native_spawn": native_spawn, "watcher": watcher}, summary)



def cmd_post(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.text or "")
    if not text.strip():
        print("Error: empty post; give --text or --file", file=sys.stderr)
        return 1
    to = [t.strip() for t in args.to.split(",") if t.strip()]
    extra = {"file": str(Path(args.file).resolve())} if args.file else None
    res = post_message(repo, args.as_, to, args.kind, text, extra=extra, push=not args.no_push, dry_run=args.dry_run)
    post = res["post"]
    lines = [f"Posted #{post['seq']} {post['from']} → {','.join(post['to'])} ({post['kind']})."]
    for name, r in res["notified"].items():
        lines.append(f"  notify {name}: {'sent' if r.get('sent') else 'not sent (' + str(r.get('reason') or r.get('stderr')) + ')'}")
    return emit(args, res, lines)



def cmd_read(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    with repo.lock():
        members = repo.load_members()
        rec = members.get("router") if args.as_ == ROUTER else (members.get("workers") or {}).get(args.as_)
        if rec is None:
            print(f"Error: unknown member '{args.as_}'", file=sys.stderr)
            return 1
        posts = repo.read_board()
        since = args.since if args.since is not None else int(rec.get("last_read_seq", 0))
        # The Router sees every post, like the swarm coordinator; a worker sees what names it.
        mine = [p for p in posts if p["seq"] > since and (args.as_ == ROUTER or args.all or args.as_ in p.get("to", []))]
        head = posts[-1]["seq"] if posts else 0
        if not args.peek:
            rec["last_read_seq"] = max(int(rec.get("last_read_seq", 0)), head)
            repo.save_members(members)
    lines = [f"board head #{head}; {len(mine)} post(s) for {args.as_} after #{since}."]
    for p in mine:
        lines.append(f"\n#{p['seq']} [{p['kind']}] {p['from']} → {','.join(p['to'])} {p['time']}")
        lines.append(p["text"].rstrip())
    return emit(args, {"head": head, "since": since, "posts": mine}, lines)



def cmd_status(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    cfg = repo.load_config()
    members, state, posts = repo.load_members(), repo.load_state(), repo.read_board()
    router = members.get("router") or {}
    workers = list((members.get("workers") or {}).values())
    counts = {s: sum(1 for w in workers if w.get("status") == s) for s in ("active", "ready", "closed")}
    unread = [p["seq"] for p in posts if ROUTER in p.get("to", []) and p["seq"] > int(router.get("last_read_seq", 0))]
    payload = {"repo_key": repo.key, "dir": str(repo.dir), "active_stage": state.get("active_stage"),
               "router": router, "board_head": posts[-1]["seq"] if posts else 0, "router_unread": unread,
               "roles": cfg.get("roles"), "worker_counts": counts, "stages": state.get("stages", {}),
               "workers": [{k: w.get(k) for k in ("id", "stage", "role", "ade", "cli", "backend_ref", "status",
                                                  "last_post_seq", "last_progress_at", "alert_count", "last_incident_path")}
                           for w in workers]}
    return emit(args, payload, [f"'{repo.key}' | stage={payload['active_stage']} | board #{payload['board_head']} | "
                                f"router unread={unread or 'none'}",
                                f"Workers: active={counts['active']}, ready={counts['ready']}, closed={counts['closed']}"])



def cmd_set_role(args: argparse.Namespace) -> int:
    """Create or update a role; a new role is only proposed until the Owner confirms it."""
    repo = Repo(args.project)
    if not ROLE_NAME_RE.fullmatch(args.role):
        print(f"Error: role name '{args.role}' must match {ROLE_NAME_RE.pattern}", file=sys.stderr)
        return 1
    with repo.lock():
        cfg = repo.load_config()
        roles = cfg.setdefault("roles", {})
        cur = roles.get(args.role)
        kind = args.kind or (cur or {}).get("kind")
        if not kind:
            print(f"Error: new role '{args.role}' needs --kind write|review", file=sys.stderr)
            return 1
        base = {**DEFAULT_KINDS.get(kind, {}), **(cfg.get("kinds") or {}).get(kind, {}), **(cur or {})}
        given = {"ade": args.ade, "cli": args.cli, "model": args.model, "effort": args.effort}
        entry = {"kind": kind, **{k: (v if v is not None else base.get(k)) for k, v in given.items()}}
        if args.clear_model:
            entry["model"] = None
        if args.purpose is not None or (cur or {}).get("purpose"):
            entry["purpose"] = args.purpose if args.purpose is not None else cur["purpose"]
        inherited = sorted(k for k, v in given.items() if v is None and cur is None)
        if cur is None and not args.confirm:
            # The script cannot ask a person: return the proposal so the Router asks the Owner, then reruns with --confirm.
            return emit(args, {"ok": False, "error_code": "ROLE_CONFIRM_REQUIRED", "role": args.role, "proposal": entry,
                               "from_kind_defaults": inherited},
                        [f"Proposed role '{args.role}' ({kind}): ade={entry['ade']} cli={entry['cli']} "
                         f"model={entry['model']} effort={entry['effort']}"
                         + (f" (suggested from '{kind}' defaults: {', '.join(inherited)})" if inherited else ""),
                         "Ask the Owner to accept or change these values, then rerun with the agreed values and --confirm."],
                        exit_code=3)
        roles[args.role] = entry
        atomic_write_json(repo.config, cfg)
    action = "Created" if cur is None else "Updated"
    lines = [f"{action} role '{args.role}' ({kind}) -> ade={entry['ade']} cli={entry['cli']} "
             f"model={entry['model']} effort={entry['effort']}"]
    return emit(args, {"ok": True, "role": args.role, "created": cur is None, "assignment": entry,
                       "inherited_from_kind": inherited}, lines)


def cmd_close_worker(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    w = (repo.load_members().get("workers") or {}).get(args.worker)
    if not w:
        print(f"Error: worker '{args.worker}' not found", file=sys.stderr)
        return 1
    cleanup: dict[str, Any] = {}
    if not args.skip_backend and w.get("backend_ref"):
        with contextlib.suppress(ValueError):
            cleanup = get_adaptor(w.get("ade")).stop(w["backend_ref"])
    if args.remove_worktree:
        wd = Path(w["workdir"]).resolve()
        if wd != repo.project and wd.exists():
            cleanup["worktree_remove"] = run_cmd(["git", "-C", str(repo.project), "worktree", "remove", "--force", str(wd)], timeout=15.0)
    with repo.lock():
        members = repo.load_members()
        rec = members["workers"][args.worker]
        rec["status"], rec["closed_at"] = "closed", iso_str(utc_now())
        repo.save_members(members)
    return emit(args, {"ok": True, "worker_id": args.worker, "cleanup": cleanup}, [f"Closed worker '{args.worker}'."])
