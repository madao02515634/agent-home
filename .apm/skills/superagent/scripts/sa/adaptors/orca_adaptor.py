"""Orca: workers started through `orchestration worker-start`, messages through `orchestration send`."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from typing import Any

from ..screen import detect_interactive_block, normalize_screen_lines, probe_result
from ..store import run_cmd
from .base import Adaptor, LaunchPlan, LaunchRequest

# Orca names the Antigravity CLI `antigravity`; the others match.
ORCA_AGENT_IDS = {"claude": "claude", "codex": "codex", "agy": "antigravity"}
LIVE_STATES = ("live", "ready", "active", "running", "working", "dispatched")


class OrcaAdaptor(Adaptor):
    name = "orca"
    creates_worktree = True

    def bin(self) -> str | None:
        # Bare `orca` on Linux outside Orca terminals is usually the GNOME screen reader.
        env_cmd = os.environ.get("ORCA_CLI_COMMAND")
        if env_cmd:
            return env_cmd
        if os.environ.get("ORCA_DEV_REPO_ROOT"):
            return shutil.which("orca-dev")
        if sys.platform.startswith("linux") and not os.environ.get("ORCA_TERMINAL_HANDLE"):
            return shutil.which("orca-ide")
        return shutil.which("orca")

    def _cmd(self, *args: str) -> list[str]:
        return [self.bin() or "orca-ide", "orchestration", *args, "--environment", "local"]

    def detect_router(self) -> dict[str, Any] | None:
        handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        return {"ade": self.name, "ref": handle} if handle else None

    def validate(self, model: str | None, effort: str | None) -> str | None:
        return "ORCA_EFFORT_NEEDS_MODEL" if effort not in (None, "none") and not model else None

    def launch(self, req: LaunchRequest) -> LaunchPlan:
        worktree = "new-child" if req.branch else f"path:{req.workdir}"
        cmd = self._cmd("worker-start") + ["--json", "--spec-file", str(req.prompt_file), "--worktree", worktree,
                                           "--agent", ORCA_AGENT_IDS.get(req.cli, req.cli),
                                           "--task-title", f"{req.stage} {req.role}"]
        cmd += ["--name", req.branch] if req.branch else []
        if req.model:
            cmd += ["--model", req.model] + (["--effort", req.effort] if req.effort not in (None, "none") else [])
        return LaunchPlan(command=cmd, timeout=60.0)

    def parse_ref(self, stdout: str, wid: str) -> str | None:
        # shortcut: field names follow `worker-start --help`, not a captured response; check on the first live run.
        with contextlib.suppress(json.JSONDecodeError, AttributeError):
            parsed = json.loads(stdout.strip())
            inner = parsed.get("result") if isinstance(parsed.get("result"), dict) else parsed
            inner = inner.get("dispatch") if isinstance(inner.get("dispatch"), dict) else inner
            return inner.get("dispatchId") or inner.get("dispatch_id") or inner.get("id")
        return None

    def send(self, ref: str, text: str) -> dict[str, Any]:
        if not self.bin():
            return {"sent": False, "reason": "orca CLI not found"}
        # A terminal handle (the Router) is addressed as-is; a worker is addressed by its dispatch.
        to = ref if ref.startswith(("dispatch:", "run:", "term_")) else f"dispatch:{ref}"
        res = run_cmd(self._cmd("send") + ["--to", to, "--subject", "superagent", "--body", text, "--json"], timeout=30.0)
        return {"sent": res["ok"], "stderr": res["stderr"].strip()[:400]}

    def probe(self, worker: dict[str, Any]) -> dict[str, Any]:
        ref = worker.get("backend_ref")
        if not self.bin() or not ref:
            return {"backend_ok": False, "error": "orca CLI or dispatch id unavailable"}
        show = run_cmd(self._cmd("worker-show") + ["--dispatch", str(ref), "--json"], timeout=15.0)
        if not show["ok"]:
            return {"backend_ok": False, "error": show["stderr"].strip() or "orca worker-show failed"}
        try:
            info = json.loads(show["stdout"]).get("result") or {}
        except json.JSONDecodeError:
            return {"backend_ok": False, "error": "invalid JSON from orca worker-show"}
        info = info.get("worker") or info.get("dispatch") or info
        read = run_cmd(self._cmd("worker-read") + ["--dispatch", str(ref)], timeout=15.0)
        screen = read["stdout"] if read["ok"] else ""
        liveness = str(info.get("liveness") or info.get("state") or info.get("status") or "live").lower()
        backend_state = "live" if liveness in LIVE_STATES else "finished"
        interactive = detect_interactive_block(normalize_screen_lines(screen)[0])
        if not interactive and str(info.get("waitingOnHuman") or "").lower() in ("yes", "true", "waiting"):
            interactive = {"pattern": "orca_waiting_on_human", "line": f"waitingOnHuman={info.get('waitingOnHuman')}"}
        return probe_result(screen, backend_state=backend_state,
                            agent_status=str(info.get("activity") or ("finished" if backend_state == "finished" else "working")).lower(),
                            status_msg=str(info.get("lastActivity") or ""), interactive=interactive)

    def stop(self, ref: str) -> dict[str, Any]:
        return {"stop": run_cmd(self._cmd("worker-stop") + ["--dispatch", ref, "--json"], timeout=20.0)}
