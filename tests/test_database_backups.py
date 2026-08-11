from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.backup_databases import (
    backup_database,
    prune_backups,
    run_backup,
    verify_database,
)


def create_database(path: Path, value: str = "healthy") -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE sample (sample_id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute("INSERT INTO sample (value) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


class DatabaseBackupTests(unittest.TestCase):
    def test_backup_is_readable_and_preserves_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination_dir = root / "backups"
            create_database(source)

            backup = backup_database(
                source,
                destination_dir,
                "20260811-120000",
            )

            verify_database(backup)
            connection = sqlite3.connect(backup)
            try:
                value = connection.execute(
                    "SELECT value FROM sample"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(value, "healthy")

    def test_missing_source_does_not_create_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                backup_database(
                    root / "missing.db",
                    root / "backups",
                    "20260811-120000",
                )
            self.assertFalse((root / "backups").exists())

    def test_run_backup_copies_all_databases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "food.db"
            second = root / "memory.db"
            create_database(first, "food")
            create_database(second, "memory")

            created = run_backup(
                databases=(first, second),
                backup_dir=root / "backups",
                retention=14,
                timestamp="20260811-120000",
            )

            self.assertEqual(len(created), 2)
            for path in created:
                verify_database(path)

    def test_pruning_keeps_newest_generations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory)
            names = [
                "food.20260809-030000.db",
                "food.20260810-030000.db",
                "food.20260811-030000.db",
            ]
            for name in names:
                (backup_dir / name).touch()

            removed = prune_backups(backup_dir, ("food",), retention=2)

            self.assertEqual(
                [path.name for path in removed],
                ["food.20260809-030000.db"],
            )
            self.assertFalse((backup_dir / names[0]).exists())
            self.assertTrue((backup_dir / names[1]).exists())
            self.assertTrue((backup_dir / names[2]).exists())

    def test_invalid_retention_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                prune_backups(Path(directory), ("food",), retention=0)


if __name__ == "__main__":
    unittest.main()
