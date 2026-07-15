from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import pytest

from literature_workbench.platform.windows.acl import (
    SYSTEM_SID,
    AccessControlEntry,
    AclCommandResult,
    AclSecurityError,
    DaclSnapshot,
    TrustedRuntimeRoot,
    VerifiedAclTarget,
    secure_owner_directory,
)


OWNER_SID = "S-1-5-21-100-200-300-400"
FOREIGN_SID = "S-1-5-32-545"


class FakeRunner:
    def __init__(self, *results: AclCommandResult) -> None:
        self.results = deque(results)
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str]) -> AclCommandResult:
        self.calls.append(arguments)
        if self.results:
            return self.results.popleft()
        return AclCommandResult(0, "", "")


class FakeDaclReader:
    def __init__(self, *snapshots: DaclSnapshot) -> None:
        self.snapshots = deque(snapshots)
        self.calls: list[Path] = []

    def read(self, target: Path) -> DaclSnapshot:
        self.calls.append(target)
        return self.snapshots.popleft()


@dataclass(slots=True)
class FakePathVerifier:
    result: VerifiedAclTarget | Exception

    def verify(self, target: Path, trusted_root: TrustedRuntimeRoot) -> VerifiedAclTarget:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _ace(sid: str, rights: str = "F") -> AccessControlEntry:
    return AccessControlEntry(sid=sid, rights=rights, allow=True, inherited=False)


def _snapshot(*aces: AccessControlEntry, valid: bool = True, protected: bool = True) -> DaclSnapshot:
    return DaclSnapshot(valid=valid, protected=protected, entries=tuple(aces))


def _trusted(tmp_path: Path) -> TrustedRuntimeRoot:
    return TrustedRuntimeRoot(path=tmp_path)


def _verified(target: Path, *, directory: bool = True) -> VerifiedAclTarget:
    return VerifiedAclTarget(canonical_path=target, is_directory=directory)


def test_acl_uses_exact_shell_false_commands_and_removes_foreign_allow(tmp_path: Path) -> None:
    target = tmp_path / "tokens"
    sid_runner = FakeRunner(AclCommandResult(0, f'"desktop\\user","{OWNER_SID}"\r\n', ""))
    acl_runner = FakeRunner()
    dacl_reader = FakeDaclReader(
        _snapshot(_ace(OWNER_SID), _ace(SYSTEM_SID), _ace(FOREIGN_SID)),
        _snapshot(_ace(OWNER_SID, "(OI)(CI)F"), _ace(SYSTEM_SID, "(OI)(CI)F")),
    )

    secure_owner_directory(
        target,
        _trusted(tmp_path),
        sid_runner=sid_runner,
        acl_runner=acl_runner,
        dacl_reader=dacl_reader,
        path_verifier=FakePathVerifier(_verified(target)),
    )

    assert sid_runner.calls == [["whoami.exe", "/user", "/fo", "csv", "/nh"]]
    assert acl_runner.calls == [
        ["icacls.exe", str(target), "/inheritance:r"],
        ["icacls.exe", str(target), "/remove:g", f"*{SYSTEM_SID}"],
        ["icacls.exe", str(target), "/remove:g", f"*{FOREIGN_SID}"],
        ["icacls.exe", str(target), "/setowner", f"*{OWNER_SID}"],
        [
            "icacls.exe",
            str(target),
            "/grant:r",
            f"*{OWNER_SID}:(OI)(CI)F",
            f"*{SYSTEM_SID}:(OI)(CI)F",
        ],
    ]


