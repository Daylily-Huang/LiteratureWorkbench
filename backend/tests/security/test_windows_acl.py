from __future__ import annotations

import subprocess
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from literature_workbench.platform.windows.acl import (
    SYSTEM_SID,
    AccessControlEntry,
    AclCommandResult,
    AclSecurityError,
    DaclSnapshot,
    PowerShellDaclReader,
    SubprocessAclCommandRunner,
    TrustedRuntimeRoot,
    VerifiedAclTarget,
    WindowsAclPathVerifier,
    secure_owner_directory,
)
from literature_workbench.platform.windows.filesystem import HandleMetadata


OWNER_SID = "S-1-5-21-100-200-300-400"
FOREIGN_SID = "S-1-5-32-545"
HOSTILE_SENTINEL = r"C:\private\token-secret-should-not-escape"


class FakeRunner:
    def __init__(self, *results: AclCommandResult) -> None:
        self.results = deque(results)
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str]) -> AclCommandResult:
        self.calls.append(arguments)
        if self.results:
            return self.results.popleft()
        return AclCommandResult(0, "", "")


@dataclass(slots=True)
class RaisingAclRunner:
    error: BaseException

    def run(self, arguments: list[str]) -> AclCommandResult:
        raise self.error


class FakeDaclReader:
    def __init__(self, *snapshots: DaclSnapshot) -> None:
        self.snapshots = deque(snapshots)
        self.calls: list[Path] = []

    def read(self, target: Path) -> DaclSnapshot:
        self.calls.append(target)
        return self.snapshots.popleft()


@dataclass(frozen=True, slots=True)
class OwnerAwareSnapshot:
    owner_sid: str
    valid: bool
    protected: bool
    entries: tuple[AccessControlEntry, ...]


class HoldingPathVerifier:
    def __init__(self, target: VerifiedAclTarget) -> None:
        self.target = target
        self.active = False
        self.entered = False
        self.exited = False

    def hold(self, target: Path, trusted_root: TrustedRuntimeRoot):
        verifier = self

        class Context:
            def __enter__(self) -> VerifiedAclTarget:
                verifier.entered = True
                verifier.active = True
                return verifier.target

            def __exit__(self, exc_type, exc, traceback) -> None:
                verifier.active = False
                verifier.exited = True

        return Context()


class LifetimeCheckingRunner(FakeRunner):
    def __init__(self, verifier: HoldingPathVerifier) -> None:
        super().__init__()
        self.verifier = verifier

    def run(self, arguments: list[str]) -> AclCommandResult:
        assert self.verifier.active, "ACL target handles were released before icacls"
        return super().run(arguments)


class LifetimeCheckingReader:
    def __init__(self, verifier: HoldingPathVerifier, *snapshots: OwnerAwareSnapshot) -> None:
        self.verifier = verifier
        self.snapshots = deque(snapshots)
        self.read_count = 0

    def read(self, target: Path) -> OwnerAwareSnapshot:
        assert self.verifier.active, "ACL target handles were released before DACL read"
        self.read_count += 1
        return self.snapshots.popleft()


class FakeAclHandle:
    def __init__(self, path: Path, *, directory: bool = True) -> None:
        self.metadata = HandleMetadata(
            final_path=path,
            volume_id="0000000000000011",
            file_id="00000000000000000000000000000022",
            is_directory=directory,
            is_reparse_point=False,
            size=0,
            modified_ns=0,
            hard_link_count=1,
        )
        self.closed = False

    def read_prefix(self, length: int) -> bytes:
        return b""

    def detach_stream(self):
        raise AssertionError("not used")

    def close(self) -> None:
        self.closed = True


class RecordingAclProvider:
    def __init__(self, handles: list[FakeAclHandle]) -> None:
        self.handles = deque(handles)
        self.calls: list[tuple[Path, bool, bool]] = []

    def open_directory(self, path: Path, *, share_delete: bool = True) -> FakeAclHandle:
        self.calls.append((path, True, share_delete))
        return self.handles.popleft()

    def open_file(self, path: Path, *, share_delete: bool = True) -> FakeAclHandle:
        self.calls.append((path, False, share_delete))
        return self.handles.popleft()


@dataclass(slots=True)
class FakePathVerifier:
    result: VerifiedAclTarget | Exception

    def hold(self, target: Path, trusted_root: TrustedRuntimeRoot):
        result = self.result

        class Context:
            def __enter__(self) -> VerifiedAclTarget:
                if isinstance(result, Exception):
                    raise result
                return result

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        return Context()


def _ace(sid: str, rights: str = "F") -> AccessControlEntry:
    return AccessControlEntry(
        sid=sid,
        rights=rights,
        allow=True,
        inherited=False,
        inheritance_flags="ContainerInherit, ObjectInherit",
        propagation_flags="None",
    )


