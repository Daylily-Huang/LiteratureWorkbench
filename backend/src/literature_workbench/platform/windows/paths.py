"""Win32 I/O paths without changing the canonical paths stored in the library."""
from __future__ import annotations

import os
from pathlib import Path


def ordinary_path(path: Path) -> Path:
    value = str(path)
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return Path(os.path.abspath(value))


def io_path(path: Path) -> Path:
    value = str(ordinary_path(path))
    if os.name == 'nt':
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)
