"""Process-subtree CPU sampling, used to tell a silent hang from a long build."""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .store import run_cmd


def _parse_ps_cputime(text: str) -> float:
    text = text.strip()
    if not text:
        return 0.0
    days = 0
    if "-" in text:
        d_str, text = text.split("-", 1)
        days = int(d_str)
    parts = [float(p) for p in text.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0.0, parts[0], parts[1]
    else:
        return 0.0
    return days * 86400.0 + h * 3600.0 + m * 60.0 + s


def inspect_process_tree(root_pid: int | None) -> list[dict[str, Any]]:
    if not root_pid or root_pid <= 0:
        return []
    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            return []
        ps_script = (
            f"$root = {int(root_pid)}; "
            "$procs = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine; "
            "$byParent = @{}; foreach ($p in $procs) { $pp = [int]$p.ParentProcessId; "
            "if (-not $byParent.ContainsKey($pp)) { $byParent[$pp] = New-Object System.Collections.ArrayList }; "
            "[void]$byParent[$pp].Add($p) }; "
            "$byId = @{}; foreach ($p in $procs) { $byId[[int]$p.ProcessId] = $p }; "
            "$queue = New-Object System.Collections.Queue; if ($byId.ContainsKey($root)) { $queue.Enqueue($byId[$root]) }; "
            "$seen = @{}; $out = @(); while ($queue.Count -gt 0) { $cur = $queue.Dequeue(); $cid = [int]$cur.ProcessId; "
            "if ($seen.ContainsKey($cid)) { continue }; $seen[$cid] = $true; $cpu = 0.0; "
            "try { $gp = Get-Process -Id $cid -ErrorAction Stop; if ($null -ne $gp.CPU) { $cpu = [math]::Round([double]$gp.CPU, 3) } } catch {}; "
            "$out += [pscustomobject]@{ pid=$cid; ppid=[int]$cur.ParentProcessId; name=$cur.Name; cpu_sec=$cpu; cmdline=$cur.CommandLine }; "
            "if ($byParent.ContainsKey($cid)) { foreach ($ch in $byParent[$cid]) { $queue.Enqueue($ch) } } }; "
            "ConvertTo-Json -InputObject @($out) -Depth 4 -Compress"
        )
        res = run_cmd([shell, "-NoProfile", "-NonInteractive", "-Command", ps_script], timeout=10.0)
        if not res["ok"] or not res["stdout"].strip():
            return []
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(res["stdout"].strip())
            return [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return []

    # comm may contain spaces (e.g. "npm exec chrome"), so it cannot sit between split fields; args stays last.
    res = run_cmd(["ps", "-eo", "pid=,ppid=,cputime=,args="], timeout=8.0)
    if not res["ok"]:
        return []
    by_id: dict[int, dict[str, Any]] = {}
    children: dict[int, list[int]] = {}
    for line in res["stdout"].splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        by_id[pid] = {"pid": pid, "ppid": ppid, "name": Path(parts[3].split()[0]).name,
                      "cpu_sec": round(_parse_ps_cputime(parts[2]), 3), "cmdline": parts[3]}
        children.setdefault(ppid, []).append(pid)
    if root_pid not in by_id:
        return []
    queue, seen, result = [root_pid], set(), []
    while queue:
        cid = queue.pop(0)
        if cid in seen or cid not in by_id:
            continue
        seen.add(cid)
        result.append(by_id[cid])
        queue.extend(children.get(cid, []))
    return result
