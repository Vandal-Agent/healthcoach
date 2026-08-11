#!/usr/bin/env python3
"""Create verified online backups of HealthCoach SQLite databases."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "backups" / "databases"
DEFAULT_DATABASES = (
    PROJECT_ROOT / "data" / "healthcoach_food.db",
    PROJECT_ROOT / "data" / "healthcoach_memory.db",
)
DEFAULT_RETENTION = 14


def verify_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            detail = result[0] if result else "no result"
            raise RuntimeError(f"integrity_check failed for {path}: {detail}")
        table_count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if table_count < 1:
            raise RuntimeError(f"backup contains no application tables: {path}")
    finally:
        connection.close()


def backup_database(source: Path, backup_dir: Path, timestamp: str) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"{source.stem}.{timestamp}.db"
    temporary = destination.with_suffix(".db.incomplete")
    temporary.unlink(missing_ok=True)

    source_connection = sqlite3.connect(
        f"file:{source}?mode=ro", uri=True, timeout=30
    )
    backup_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()

    try:
        verify_database(temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def prune_backups(
    backup_dir: Path,
    database_stems: Iterable[str],
    retention: int,
) -> list[Path]:
    if retention < 1:
        raise ValueError("retention must be at least 1")

    removed: list[Path] = []
    for stem in database_stems:
        backups = sorted(
            backup_dir.glob(f"{stem}.[0-9]*.db"),
            key=lambda path: path.name,
            reverse=True,
        )
        for old_backup in backups[retention:]:
            old_backup.unlink()
            removed.append(old_backup)
    return removed


def run_backup(
    databases: Iterable[Path] = DEFAULT_DATABASES,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    retention: int = DEFAULT_RETENTION,
    timestamp: str | None = None,
) -> list[Path]:
    database_paths = tuple(Path(path).resolve() for path in databases)
    if not database_paths:
        raise ValueError("at least one database is required")

    stamp = timestamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    created = [
        backup_database(path, Path(backup_dir).resolve(), stamp)
        for path in database_paths
    ]
    prune_backups(
        Path(backup_dir).resolve(),
        (path.stem for path in database_paths),
        retention,
    )
    return created


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create verified HealthCoach SQLite backups."
    )
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--retention", type=int, default=DEFAULT_RETENTION)
    parser.add_argument(
        "--database", action="append", type=Path, dest="databases"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    created = run_backup(
        databases=args.databases or DEFAULT_DATABASES,
        backup_dir=args.backup_dir,
        retention=args.retention,
    )
    for path in created:
        print(f"Verified backup: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
