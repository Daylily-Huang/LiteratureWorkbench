from pathlib import Path
import json
import os
import socket

import pytest

import literature_workbench.launcher as launcher
from literature_workbench.config import AppSettings
from literature_workbench.platform.windows.loopback import (
    preference_path,
    read_preference,
    reserve_address,
    workspace_key,
    write_preference,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_listener_is_reserved_loopback_only_and_restarts_at_successful_address(tmp_path):
    root = tmp_path / "workspace"
    port = free_port()
    write_preference(root, port)
    with reserve_address(root) as binding:
        assert binding.port == port and binding.listener.getsockname()[0] == "127.0.0.1"
        assert not binding.listener.get_inheritable()
        with socket.socket() as competitor:
            if os.name == "nt":
                competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competitor.bind(("127.0.0.1", port))
        binding.remember()
        assert binding.public()["remembered"] and not binding.public()["address_changed"]
    with reserve_address(root) as restarted:
        assert restarted.port == port


def test_collision_never_touches_other_service_and_persists_only_own_ready_address(tmp_path):
    root = tmp_path / "workspace"
    with socket.socket() as other:
        if os.name == "nt":
            other.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        other.bind(("127.0.0.1", 0))
        other.listen()
        old_port = int(other.getsockname()[1])
        write_preference(root, old_port)
        with reserve_address(root) as binding:
            assert binding.port != old_port
            assert (
                read_preference(root)[0] == old_port
            )  # Failed starts must not remember a merely selected port.
            public = binding.public()
            assert (
                public["address_changed"]
                and public["previous_url"] == f"http://127.0.0.1:{old_port}/"
            )
            binding.remember()
            new_port = binding.port
            assert read_preference(root)[0] == new_port
        assert other.getsockname()[1] == old_port
        with reserve_address(root) as restarted:
            assert restarted.port == new_port


@pytest.mark.parametrize("value", [False, True, 0, -1, 80, 65536, "59978", None])
def test_invalid_port_record_cannot_choose_arbitrary_or_privileged_address(tmp_path, value):
    path = preference_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"version": 1, "workspace_id": workspace_key(tmp_path), "port": value}),
        encoding="utf-8",
    )
    assert read_preference(tmp_path) == (None, "address_record_reset")


def test_copied_corrupt_and_oversized_address_records_do_not_break_reading(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    write_preference(first, free_port())
    target = preference_path(second)
    target.parent.mkdir(parents=True)
    target.write_bytes(preference_path(first).read_bytes())
    assert read_preference(second) == (None, "address_record_reset")
    target.write_bytes(b"x" * 4097)
    assert read_preference(second) == (None, "address_record_reset")
    target.write_bytes(b"{broken")
    with reserve_address(second) as binding:
        assert binding.warning == "address_record_reset"
        binding.remember()
        assert read_preference(second)[0] == binding.port


def test_hardlinked_preference_is_not_read_or_written_outside_workspace(tmp_path):
    root = tmp_path / "one"
    path = preference_path(root)
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("leave untouched", encoding="utf-8")
    os.link(outside, path)
    assert read_preference(root) == (None, "address_record_unavailable")
    with reserve_address(root) as binding:
        binding.remember()
        assert not binding.public()["remembered"]
    with pytest.raises(OSError):
        write_preference(root, free_port())
    assert outside.read_text(encoding="utf-8") == "leave untouched"


def test_preference_write_failure_keeps_listener_usable_and_reports_missing_memory(
    tmp_path, monkeypatch
):
    import literature_workbench.platform.windows.loopback as module

    with reserve_address(tmp_path) as binding:
        monkeypatch.setattr(
            module, "write_preference", lambda *_: (_ for _ in ()).throw(OSError("disk full"))
        )
        binding.remember()
        assert binding.listener.fileno() >= 0
        assert binding.public()["warning"] == "address_record_unavailable"
        assert not binding.public()["remembered"]


def test_supervisor_failure_releases_reserved_socket_without_overwriting_old_address(
    tmp_path, monkeypatch
):
    port = free_port()
    write_preference(tmp_path, port)
    original = preference_path(tmp_path).read_bytes()
    monkeypatch.setattr(launcher, "recover_exact_stale_worker", lambda _: False)
    monkeypatch.setattr(launcher, "_wait_worker_lock_available", lambda _: None)

    def broken_app(*args):
        raise FileNotFoundError("missing frontend")

    monkeypatch.setattr(launcher, "create_desktop_app", broken_app)
    with pytest.raises(FileNotFoundError):
        launcher.run_supervisor(
            AppSettings(data_root=tmp_path), tmp_path / "no-web", acl_hardener=lambda *_: None
        )
    assert preference_path(tmp_path).read_bytes() == original
    with reserve_address(tmp_path) as retried:
        assert retried.port == port


def test_public_context_exposes_only_address_metadata_with_authenticated_read(
    client, authenticated
):
    client.app.state.launch_address = {
        "current_url": "http://127.0.0.1:50001/",
        "remembered": True,
        "address_changed": False,
        "previous_url": None,
        "warning": "",
    }
    value = client.get("/api/data/context").json()
    assert value["launch_address"]["current_url"] == "http://127.0.0.1:50001/"
    assert "control_token" not in str(value)
    client.cookies.clear()
    assert client.get("/api/data/context").status_code == 403
@pytest.mark.parametrize("action", ["reuse", "stop"])
def test_live_old_process_must_own_listener_before_any_request(tmp_path: Path, action: str) -> None:
    import os
    import subprocess
    import sys

    from literature_workbench import launcher

    # The old supervisor is alive, but a different process owns its former port.
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as old:
        try:
            with socket.socket() as foreign:
                foreign.bind(("127.0.0.1", 0))
                foreign.listen()
                port = foreign.getsockname()[1]
                identity = launcher.lookup_process_identity(old.pid)
                current = launcher.lookup_process_identity(os.getpid())
                assert identity is not None and current is not None
                assert launcher.owns_listening_port(current, port)
                assert not launcher.owns_listening_port(identity, port)
                path = tmp_path / "runtime" / "launcher" / "launcher.json"
                path.parent.mkdir(parents=True)
                manifest = launcher.RuntimeManifest(port, identity, identity, "c" * 43, "old")
                path.write_text(json.dumps(manifest.to_json()), encoding="utf-8")
                calls: list[str] = []
                common = dict(
                    health_probe=lambda _: calls.append("health") or True,
                    control_request=lambda *_: calls.append("control") or "foreign-token",
                )
                if action == "reuse":
                    assert not launcher.reuse_running_instance(path, browser_open=lambda _: None, **common)
                else:
                    assert not launcher.stop_running_instance(
                        path, exact_stopper=lambda _: None, timeout_seconds=0, **common
                    )
                assert calls == []
                assert old.poll() is None and foreign.getsockname()[1] == port
        finally:
            old.terminate()


