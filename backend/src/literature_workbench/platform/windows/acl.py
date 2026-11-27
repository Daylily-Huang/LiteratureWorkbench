from __future__ import annotations

import base64
import csv
import json
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from literature_workbench.platform.windows.filesystem import (
    OwnedFileHandle,
    Win32FileMetadataProvider,
)
from literature_workbench.platform.windows.powershell import (
    default_powershell_candidates,
)


SYSTEM_SID = "S-1-5-18"
_SID_PATTERN = re.compile(r"^S-1-(?:\d+-)+\d+$")
_PRODUCTION_RUNTIME_ROOT = Path(r"D:\LiteratureWorkbenchData\runtime")
_DACL_SCRIPT = (
    "$target=[Text.Encoding]::UTF8.GetString("
    "[Convert]::FromBase64String('__TARGET_B64__'));"
    "$acl=Get-Acl -LiteralPath $target;"
    "$sddl=$acl.GetSecurityDescriptorSddlForm("
    "[System.Security.AccessControl.AccessControlSections]::All);"
    "$raw=[System.Security.AccessControl.RawSecurityDescriptor]::new($sddl);"
    "$ownerSid=if($null -ne $raw.Owner){$raw.Owner.Value}else{''};"
    "$valid=($null -ne $raw.DiscretionaryAcl);"
    "$protected=(($raw.ControlFlags -band "
    "[System.Security.AccessControl.ControlFlags]::DiscretionaryAclProtected) -ne 0);"
    "$entries=@($acl.Access | ForEach-Object {"
    "$sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;"
    "[pscustomobject]@{sid=$sid;rights=$_.FileSystemRights.ToString();"
    "allow=($_.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow);"
    "inherited=$_.IsInherited;inheritance_flags=$_.InheritanceFlags.ToString();"
    "propagation_flags=$_.PropagationFlags.ToString()}});"
    "[pscustomobject]@{owner_sid=$ownerSid;valid=$valid;protected=$protected;entries=$entries}"
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
        try:
            completed = subprocess.run(
                arguments,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AclSecurityError("acl_command_failed") from None
        return AclCommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class AccessControlEntry:
    sid: str
    rights: str
    allow: bool
    inherited: bool
    inheritance_flags: str
    propagation_flags: str


@dataclass(frozen=True, slots=True)
class DaclSnapshot:
    owner_sid: str
    valid: bool
    protected: bool
    entries: tuple[AccessControlEntry, ...]


class DaclReader(Protocol):
    def read(self, target: Path) -> DaclSnapshot: ...


class PowerShellDaclReader:
    def __init__(
        self,
        runner: AclCommandRunner,
        *,
        executables: tuple[str, ...] | None = None,
    ) -> None:
        self._runner = runner
        self._executables = (
            tuple(executables)
            if executables is not None
            else default_powershell_candidates()
        )

    def read(self, target: Path) -> DaclSnapshot:
        encoded_target = base64.b64encode(str(target).encode("utf-8")).decode("ascii")
        script = _DACL_SCRIPT.replace("__TARGET_B64__", encoded_target)
        result: AclCommandResult | None = None
        for executable in self._executables:
            try:
                result = self._runner.run(
                    [executable, "-NoProfile", "-Command", script]
                )
            except (OSError, subprocess.TimeoutExpired, AclSecurityError):
                continue
            break
        if result is None:
            raise AclSecurityError("dacl_read_failed") from None
        if result.returncode != 0:
            raise AclSecurityError("dacl_read_failed")
        try:
            payload = json.loads(result.stdout)
            owner_sid = payload["owner_sid"]
            valid = payload["valid"]
            protected = payload["protected"]
            raw_entries = payload["entries"]
            if (
                not isinstance(owner_sid, str)
                or _SID_PATTERN.fullmatch(owner_sid) is None
                or not isinstance(valid, bool)
                or not isinstance(protected, bool)
                or not isinstance(raw_entries, list)
            ):
                raise TypeError("invalid DACL payload")
            entries = tuple(
                AccessControlEntry(
                    sid=entry["sid"],
                    rights=entry["rights"],
                    allow=entry["allow"],
                    inherited=entry["inherited"],
                    inheritance_flags=entry["inheritance_flags"],
                    propagation_flags=entry["propagation_flags"],
                )
                for entry in raw_entries
            )
            if any(
                _SID_PATTERN.fullmatch(entry.sid) is None
                or not isinstance(entry.rights, str)
                or not isinstance(entry.allow, bool)
                or not isinstance(entry.inherited, bool)
                or not isinstance(entry.inheritance_flags, str)
                or not isinstance(entry.propagation_flags, str)
                for entry in entries
            ):
                raise TypeError("invalid DACL entry")
            return DaclSnapshot(
                owner_sid=owner_sid,
                valid=valid,
                protected=protected,
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
    def hold(
        self,
        target: Path,
        trusted_root: TrustedRuntimeRoot,
    ) -> AbstractContextManager[VerifiedAclTarget]: ...


class AclHandleProvider(Protocol):
    def open_directory(
        self,
        path: Path,
        *,
        share_delete: bool = True,
    ) -> OwnedFileHandle: ...

    def open_file(
        self,
        path: Path,
        *,
        share_delete: bool = True,
    ) -> OwnedFileHandle: ...


class WindowsAclPathVerifier:
    def __init__(self, provider: AclHandleProvider | None = None) -> None:
        self._provider = provider or Win32FileMetadataProvider()

    @contextmanager
    def hold(
        self,
        target: Path,
        trusted_root: TrustedRuntimeRoot,
    ) -> Iterator[VerifiedAclTarget]:
        root_path = Path(os.path.abspath(trusted_root.path))
        target_path = Path(os.path.abspath(target))
        if not self._contained(target_path, root_path):
            raise AclSecurityError("outside_trusted_root")
        held_handles: list[OwnedFileHandle] = []
        try:
            root_handle = self._provider.open_directory(root_path, share_delete=False)
            held_handles.append(root_handle)
            root = root_handle.metadata
            if not root.is_directory or root.is_reparse_point:
                raise AclSecurityError("reparse_root")

            relative = Path(os.path.relpath(target_path, root_path))
            current = root_path
            for component in relative.parts[:-1]:
                current /= component
                component_handle = self._provider.open_directory(
                    current,
                    share_delete=False,
                )
                held_handles.append(component_handle)
                metadata = component_handle.metadata
                if not metadata.is_directory or metadata.is_reparse_point:
                    raise AclSecurityError("reparse_target")
                if not self._contained(metadata.final_path, root.final_path):
                    raise AclSecurityError("outside_trusted_root")

            try:
                target_handle = self._provider.open_directory(
                    target_path,
                    share_delete=False,
                )
            except OSError:
                target_handle = self._provider.open_file(
                    target_path,
                    share_delete=False,
                )
            held_handles.append(target_handle)
            metadata = target_handle.metadata
            if metadata.is_reparse_point:
                raise AclSecurityError("reparse_target")
            if not self._contained(metadata.final_path, root.final_path):
                raise AclSecurityError("outside_trusted_root")
            yield VerifiedAclTarget(metadata.final_path, metadata.is_directory)
        finally:
            for handle in reversed(held_handles):
                handle.close()

    @staticmethod
    def _contained(candidate: Path, root: Path) -> bool:
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        root_key = os.path.normcase(os.path.abspath(root))
        try:
            return os.path.commonpath((candidate_key, root_key)) == root_key
        except ValueError:
            return False


def _run_required(runner: AclCommandRunner, arguments: list[str]) -> None:
    try:
        result = runner.run(arguments)
    except (OSError, subprocess.TimeoutExpired, AclSecurityError):
        raise AclSecurityError("acl_command_failed") from None
    if result.returncode != 0:
        raise AclSecurityError("acl_command_failed")


def _current_user_sid(runner: AclCommandRunner) -> str:
    try:
        result = runner.run(["whoami.exe", "/user", "/fo", "csv", "/nh"])
    except (OSError, subprocess.TimeoutExpired, AclSecurityError):
        raise AclSecurityError("invalid_current_user_sid") from None
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


def _has_required_directory_inheritance(entry: AccessControlEntry) -> bool:
    flags = {
        flag.strip().casefold()
        for flag in entry.inheritance_flags.split(",")
        if flag.strip()
    }
    return {"containerinherit", "objectinherit"}.issubset(flags)


def _verify_final_dacl(
    snapshot: DaclSnapshot,
    *,
    owner_sid: str,
    directory: bool,
) -> None:
    expected = {owner_sid, SYSTEM_SID}
    if snapshot.owner_sid != owner_sid or not snapshot.valid or not snapshot.protected:
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
    if directory and any(
        not _has_required_directory_inheritance(by_sid[sid])
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
    verifier = path_verifier or WindowsAclPathVerifier()
    with verifier.hold(target, trusted_runtime_root) as verified:
        owner_sid = _current_user_sid(resolved_sid_runner)
        initial = resolved_reader.read(verified.canonical_path)
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
        _run_required(
            resolved_acl_runner,
            ["icacls.exe", str(verified.canonical_path), "/setowner", f"*{owner_sid}"],
        )
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
                and entry.sid not in {owner_sid, SYSTEM_SID}
            }
        )
        for sid in non_owner_allow_sids:
            _run_required(
                resolved_acl_runner,
                ["icacls.exe", str(verified.canonical_path), "/remove:g", f"*{sid}"],
            )
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
