from __future__ import annotations

from threading import Event, Thread
from uuid import uuid4

from fastapi.testclient import TestClient

from literature_workbench.features.library.coordinator import LibraryScanCoordinator


def test_coordination_lock_is_released_after_exception() -> None:
    coordinator = LibraryScanCoordinator()
    library_id = uuid4()

    try:
        with coordinator.acquire(library_id):
            raise RuntimeError("injected_failure")
    except RuntimeError:
        pass

    acquired_again = Event()

    def reacquire() -> None:
        with coordinator.acquire(library_id):
            acquired_again.set()

    thread = Thread(target=reacquire)
    thread.start()
    assert acquired_again.wait(1)
    thread.join(1)
    assert not thread.is_alive()


def test_different_libraries_can_scan_in_parallel() -> None:
    coordinator = LibraryScanCoordinator()
    first_library = uuid4()
    second_library = uuid4()
    second_acquired = Event()

    def acquire_second() -> None:
        with coordinator.acquire(second_library):
            second_acquired.set()

    with coordinator.acquire(first_library):
        thread = Thread(target=acquire_second)
        thread.start()
        assert second_acquired.wait(1)
        thread.join(1)

    assert not thread.is_alive()


def test_app_composition_keeps_the_shared_scan_coordinator(client: TestClient) -> None:
    assert isinstance(client.app.state.library_scan_coordinator, LibraryScanCoordinator)
    assert (
        client.app.state.library_scanner._coordinator
        is client.app.state.library_scan_coordinator
    )
