from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DATABASE_PATH: Final[Path] = PROJECT_ROOT / "data" / "healthcoach_memory.db"

SCHEMA_VERSION: Final[int] = 1
LOGIC_VERSION: Final[str] = "memory_v1_rules_1"


def current_timestamp() -> str:
    """Return a local ISO 8601 timestamp with timezone information."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_connection(database_path: Path = DATABASE_PATH) -> sqlite3.Connection:
    """Open the SQLite database and enable foreign-key enforcement."""
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the Memory V1 tables and indexes if they do not exist."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            recommendation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_code TEXT NOT NULL UNIQUE,
            case_type TEXT NOT NULL,
            recommendation_text TEXT NOT NULL,
            recommendation_reason TEXT,
            expected_metric TEXT,
            default_expected_threshold REAL,
            enabled INTEGER NOT NULL DEFAULT 1
                CHECK (enabled IN (0, 1)),
            priority_rank INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cases (
            case_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            case_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK (
                    status IN (
                        'open',
                        'evaluated',
                        'closed',
                        'cancelled'
                    )
                ),

            case_type TEXT NOT NULL,
            priority TEXT NOT NULL
                CHECK (priority IN ('low', 'medium', 'high')),
            tags_json TEXT,

            observation_code TEXT NOT NULL,
            observation TEXT NOT NULL,
            supporting_data_json TEXT NOT NULL,
            data_confidence REAL NOT NULL
                CHECK (
                    data_confidence >= 0.0
                    AND data_confidence <= 1.0
                ),

            meal_category TEXT,
            estimated_window_start TEXT,
            estimated_window_end TEXT,

            recommendation_id INTEGER,
            recommendation_code TEXT NOT NULL,
            recommendation_text TEXT NOT NULL,
            recommendation_reason TEXT,
            expected_result TEXT NOT NULL,
            expected_metric TEXT,
            expected_threshold REAL,
            evaluation_due_at TEXT NOT NULL,

            followed_status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (
                    followed_status IN (
                        'yes',
                        'no',
                        'partial',
                        'likely',
                        'unknown'
                    )
                ),
            follow_through_evidence TEXT,
            follow_through_confidence REAL
                CHECK (
                    follow_through_confidence IS NULL
                    OR (
                        follow_through_confidence >= 0.0
                        AND follow_through_confidence <= 1.0
                    )
                ),
            actual_result TEXT,
            actual_value REAL,
            successful INTEGER
                CHECK (
                    successful IS NULL
                    OR successful IN (0, 1)
                ),
            outcome_confidence REAL
                CHECK (
                    outcome_confidence IS NULL
                    OR (
                        outcome_confidence >= 0.0
                        AND outcome_confidence <= 1.0
                    )
                ),
            evaluated_at TEXT,
            closed_at TEXT,

            observation_source TEXT NOT NULL DEFAULT 'rules'
                CHECK (
                    observation_source IN (
                        'rules',
                        'ai',
                        'hybrid',
                        'user'
                    )
                ),
            recommendation_source TEXT NOT NULL DEFAULT 'rules'
                CHECK (
                    recommendation_source IN (
                        'rules',
                        'ai',
                        'hybrid',
                        'user'
                    )
                ),
            evaluator_source TEXT
                CHECK (
                    evaluator_source IS NULL
                    OR evaluator_source IN (
                        'rules',
                        'ai',
                        'hybrid',
                        'user'
                    )
                ),
            logic_version TEXT NOT NULL,
            model_name TEXT,
            prompt_version TEXT,

            notes TEXT,
            created_by TEXT NOT NULL DEFAULT 'healthcoach',
            updated_at TEXT NOT NULL,

            FOREIGN KEY (recommendation_id)
                REFERENCES recommendations (recommendation_id)
        );

        CREATE INDEX IF NOT EXISTS idx_cases_case_date
            ON cases (case_date);

        CREATE INDEX IF NOT EXISTS idx_cases_status
            ON cases (status);

        CREATE INDEX IF NOT EXISTS idx_cases_type
            ON cases (case_type);

        CREATE INDEX IF NOT EXISTS idx_cases_recommendation_code
            ON cases (recommendation_code);

        CREATE INDEX IF NOT EXISTS idx_cases_evaluation_due_at
            ON cases (evaluation_due_at);
        """
    )


def seed_schema_version(connection: sqlite3.Connection) -> None:
    """Insert the initial schema-version record if it is missing."""
    connection.execute(
        """
        INSERT OR IGNORE INTO schema_version (
            version,
            applied_at,
            description
        )
        VALUES (?, ?, ?)
        """,
        (
            SCHEMA_VERSION,
            current_timestamp(),
            "Initial HealthCoach Memory schema",
        ),
    )


def seed_recommendations(connection: sqlite3.Connection) -> None:
    """Insert the initial approved recommendation if it is missing."""
    timestamp = current_timestamp()

    connection.execute(
        """
        INSERT OR IGNORE INTO recommendations (
            recommendation_code,
            case_type,
            recommendation_text,
            recommendation_reason,
            expected_metric,
            default_expected_threshold,
            enabled,
            priority_rank,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "update_missing_data",
            "missing_data",
            "Update your Lose It totals so I can check today's goals.",
            (
                "Current nutrition data is missing, so HealthCoach "
                "cannot evaluate progress reliably."
            ),
            "dietary_cals",
            None,
            1,
            10,
            timestamp,
            timestamp,
        ),
    )


def validate_database(connection: sqlite3.Connection) -> dict[str, object]:
    """Return a summary proving that the database initialized correctly."""
    table_rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()

    tables = [row["name"] for row in table_rows]

    schema_row = connection.execute(
        """
        SELECT version, applied_at, description
        FROM schema_version
        ORDER BY version DESC
        LIMIT 1
        """
    ).fetchone()

    recommendation_row = connection.execute(
        """
        SELECT
            recommendation_code,
            case_type,
            recommendation_text,
            enabled
        FROM recommendations
        WHERE recommendation_code = ?
        """,
        ("update_missing_data",),
    ).fetchone()

    return {
        "database_path": str(DATABASE_PATH),
        "tables": tables,
        "schema_version": dict(schema_row) if schema_row else None,
        "initial_recommendation": (
            dict(recommendation_row) if recommendation_row else None
        ),
    }


def initialize_database(
    database_path: Path = DATABASE_PATH,
) -> dict[str, object]:
    """Create, seed, and validate the HealthCoach Memory database."""
    with get_connection(database_path) as connection:
        create_schema(connection)
        seed_schema_version(connection)
        seed_recommendations(connection)
        connection.commit()

        return validate_database(connection)


def main() -> None:
    """Initialize the database and print a readable validation summary."""
    result = initialize_database()

    print("HealthCoach Memory database initialized successfully.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()