def test_acl_file_grants_file_rights_without_inheritance_flags(tmp_path: Path) -> None:
    target = tmp_path / "control.token"
    sid_runner = FakeRunner(AclCommandResult(0, f'"user","{OWNER_SID}"', ""))
    acl_runner = FakeRunner()
    dacl_reader = FakeDaclReader(
        _snapshot(),
        _snapshot(_ace(OWNER_SID), _ace(SYSTEM_SID)),
    )

    secure_owner_directory(
        target,
        _trusted(tmp_path),
        sid_runner=sid_runner,
        acl_runner=acl_runner,
        dacl_reader=dacl_reader,
        path_verifier=FakePathVerifier(_verified(target, directory=False)),
    )

    assert acl_runner.calls[-1][-2:] == [f"*{OWNER_SID}:F", f"*{SYSTEM_SID}:F"]


@pytest.mark.parametrize(
    "whoami_output",
    ["", '"user","not-a-sid"', '"user","S-1-5"', '"user","S-1-5-18 extra"'],
)
def test_malformed_current_sid_fails_before_acl_commands(tmp_path: Path, whoami_output: str) -> None:
    acl_runner = FakeRunner()

    with pytest.raises(AclSecurityError, match="invalid_current_user_sid"):
        secure_owner_directory(
            tmp_path / "tokens",
            _trusted(tmp_path),
            sid_runner=FakeRunner(AclCommandResult(0, whoami_output, "")),
            acl_runner=acl_runner,
            dacl_reader=FakeDaclReader(_snapshot()),
            path_verifier=FakePathVerifier(_verified(tmp_path / "tokens")),
        )

    assert acl_runner.calls == []


@pytest.mark.parametrize("reason", ["outside_trusted_root", "reparse_target", "reparse_root"])
def test_acl_rejects_untrusted_or_reparse_target_before_commands(tmp_path: Path, reason: str) -> None:
    sid_runner = FakeRunner()
    acl_runner = FakeRunner()

    with pytest.raises(AclSecurityError, match=reason):
        secure_owner_directory(
            tmp_path / "tokens",
            _trusted(tmp_path),
            sid_runner=sid_runner,
            acl_runner=acl_runner,
            dacl_reader=FakeDaclReader(_snapshot()),
            path_verifier=FakePathVerifier(AclSecurityError(reason)),
        )

    assert sid_runner.calls == []
    assert acl_runner.calls == []


@pytest.mark.parametrize(
    "final_snapshot",
    [
        _snapshot(_ace(OWNER_SID), _ace(SYSTEM_SID), _ace(FOREIGN_SID)),
        _snapshot(_ace(OWNER_SID), valid=False),
        _snapshot(_ace(OWNER_SID), protected=False),
        _snapshot(_ace(OWNER_SID)),
        _snapshot(_ace(OWNER_SID, "R"), _ace(SYSTEM_SID, "F")),
    ],
)
def test_acl_dacl_verification_failure_is_fatal(
    tmp_path: Path,
    final_snapshot: DaclSnapshot,
) -> None:
    target = tmp_path / "tokens"
    dacl_reader = FakeDaclReader(_snapshot(_ace(FOREIGN_SID)), final_snapshot)

    with pytest.raises(AclSecurityError, match="dacl_verification_failed"):
        secure_owner_directory(
            target,
            _trusted(tmp_path),
            sid_runner=FakeRunner(AclCommandResult(0, f'"user","{OWNER_SID}"', "")),
            acl_runner=FakeRunner(),
            dacl_reader=dacl_reader,
            path_verifier=FakePathVerifier(_verified(target)),
        )


def test_acl_command_exit_failure_is_fatal_even_before_dacl_verification(tmp_path: Path) -> None:
    target = tmp_path / "tokens"
    acl_runner = FakeRunner(AclCommandResult(5, "", "sensitive diagnostic"))

    with pytest.raises(AclSecurityError, match="acl_command_failed") as exc_info:
        secure_owner_directory(
            target,
            _trusted(tmp_path),
            sid_runner=FakeRunner(AclCommandResult(0, f'"user","{OWNER_SID}"', "")),
            acl_runner=acl_runner,
            dacl_reader=FakeDaclReader(_snapshot()),
            path_verifier=FakePathVerifier(_verified(target)),
        )

    assert "sensitive diagnostic" not in str(exc_info.value)
