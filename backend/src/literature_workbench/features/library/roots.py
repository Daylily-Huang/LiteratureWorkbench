from __future__ import annotations

import os
from pathlib import Path

from literature_workbench.features.library.models import LibraryRoot
from literature_workbench.features.library.repository import LibraryRepository
from literature_workbench.platform.windows.filesystem import RootIdentity, SafeFileInspector, UnsafePath


INITIAL_APPROVED_LIBRARY = Path(r"D:\black-muntjac-project\papers")


class SelectionIdentityChanged(ValueError):
    def __init__(self) -> None:
        super().__init__("selection_identity_changed")


class LibraryRootService:
    def __init__(
        self,
        repository: LibraryRepository,
        inspector: SafeFileInspector,
        *,
        private_data_root: Path,
    ) -> None:
        self._repository = repository
        self._inspector = inspector
        self._private_data_root = private_data_root

    def register_selected_root(
        self,
        selected: RootIdentity,
        *,
        recursive: bool,
    ) -> LibraryRoot:
        current = self._inspector.inspect_root_directory(selected.canonical_path)
        if current != selected:
            raise SelectionIdentityChanged
        self._reject_private_data_overlap(current.canonical_path)
        return self._repository.register(
            current,
            recursive=recursive,
            authorization_source="native_picker_confirmed",
        )

    def register_initial_approved_library(self) -> LibraryRoot | None:
        try:
            identity = self._inspector.inspect_root_directory(INITIAL_APPROVED_LIBRARY)
            self._reject_private_data_overlap(identity.canonical_path)
            return self._repository.register(
                identity,
                recursive=True,
                authorization_source="initial_user_approved",
            )
        except (FileNotFoundError, OSError, UnsafePath, ValueError):
            return None

    def _reject_private_data_overlap(self, candidate: Path) -> None:
        candidate_key = os.path.normcase(os.path.abspath(candidate))
        private_key = os.path.normcase(os.path.abspath(self._private_data_root))
        try:
            shared = os.path.commonpath((candidate_key, private_key))
        except ValueError:
            return
        if shared in {candidate_key, private_key}:
            raise ValueError("private_data_root_not_allowed")
