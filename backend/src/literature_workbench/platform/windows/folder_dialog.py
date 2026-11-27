from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from literature_workbench.platform.windows.powershell import (
    default_powershell_candidates,
)


FIXED_DIALOG_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "$owner = New-Object System.Windows.Forms.Form; "
    "$owner.TopMost = $true; "
    "$owner.StartPosition = 'CenterScreen'; "
    "$owner.ShowInTaskbar = $true; "
    "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
    "$dialog.Description = '选择要关联到文献工作台的本地文件夹'; "
    "$dialog.UseDescriptionForTitle = $true; "
    "$dialog.ShowNewFolderButton = $false; "
    "try { "
    "if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { "
    "[Console]::Out.WriteLine($dialog.SelectedPath) } "
    "} finally { $dialog.Dispose(); $owner.Dispose() }"
)


class FolderDialogError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("folder_dialog_failed")


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, arguments: list[str]) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, arguments: list[str]) -> CommandResult:
        completed = subprocess.run(
            arguments,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class FolderDialog:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessCommandRunner()

    def select(self) -> Path | None:
        result: CommandResult | None = None
        for executable in default_powershell_candidates():
            try:
                result = self._runner.run(
                    [executable, "-NoProfile", "-STA", "-Command", FIXED_DIALOG_SCRIPT]
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            break
        if result is None:
            raise FolderDialogError
        if result.returncode != 0:
            raise FolderDialogError
        selected = result.stdout.rstrip("\r\n")
        if not selected:
            return None
        return Path(selected)
