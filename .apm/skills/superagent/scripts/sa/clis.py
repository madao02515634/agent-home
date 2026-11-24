"""Command lines for the agent CLIs a worker can run (claude, codex, agy)."""

from __future__ import annotations

from pathlib import Path


def build_cli_argv(cli: str, prompt: str, extra_dirs: list[Path], model: str | None, effort: str | None,
                   settings: Path | None = None) -> list[str]:
    """`settings` carries superagent's Stop hook; only claude takes per-run settings without a trust step."""
    effort = None if effort == "none" else effort
    dirs = [str(d) for d in dict.fromkeys(p.resolve() for p in extra_dirs)]
    if cli == "claude":
        argv = ["claude"]
        argv += ["--model", model] if model else []
        argv += ["--effort", effort] if effort else []
        for d in dirs:
            argv += ["--add-dir", d]
        argv += ["--settings", str(settings)] if settings else []
        return [*argv, "--permission-mode", "auto", prompt]
    if cli == "codex":
        argv = ["codex", "--no-daemon", "-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
        for d in dirs:
            argv += ["--add-dir", d]
        argv += ["-m", model] if model else []
        argv += ["-c", f'model_reasoning_effort="{effort}"'] if effort else []
        return [*argv, "-a", "never", prompt]
    if cli == "agy":
        argv = ["agy"]
        for d in dirs:
            argv += ["--add-dir", d]
        argv += ["--model", model] if model else []
        argv += ["--effort", effort] if effort else []
        return [*argv, "-i", prompt]
    return [cli, prompt]