def _snapshot(*aces: AccessControlEntry, valid: bool = True, protected: bool = True) -> DaclSnapshot:
    return DaclSnapshot(
        owner_sid=OWNER_SID,
        valid=valid,
        protected=protected,
        entries=tuple(aces),
    )


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


def test_acl_rejects_final_owner_sid_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "tokens"
    final = OwnerAwareSnapshot(
        owner_sid=FOREIGN_SID,
        valid=True,
        protected=True,
        entries=(_ace(OWNER_SID, "(OI)(CI)F"), _ace(SYSTEM_SID, "(OI)(CI)F")),
    )

    with pytest.raises(AclSecurityError, match="dacl_verification_failed"):
        secure_owner_directory(
            target,
            _trusted(tmp_path),
            sid_runner=FakeRunner(AclCommandResult(0, f'"user","{OWNER_SID}"', "")),
            acl_runner=FakeRunner(),
            dacl_reader=FakeDaclReader(_snapshot(), final),
            path_verifier=FakePathVerifier(_verified(target)),
        )


def test_dacl_reader_script_derives_validity_and_returns_actual_owner_sid(tmp_path: Path) -> None:
    payload = (
        '{"owner_sid":"S-1-5-21-7","valid":false,"protected":true,'
        '"entries":[]}'
    )
    runner = FakeRunner(AclCommandResult(0, payload, ""))

    snapshot = PowerShellDaclReader(runner).read(tmp_path / "tokens")

    assert snapshot.owner_sid == "S-1-5-21-7"
    assert snapshot.valid is False
    script = runner.calls[0][3]
    assert "valid=$true" not in script
    assert "RawSecurityDescriptor" in script


def test_dacl_reader_rejects_string_validity_instead_of_coercing_it(tmp_path: Path) -> None:
    payload = (
        '{"owner_sid":"S-1-5-21-7","valid":"false","protected":true,'
        '"entries":[]}'
    )

    with pytest.raises(AclSecurityError, match="dacl_read_failed"):
        PowerShellDaclReader(FakeRunner(AclCommandResult(0, payload, ""))).read(
            tmp_path / "tokens"
        )


def test_acl_holds_verified_handles_through_mutation_and_final_descriptor_read(
    tmp_path: Path,
) -> None:
    target = tmp_path / "nested" / "tokens"
    verifier = HoldingPathVerifier(_verified(target))
    initial = OwnerAwareSnapshot(
        owner_sid=OWNER_SID,
        valid=True,
        protected=True,
        entries=(),
    )
    final = OwnerAwareSnapshot(
        owner_sid=OWNER_SID,
        valid=True,
        protected=True,
        entries=(_ace(OWNER_SID, "(OI)(CI)F"), _ace(SYSTEM_SID, "(OI)(CI)F")),
    )
    reader = LifetimeCheckingReader(verifier, initial, final)

    secure_owner_directory(
        target,
        _trusted(tmp_path),
        sid_runner=FakeRunner(AclCommandResult(0, f'"user","{OWNER_SID}"', "")),
        acl_runner=LifetimeCheckingRunner(verifier),
        dacl_reader=reader,
        path_verifier=verifier,
    )

    assert verifier.entered is True
    assert verifier.exited is True
    assert reader.read_count == 2


def test_windows_acl_context_holds_root_components_and_target_without_share_delete() -> None:
    root = Path(r"C:\runtime")
    intermediate = root / "nested"
    target = intermediate / "tokens"
    handles = [FakeAclHandle(root), FakeAclHandle(intermediate), FakeAclHandle(target)]
    provider = RecordingAclProvider(handles.copy())
    verifier = WindowsAclPathVerifier(provider)

    with verifier.hold(target, TrustedRuntimeRoot(root)) as verified:
        assert verified.canonical_path == target
        assert all(not handle.closed for handle in handles)
        assert provider.calls == [
            (root, True, False),
            (intermediate, True, False),
            (target, True, False),
        ]

    assert all(handle.closed for handle in handles)


