from __future__ import annotations

import os
from pathlib import Path


def default_powershell_candidates() -> tuple[str, ...]:
    """Return PowerShell executables from most to least preferred.

    Bare names rely on PATH, which a launcher environment may not populate
    (Task Scheduler, stripped-down shells).  Absolute fallbacks keep ACL
    hardening and dialogs working there: PowerShell 7 under Program Files
    when present, then the in-box Windows PowerShell under %SystemRoot%.
    """
    candidates = ["pwsh.exe", "powershell.exe"]
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        pwsh = Path(program_files) / "PowerShell" / "7" / "pwsh.exe"
        if pwsh.is_file():
            candidates.append(str(pwsh))
    system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    windows_powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if windows_powershell.is_file():
        candidates.append(str(windows_powershell))
    return tuple(candidates)
