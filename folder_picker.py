"""Browse recording locations on the machine running the scanner."""

import os
from pathlib import Path


FOLDER_LIMIT = 500
ENTRY_LIMIT = 5_000


def _directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Enter an absolute folder path on the scanner server.")
    try:
        resolved = path.resolve(strict=True)
    except RuntimeError as exc:
        raise ValueError("The server folder contains a circular symbolic link.") from exc
    if not resolved.is_dir():
        raise NotADirectoryError(f"This server path is not a folder: {resolved}")
    return resolved


def browse_folders(path: Path) -> dict:
    """Return real subfolders and the canonical server path for the web picker."""
    directory = _directory(path)
    folders = []
    truncated = False
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= ENTRY_LIMIT:
                truncated = True
                break
            if entry.is_dir(follow_symlinks=False):
                if len(folders) >= FOLDER_LIMIT:
                    truncated = True
                    break
                folders.append(entry.name)
    return {
        "path": str(directory),
        "parent": str(directory.parent) if directory != directory.parent else None,
        "folders": sorted(folders, key=lambda name: (name.casefold(), name)),
        "writable": os.access(directory, os.W_OK | os.X_OK),
        "truncated": truncated,
    }


def select_folder(path: Path) -> Path:
    """Validate a user-selected server folder before setting it as a recording location."""
    directory = _directory(path)
    if not os.access(directory, os.W_OK | os.X_OK):
        raise PermissionError(f"The scanner cannot write to the selected folder: {directory}")
    return directory
