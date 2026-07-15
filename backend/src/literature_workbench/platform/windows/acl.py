from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from literature_workbench.platform.windows.filesystem import Win32FileMetadataProvider


SYSTEM_SID = "S-1-5-18"
_SID_PATTERN = re.compile(r"^S-1-(?:\d+-)+\d+$")
_PRODUCTION_RUNTIME_ROOT = Path(r"D:\LiteratureWorkbenchData\runtime")
_DACL_SCRIPT = (
    "$acl=Get-Acl -LiteralPath $args[0];"
    "$entries=@($acl.Access | ForEach-Object {"
    "$sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;"
    "[pscustomobject]@{sid=$sid;rights=$_.FileSystemRights.ToString();"
    "allow=($_.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow);"
    "inherited=$_.IsInherited}});"
    "[pscustomobject]@{valid=$true;protected=$acl.AreAccessRulesProtected;entries=$entries}"
    "| ConvertTo-Json -Compress -Depth 4"
)


class AclSecurityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AclCommandResult:
    returncode: int
    stdout: str
    stderr: str


class AclCommandRunner(Protocol):
    def run(self, arguments: list[str]) -> AclCommandResult: ...


class SubprocessAclCommandRunner:
    def run(self, arguments: list[str]) -> AclCommandResult:
        completed = subprocess.run(
            arguments,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return AclCommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class AccessControlEntry:
    sid: str
    rights: str
    allow: bool
    inherited: bool


@dataclass(frozen=True, slots=True)
class DaclSnapshot:
    valid: bool
    protected: bool
    entries: tuple[AccessControlEntry, ...]


class DaclReader(Protocol):
    def read(self, target: Path) -> DaclSnapshot: ...


class PowerShellDaclReader:
    def __init__(self, runner: AclCommandRunner) -> None:
        self._runner = runner

    def read(self, target: Path) -> DaclSnapshot:
        result = self._runner.run(
            ["pwsh.exe", "-NoProfile", "-Command", _DACL_SCRIPT, str(target)]
        )
        if result.returncode != 0:
            raise AclSecurityError("dacl_read_failed")
        try:
            payload = json.loads(result.stdout)
            entries = tuple(
                AccessControlEntry(
                    sid=str(entry["sid"]),
                    rights=str(entry["rights"]),
                    allow=bool(entry["allow"]),
                    inherited=bool(entry["inherited"]),
                )
                for entry in payload["entries"]
            )
            return DaclSnapshot(
                valid=bool(payload["valid"]),
                protected=bool(payload["protected"]),
                entries=entries,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise AclSecurityError("dacl_read_failed") from error


@dataclass(frozen=True, slots=True)
class TrustedRuntimeRoot:
    path: Path


def create_production_trusted_runtime_root() -> TrustedRuntimeRoot:
    return TrustedRuntimeRoot(_PRODUCTION_RUNTIME_ROOT)


@dataclass(frozen=True, slots=True)
class VerifiedAclTarget:
    canonical_path: Path
    is_directory: bool


class AclPathVerifier(Protocol):
    def verify(self, target: Path, trusted_root: TrustedRuntimeRoot) -> VerifiedAclTarget: ...


class WindowsAclPathVerifier:
    def __init__(self, provider: Win32FileMetadataProvider | None = None) -> None:
        self._provider = provider or Win32FileMetadataProvider()

    def verify(self, target: Path, trusted_root: TrustedRuntimeRoot) -> VerifiedAclTarget:
        root_handle = self._provider.open_directory(trusted_root.path)
        target_handle = None
        try:
            root = root_handle.metadata
            if not root.is_directory or root.is_reparse_point:
                raise AclSecurityError("reparse_root")
            try:
                target_handle = self._provider.open_directory(target)
            except OSError:
                target_handle = self._provider.open_file(target)
            metadata = target_handle.metadata
            if metadata.is_reparse_point:
                raise AclSecurityError("reparse_target")
            if not self._contained(metadata.final_path, root.final_path):
                raise AclSecurityError("outside_trusted_root")
            return VerifiedAclTarget(metadata.final_path, metadata.is_directory)
        finally:
            if target_handle is not None:
                target_handle.close()
            root_handle.close()

    @staticmethod
    def _contained(candidate: Path, root: Path) -> bool:
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        root_key = os.path.normcase(os.path.abspath(root))
        try:
            return os.path.commonpath((candidate_key, root_key)) == root_key
        except ValueError:
            return False


def _run_required(runner: AclCommandRunner, arguments: list[str]) -> None:
    if runner.run(arguments).returncode != 0:
        raise AclSecurityError("acl_command_failed")


def _current_user_sid(runner: AclCommandRunner) -> str:
    result = runner.run(["whoami.exe", "/user", "/fo", "csv", "/nh"])
    if result.returncode != 0:
        raise AclSecurityError("invalid_current_user_sid")
    try:
        rows = list(csv.reader(result.stdout.splitlines()))
        sid = rows[0][1].strip() if len(rows) == 1 and len(rows[0]) == 2 else ""
    except (csv.Error, IndexError):
        sid = ""
    if _SID_PATTERN.fullmatch(sid) is None:
        raise AclSecurityError("invalid_current_user_sid")
    return sid


def _has_required_rights(entry: AccessControlEntry, *, directory: bool) -> bool:
    rights = entry.rights.replace(" ", "").upper()
    if "FULLCONTROL" in rights:
        return True
    if directory:
        return "(OI)(CI)F" in rights
    return rights == "F"


def _verify_final_dacl(
    snapshot: DaclSnapshot,
    *,
    owner_sid: str,
    directory: bool,
) -> None:
    expected = {owner_sid, SYSTEM_SID}
    if not snapshot.valid or not snapshot.protected:
        raise AclSecurityError("dacl_verification_failed")
    if any(
        not entry.allow or entry.inherited or entry.sid not in expected
        for entry in snapshot.entries
    ):
        raise AclSecurityError("dacl_verification_failed")
    by_sid = {entry.sid: entry for entry in snapshot.entries if entry.allow}
    if set(by_sid) != expected:
        raise AclSecurityError("dacl_verification_failed")
    if any(
        not _has_required_rights(by_sid[sid], directory=directory)
        for sid in expected
    ):
        raise AclSecurityError("dacl_verification_failed")


def secure_owner_directory(
    target: Path,
    trusted_runtime_root: TrustedRuntimeRoot,
    *,
    sid_runner: AclCommandRunner | None = None,
    acl_runner: AclCommandRunner | None = None,
    dacl_reader: DaclReader | None = None,
    path_verifier: AclPathVerifier | None = None,
) -> None:
    resolved_acl_runner = acl_runner or SubprocessAclCommandRunner()
    resolved_sid_runner = sid_runner or SubprocessAclCommandRunner()
    resolved_reader = dacl_reader or PowerShellDaclReader(resolved_acl_runner)
    verified = (path_verifier or WindowsAclPathVerifier()).verify(target, trusted_runtime_root)
    owner_sid = _current_user_sid(resolved_sid_runner)
    initial = resolved_reader.read(verified.canonical_path)

    _run_required(
        resolved_acl_runner,
        ["icacls.exe", str(verified.canonical_path), "/inheritance:r"],
    )
    non_owner_allow_sids = sorted(
        {
            entry.sid
            for entry in initial.entries
            if entry.allow
            and not entry.inherited
            and entry.sid != owner_sid
        }
    )
    for sid in non_owner_allow_sids:
        _run_required(
            resolved_acl_runner,
            ["icacls.exe", str(verified.canonical_path), "/remove:g", f"*{sid}"],
        )
    _run_required(
        resolved_acl_runner,
        ["icacls.exe", str(verified.canonical_path), "/setowner", f"*{owner_sid}"],
    )
    inheritance = "(OI)(CI)" if verified.is_directory else ""
    _run_required(
        resolved_acl_runner,
        [
            "icacls.exe",
            str(verified.canonical_path),
            "/grant:r",
            f"*{owner_sid}:{inheritance}F",
            f"*{SYSTEM_SID}:{inheritance}F",
        ],
    )
    _verify_final_dacl(
        resolved_reader.read(verified.canonical_path),
        owner_sid=owner_sid,
        directory=verified.is_directory,
    )
