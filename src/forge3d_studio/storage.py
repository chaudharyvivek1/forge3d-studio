"""Asset storage.

Local filesystem behind a tiny interface, so an S3-compatible backend is a
substitution rather than a rewrite. Paths stored in the database are **relative**
to the storage root — absolute paths in a database are how you get an
unmigratable install.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import get_settings


def _root() -> Path:
    root = Path(get_settings().storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def store(user_id: str, asset_id: str, source: str | Path, kind: str) -> str:
    """Copy a produced file into storage and return its relative path."""
    src = Path(source)
    if not src.is_file():
        raise FileNotFoundError(f"nothing to store at {src}")
    suffix = src.suffix or f".{kind}"
    relative = Path(user_id) / asset_id / f"{kind}{suffix}"
    target = _root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    return str(relative)


def resolve(relative: str) -> Path:
    """Absolute path for a stored asset, refusing to escape the root."""
    root = _root().resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("path escapes the storage root")
    return candidate


def size_of(relative: str) -> int:
    path = resolve(relative)
    return path.stat().st_size if path.exists() else 0


def delete_tree(user_id: str, asset_id: str) -> None:
    target = _root() / user_id / asset_id
    shutil.rmtree(target, ignore_errors=True)
