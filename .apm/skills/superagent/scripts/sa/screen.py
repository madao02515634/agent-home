"""Screen-text heuristics shared by adaptors that can read a worker's terminal."""

from __future__ import annotations

import hashlib
from typing import Any

from .constants import ANSI_RE, INTERACTIVE_PATTERNS, VOLATILE_LINE_RES


def normalize_screen_lines(raw_text: str) -> tuple[list[str], str]:
    cleaned, stable = [], []
    for raw in raw_text.splitlines():
        line = ANSI_RE.sub("", raw).rstrip()
        cleaned.append(line)
        if line.strip() and not any(pat.search(line) for pat in VOLATILE_LINE_RES):
            stable.append(line)
    digest = hashlib.sha256("\n".join(stable[-45:]).encode("utf-8", errors="replace")).hexdigest()
    return cleaned[-45:], digest


def detect_interactive_block(lines: list[str]) -> dict[str, str] | None:
    non_empty = [ln.strip() for ln in lines if ln.strip()][-25:]
    for ln in reversed(non_empty):
        for label, pat in INTERACTIVE_PATTERNS:
            if pat.search(ln):
                return {"pattern": label, "line": ln[:240]}
    return None


def probe_result(screen_text: str, *, backend_state: str, agent_status: str, status_msg: str = "",
                 exit_code: Any = None, pid: Any = None, process_tree: list[dict[str, Any]] | None = None,
                 interactive: dict[str, str] | None = None) -> dict[str, Any]:
    """The probe shape watch consumes, whatever ADE produced it."""
    lines, digest = normalize_screen_lines(screen_text)
    tree = process_tree or []
    return {"backend_ok": True, "backend_state": backend_state, "agent_status": agent_status, "status_msg": status_msg,
            "exit_code": exit_code, "pid": pid, "screen_lines": lines, "screen_hash": digest, "process_tree": tree,
            "subtree_cpu_sec": round(sum(float(p.get("cpu_sec", 0)) for p in tree), 3),
            "interactive_match": interactive or detect_interactive_block(lines)}
