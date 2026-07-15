from __future__ import annotations

from pathlib import Path

from literature_workbench.features.library.models import LibraryRoot
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.platform.windows.filesystem import RootIdentity, SafeFileInspector


INITIAL_APPROVED_LIBRARY = Path(r"D:\black-muntjac-project\papers")


class SelectionIdentityChanged(ValueError):
    def __init__(self) -> None:
        super().__init__("selection_identity_changed")


class LibraryRootService:
    def __init__(self, repository: LibraryRepository, inspector: SafeFileInspector) -> None:
        self._repository = repository
        self._inspector = inspector

    def register_selected_root(
        self,
        selected: RootIdentity,
        *,
        recursive: bool,
    ) -> LibraryRoot:
        current = self._inspector.inspect_root_directory(selected.canonical_path)
        if current != selected:
            raise SelectionIdentityChanged
        return self._repository.register(
            current,
            recursive=recursive,
            authorization_source="native_picker",
        )

    def register_initial_approved_library(self) -> LibraryRoot:
        identity = self._inspector.inspect_root_directory(INITIAL_APPROVED_LIBRARY)
        return self._repository.register(
            identity,
            recursive=True,
            authorization_source="initial_user_approved",
        )