def test_real_windows_acl_context_blocks_target_replacement_until_exit(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    target = root / "nested" / "tokens"
    target.mkdir(parents=True)
    replacement = target.with_name("replacement")

    with WindowsAclPathVerifier().hold(target, TrustedRuntimeRoot(root)):
        with pytest.raises(OSError):
            target.rename(replacement)
        assert target.is_dir()

    target.rename(replacement)
    assert replacement.is_dir()


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


@pytest.mark.parametrize(
    "error",
    [
        OSError(f"launch failed: {HOSTILE_SENTINEL}"),
        subprocess.TimeoutExpired(
            ["icacls.exe", HOSTILE_SENTINEL],
            60,
            output=HOSTILE_SENTINEL,
            stderr=HOSTILE_SENTINEL,
        ),
    ],
)
def test_subprocess_acl_runner_sanitizes_process_failures_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
) -> None:
    def fail_run(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(AclSecurityError) as exc_info:
        SubprocessAclCommandRunner().run(["icacls.exe", HOSTILE_SENTINEL])

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert str(exc_info.value) == "acl_command_failed"
    assert HOSTILE_SENTINEL not in repr(exc_info.value)
    assert HOSTILE_SENTINEL not in rendered
    assert HOSTILE_SENTINEL not in caplog.text


@pytest.mark.parametrize("surface", ["sid", "icacls", "descriptor"])
def test_acl_call_surfaces_sanitize_injected_runner_failures(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    surface: str,
) -> None:
    target = tmp_path / "private-token-root"
    hostile_error = OSError(f"{HOSTILE_SENTINEL}; target={target}")
    sid_runner = (
        RaisingAclRunner(hostile_error)
        if surface == "sid"
        else FakeRunner(AclCommandResult(0, f'"user","{OWNER_SID}"', ""))
    )
    acl_runner = RaisingAclRunner(hostile_error) if surface == "icacls" else FakeRunner()
    reader = (
        PowerShellDaclReader(RaisingAclRunner(hostile_error))
        if surface == "descriptor"
        else FakeDaclReader(
            _snapshot(),
            _snapshot(_ace(OWNER_SID, "(OI)(CI)F"), _ace(SYSTEM_SID, "(OI)(CI)F")),
        )
    )

    with pytest.raises(AclSecurityError) as exc_info:
        secure_owner_directory(
            target,
            _trusted(tmp_path),
            sid_runner=sid_runner,
            acl_runner=acl_runner,
            dacl_reader=reader,
            path_verifier=FakePathVerifier(_verified(target)),
        )

    expected = {
        "sid": "invalid_current_user_sid",
        "icacls": "acl_command_failed",
        "descriptor": "dacl_read_failed",
    }[surface]
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert str(exc_info.value) == expected
    assert HOSTILE_SENTINEL not in repr(exc_info.value)
    assert HOSTILE_SENTINEL not in rendered
    assert str(target) not in rendered
    assert HOSTILE_SENTINEL not in caplog.text


def test_dacl_reader_collects_inheritance_and_propagation_flags(tmp_path: Path) -> None:
    payload = (
        '{"owner_sid":"S-1-5-21-7","valid":true,"protected":true,'
        '"entries":[{"sid":"S-1-5-21-7","rights":"FullControl",'
        '"allow":true,"inherited":false,'
        '"inheritance_flags":"ContainerInherit, ObjectInherit",'
        '"propagation_flags":"None"}]}'
    )
    runner = FakeRunner(AclCommandResult(0, payload, ""))

    snapshot = PowerShellDaclReader(runner).read(tmp_path / "tokens")

    assert snapshot.entries[0].inheritance_flags == "ContainerInherit, ObjectInherit"
    assert snapshot.entries[0].propagation_flags == "None"
    assert "InheritanceFlags" in runner.calls[0][3]
    assert "PropagationFlags" in runner.calls[0][3]


def test_dacl_reader_rejects_non_string_inheritance_flags(tmp_path: Path) -> None:
    payload = (
        '{"owner_sid":"S-1-5-21-7","valid":true,"protected":true,'
        '"entries":[{"sid":"S-1-5-21-7","rights":"FullControl",'
        '"allow":true,"inherited":false,"inheritance_flags":[], '
        '"propagation_flags":"None"}]}'
    )

    with pytest.raises(AclSecurityError, match="dacl_read_failed"):
        PowerShellDaclReader(FakeRunner(AclCommandResult(0, payload, ""))).read(
            tmp_path / "tokens"
        )


def test_directory_full_control_without_container_and_object_inherit_fails_closed(
    tmp_path: Path,
) -> None:
    missing_flags = cast(
        AccessControlEntry,
        SimpleNamespace(
            sid=OWNER_SID,
            rights="FullControl",
            allow=True,
            inherited=False,
            inheritance_flags="None",
            propagation_flags="None",
        ),
    )
    system_missing_flags = cast(
        AccessControlEntry,
        SimpleNamespace(
            sid=SYSTEM_SID,
            rights="FullControl",
            allow=True,
            inherited=False,
            inheritance_flags="ObjectInherit",
            propagation_flags="None",
        ),
    )

    with pytest.raises(AclSecurityError, match="dacl_verification_failed"):
        secure_owner_directory(
            tmp_path / "tokens",
            _trusted(tmp_path),
            sid_runner=FakeRunner(AclCommandResult(0, f'"user","{OWNER_SID}"', "")),
            acl_runner=FakeRunner(),
            dacl_reader=FakeDaclReader(
                _snapshot(),
                DaclSnapshot(
                    owner_sid=OWNER_SID,
                    valid=True,
                    protected=True,
                    entries=(missing_flags, system_missing_flags),
                ),
            ),
            path_verifier=FakePathVerifier(_verified(tmp_path / "tokens")),
        )
