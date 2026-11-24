"""Paths, locking and JSON state of one project's .superagent/<owner>-<repo>/ store."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

# The CLI entry; workers and the hc watcher re-invoke it by absolute path.
SCRIPT = Path(__file__).resolve().parent.parent / "superagent.py"



def utc_now(override_iso: str | None = None) -> datetime:
    if override_iso:
        dt = datetime.fromisoformat(override_iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)



def iso_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)



def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)



def read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default



def emit(args: argparse.Namespace, payload: dict[str, Any], summary: list[str], exit_code: int = 0) -> int:
    if getattr(args, "output", None):
        atomic_write_json(Path(args.output), payload)
        summary = [*summary, f"Output written to: {Path(args.output).resolve()}"]
    for line in summary:
        print(line)
    return exit_code



def run_cmd(argv: list[str], cwd: Path | None = None, timeout: float = 15.0) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr, "elapsed_sec": round(time.monotonic() - t0, 3)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s",
                "elapsed_sec": round(time.monotonic() - t0, 3)}
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": str(exc), "elapsed_sec": 0.0}



def hc_name(text: str) -> str:
    """hc names: 1-32 of a-z 0-9 - _."""
    name = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
    return (name or "worker")[:32]



def default_home() -> Path | None:
    """The git repo holding this script; the home unless SUPERAGENT_HOME overrides it."""
    res = run_cmd(["git", "-C", str(SCRIPT.parent), "rev-parse", "--show-toplevel"], timeout=5.0)
    return Path(res["stdout"].strip()).resolve() if res["ok"] else None


def superagent_home() -> Path:
    env = os.environ.get("SUPERAGENT_HOME")
    if env:
        return Path(env).resolve()
    home = default_home()
    if home is None:
        raise RuntimeError(f"Cannot locate superagent home: {SCRIPT} is not inside a git repo; set SUPERAGENT_HOME")
    return home



def home_env_argv() -> list[str]:
    """Commands run by workers and the watcher do not inherit this shell's env, so an overridden home travels with them."""
    env = os.environ.get("SUPERAGENT_HOME")
    return ["env", f"SUPERAGENT_HOME={Path(env).resolve()}"] if env else []


def parse_remote_key(url: str) -> str | None:
    """`git@host:owner/name.git`, `https://host/group/sub/name` -> `owner-name` / `group-sub-name`."""
    url = url.strip()
    m = re.match(r"^[\w.-]+@[\w.-]+:(?P<path>.+)$", url) or re.match(r"^[a-z+]+://[^/]+/(?P<path>.+)$", url)
    if not m:
        return None
    parts = [p for p in re.sub(r"\.git/?$", "", m.group("path").strip("/")).split("/") if p]
    return "-".join(parts) if len(parts) >= 2 else None



def repo_key(project: Path) -> str:
    res = run_cmd(["git", "-C", str(project), "remote", "get-url", "origin"], timeout=5.0)
    key = parse_remote_key(res["stdout"]) if res["ok"] else None
    key = key or project.name
    return re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-") or "repo"



class Repo:
    """Paths of one project's superagent store."""

    def __init__(self, project: str | Path):
        self.project = Path(project).resolve()
        if not self.project.exists():
            raise FileNotFoundError(f"project path does not exist: {self.project}")
        self.home = superagent_home()
        self.key = repo_key(self.project)
        self.dir = self.home / ".superagent" / self.key
        self.config = self.dir / "config.json"
        self.members = self.dir / "members.json"
        self.state = self.dir / "state.json"
        self.board = self.dir / "board.jsonl"
        self.stages = self.dir / "stages"

    def stage_dir(self, stage: str) -> Path:
        return self.stages / stage

    def load_config(self) -> dict[str, Any]:
        if not self.config.exists():
            raise FileNotFoundError(f"Missing config: {self.config}. Run `init` first.")
        return read_json(self.config, {})

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """One lock for every read-modify-write of members/state/board: workers post concurrently."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "a+b") as fh:
            if fcntl:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            else:  # pragma: no cover
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

    def load_members(self) -> dict[str, Any]:
        return read_json(self.members, {"router": None, "workers": {}})

    def save_members(self, members: dict[str, Any]) -> None:
        atomic_write_json(self.members, members)

    def load_state(self) -> dict[str, Any]:
        return read_json(self.state, {"version": 2, "active_stage": None, "idle_ticks": 0, "stages": {}})

    def save_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state, state)

    def read_board(self) -> list[dict[str, Any]]:
        if not self.board.exists():
            return []
        out = []
        for line in self.board.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def append_board(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Caller holds the lock; seq is the line count + 1."""
        posts = self.read_board()
        entry = {"seq": (posts[-1]["seq"] if posts else 0) + 1, **entry}
        with self.board.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry



def ensure_home_ignored(home: Path) -> dict[str, Any]:
    probe = run_cmd(["git", "-C", str(home), "check-ignore", "-q", ".superagent/probe"], timeout=5.0)
    if probe["returncode"] == 0:
        return {"ignored": True, "added": False}
    gi = home / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    sep = "" if not text or text.endswith("\n") else "\n"
    gi.write_text(text + sep + "\n# superagent 每台機器的設定與進度\n.superagent/\n", encoding="utf-8")
    return {"ignored": True, "added": True, "gitignore": str(gi)}
