"""herdr-core (hc): background terminals with screen reads, process trees and typed-in notices."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from typing import Any

from ..clis import build_cli_argv
from ..procs import inspect_process_tree
from ..screen import probe_result
from ..store import run_cmd
from .base import Adaptor, LaunchPlan, LaunchRequest


class HcAdaptor(Adaptor):
    name = "hc"
    passes_cli_args = True

    def bin(self) -> str | None:
        return shutil.which("hc") or shutil.which("herdr-core")

    def get(self, ref: str) -> dict[str, Any] | None:
        res = run_cmd([self.bin() or "hc", "get", ref], timeout=10.0)
        if not res["ok"]:
            return None
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(res["stdout"])
        return None

    def detect_router(self) -> dict[str, Any] | None:
        hc_id = os.environ.get("HC_TERMINAL_ID")
        if not (hc_id and self.bin()):
            return None
        info = self.get(hc_id) or {}
        return {"ade": self.name, "ref": hc_id, "herdr_space": (info.get("labels") or {}).get("herdr.space")}

    def run_background(self, name: str, cwd: str, argv: list[str], *, label: str | None = None,
                       notify: bool = True) -> list[str]:
        # --outlive-parent: without it the worker dies when the Router's terminal closes.
        cmd = [self.bin() or "hc", "run", "--bg", "--name", name, "--cwd", cwd, "--outlive-parent"]
        cmd += [] if notify else ["--no-notify"]
        cmd += ["--label", label] if label else []
        return [*cmd, "--", *argv]

    def launch(self, req: LaunchRequest) -> LaunchPlan:
        short_prompt = f"讀 {req.prompt_file} 並照做。"
        # Without a herdr.space label the terminal stays hidden from the Herdr window.
        label = f"herdr.space={req.herdr_space}" if req.herdr_space else None
        argv = build_cli_argv(req.cli, short_prompt, req.extra_dirs, req.model, req.effort, req.settings_file)
        return LaunchPlan(command=self.run_background(req.wid, str(req.workdir), argv, label=label))

    def parse_ref(self, stdout: str, wid: str) -> str | None:
        with contextlib.suppress(json.JSONDecodeError, AttributeError):
            return json.loads(stdout.strip()).get("address") or wid
        return wid

    def is_live(self, ref: str) -> bool:
        return (self.get(ref) or {}).get("state") == "live"

    def send(self, ref: str, text: str) -> dict[str, Any]:
        if not self.bin():
            return {"sent": False, "reason": "hc not found"}
        # hc send waits up to ~60s for a person's unfinished draft in that terminal.
        res = run_cmd([self.bin() or "hc", "send", ref, text], timeout=90.0)
        return {"sent": res["ok"], "stderr": res["stderr"].strip()[:400]}

    def probe(self, worker: dict[str, Any]) -> dict[str, Any]:
        ref = worker.get("backend_ref")
        if not self.bin() or not ref:
            return {"backend_ok": False, "error": "hc or backend_ref unavailable"}
        info = self.get(ref)
        if info is None:
            return {"backend_ok": False, "error": f"hc get {ref} failed"}
        read = run_cmd([self.bin() or "hc", "read", "--lines", "0", ref], timeout=10.0)
        root_pid = (info.get("foreground") or {}).get("pid") or info.get("pid")
        st = info.get("status") or {}
        return probe_result(read["stdout"] if read["ok"] else "", backend_state=info.get("state", "live"),
                            agent_status=st.get("state") or ("finished" if info.get("state") == "finished" else "working"),
                            status_msg=st.get("msg", ""), exit_code=info.get("exit_code"), pid=root_pid,
                            process_tree=inspect_process_tree(root_pid))

    def stop(self, ref: str) -> dict[str, Any]:
        hbin = self.bin() or "hc"
        return {"kill": run_cmd([hbin, "kill", ref], timeout=8.0), "rm": run_cmd([hbin, "rm", ref], timeout=8.0)}
