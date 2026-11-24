"""Routes Router detection, notices, probes and the watcher to the adaptor named by a member's `ade`."""

from __future__ import annotations

import hashlib
from typing import Any

from .adaptors import ADAPTORS, ROUTER_ORDER, get_adaptor
from .adaptors.hc_adaptor import HcAdaptor
from .screen import probe_result
from .store import SCRIPT, Repo, default_home, hc_name, home_env_argv, run_cmd


def detect_router() -> dict[str, Any]:
    """Where the Router (whatever harness runs this skill) can be reached by push."""
    for name in ROUTER_ORDER:
        endpoint = ADAPTORS[name].detect_router()
        if endpoint:
            return endpoint
    return {"ade": "none", "ref": None}


def notify(ade: str | None, ref: str | None, text: str, dry_run: bool = False) -> dict[str, Any]:
    adaptor = ADAPTORS.get(ade or "")
    if not adaptor or not adaptor.can_push or not ref:
        return {"sent": False, "reason": f"no push channel (ade={ade})"}
    if dry_run:
        return {"sent": True, "dry_run": True, "ade": ade, "ref": ref, "text": text}
    return {**adaptor.send(ref, text), "ade": ade, "ref": ref}


def probe_worker(worker: dict[str, Any], mock_probes: dict[str, Any] | None = None) -> dict[str, Any]:
    if mock_probes and worker["id"] in mock_probes:
        mp = mock_probes[worker["id"]]
        return probe_result(mp.get("screen_text", ""), backend_state=mp.get("backend_state", "live"),
                            agent_status=mp.get("agent_status", "working"), status_msg=mp.get("status_msg", ""),
                            exit_code=mp.get("exit_code"), pid=mp.get("pid"), process_tree=mp.get("process_tree"))
    try:
        return get_adaptor(worker.get("ade")).probe(worker)
    except ValueError as exc:
        return {"backend_ok": False, "error": str(exc)}


def watcher_name(repo: Repo) -> str:
    # Two homes (SUPERAGENT_HOME) for one repo must not share a watcher, or the second store goes unwatched.
    tag = "" if repo.home == default_home() else "-" + hashlib.sha1(str(repo.home).encode()).hexdigest()[:6]
    return hc_name(f"sa-watch-{repo.key[:32 - 9 - len(tag)]}{tag}")


def ensure_watcher(repo: Repo, dry_run: bool = False) -> dict[str, Any]:
    """Run watch as an hc background loop so stalls need no LLM cron; alerts reach the Router over its own ADE."""
    hc = ADAPTORS["hc"]
    assert isinstance(hc, HcAdaptor)
    if not hc.bin() and not dry_run:
        return {"started": False, "reason": "hc not installed; schedule `watch` with the harness scheduler"}
    name = watcher_name(repo)
    if not dry_run and hc.is_live(name):
        return {"started": False, "reason": "already running", "name": name}
    argv = [*home_env_argv(), "uv", "run", str(SCRIPT), "watch", "--project", str(repo.project), "--auto-nudge", "--loop"]
    cmd = hc.run_background(name, str(repo.project), argv, notify=False)
    if dry_run:
        return {"started": True, "dry_run": True, "command": cmd}
    res = run_cmd(cmd, timeout=20.0)
    return {"started": res["ok"], "name": name, "command": cmd, "stderr": res["stderr"].strip()[:400]}
