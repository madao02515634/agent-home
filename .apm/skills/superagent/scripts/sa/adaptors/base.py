"""The contract every ADE adaptor implements; dispatch, watch, notify and close-worker only talk to this."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LaunchRequest:
    wid: str
    role: str
    stage: str
    workdir: Path
    prompt_file: Path
    instructions: str
    cli: str
    model: str | None
    effort: str | None
    extra_dirs: list[Path] = field(default_factory=list)
    herdr_space: str | None = None
    settings_file: Path | None = None  # per-run CLI settings holding the Stop hook
    branch: str | None = None


@dataclass
class LaunchPlan:
    command: list[str] | None = None  # run by dispatch; None means the Router spawns it (native)
    native_spawn: dict[str, Any] | None = None
    timeout: float = 20.0


class Adaptor:
    name = ""
    can_push = True  # send() reaches an agent running on this ADE
    can_probe = True  # probe() returns screen/state, so watch can judge stalls
    needs_pinned_model = True  # dispatch refuses an empty model/effort
    creates_worktree = False  # the ADE makes its own worktree for --create-worktree-branch
    passes_cli_args = False  # launch() builds the CLI argv itself, so per-run settings (the Stop hook) reach it

    def bin(self) -> str | None:
        return None

    def detect_router(self) -> dict[str, Any] | None:
        """The Router's endpoint when the Router itself runs on this ADE, else None."""
        return None

    def validate(self, model: str | None, effort: str | None) -> str | None:
        """An error code when the ADE cannot take this model/effort combination."""
        return None

    def launch(self, req: LaunchRequest) -> LaunchPlan:
        raise NotImplementedError

    def parse_ref(self, stdout: str, wid: str) -> str | None:
        return None

    def send(self, ref: str, text: str) -> dict[str, Any]:
        return {"sent": False, "reason": f"{self.name} has no push channel"}

    def probe(self, worker: dict[str, Any]) -> dict[str, Any]:
        return {"backend_ok": False, "error": f"{self.name} cannot be probed"}

    def stop(self, ref: str) -> dict[str, Any]:
        return {}
