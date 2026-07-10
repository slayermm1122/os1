from __future__ import annotations

import os
from pathlib import Path


def prepare_private_directory(path: Path, *, manage_existing: bool) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and (manage_existing or not existed):
        path.chmod(0o700)


def secure_private_files(paths: tuple[Path, ...]) -> None:
    if os.name != "posix":
        return
    for path in paths:
        if path.exists():
            path.chmod(0o600)


def sqlite_files(db_path: Path) -> tuple[Path, ...]:
    return (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )
