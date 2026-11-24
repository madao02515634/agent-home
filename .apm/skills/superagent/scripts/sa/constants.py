"""Shared constants: screen heuristics, choices and defaults."""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

VOLATILE_LINE_RES = [
    re.compile(r"^\s*[✻✶✳✢●·•]?\s*(Brewed|Sketching|Sautéed|Crunched|Churned|Worked|Thinking|Running)\b.*\d+[ms]", re.I),
    re.compile(r"Cache\s+\d+%\s+\d+:\d+", re.I),
    re.compile(r"\b\d+(\.\d+)?k/\d+(\.\d+)?[mk]\b", re.I),
    re.compile(r"^\s*[─━═\-]{10,}\s*$"),
    re.compile(r"\b5h:\s*[●○]+|\b7d:\s*[●○]+"),
]

INTERACTIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("permission_prompt", re.compile(r"Do you want to proceed|Allow once|Allow for this session|Permission required|Approve this", re.I)),
    ("yn_prompt", re.compile(r"\[y/N\]|\[Y/n\]|\(y/n\)|Press Enter to continue", re.I)),
    ("trust_or_auth", re.compile(r"Do you trust the files|trust this folder|trust the contents|one you trust|Adding a new directory|Please login|Sign in to|Enter password|Enter passphrase", re.I)),
    ("quota_or_rate_limit", re.compile(r"rate limit|quota exceeded|usage limit|hit your limit|overloaded|try again at", re.I)),
]

PLACEHOLDER_RE = re.compile(r"<(?:SHA|commit SHA|結束碼|工作目錄|絕對路徑|階段)>|\bTBD\b|\bPLACEHOLDER\b")


CLI_CHOICES = ["claude", "codex", "agy"]


# "none" pins a model that has no effort setting (Orca rejects any effort for claude haiku); it is never passed on.
EFFORT_CHOICES = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

# Role names are free (the Router names them per task); a role is a worker id prefix, so keep it hc-safe.
ROLE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
ROLE_KINDS = ["write", "review"]

POST_KINDS = ["brief", "done", "ask", "answer", "note", "alert"]

ROUTER = "router"

WATCHER = "watch"

DEFAULT_WATCHDOG = {
    "poll_interval_sec": 180,
    "state_stall_sec": 180,
    "prompt_stall_sec": 120,
    "silent_hang_sec": 600,
    "cpu_delta_epsilon_sec": 0.5,
    "repeat_cooldown_sec": 600,
    "max_alerts": 3,
    "dormant_idle_ticks": 5,
}

# Fields a new role inherits from its kind. Writing and reviewing default to different vendors so review is cross-model.
# dispatch refuses an empty model/effort: an unpinned role inherits whatever the CLI defaults to that day.
DEFAULT_KINDS = {
    "write": {"ade": "hc", "cli": "claude", "model": "opus", "effort": "high"},
    "review": {"ade": "hc", "cli": "codex", "model": "gpt-6.1-terra", "effort": "high"},
}

DEFAULT_MAX_STAGE_RESETS = 2
