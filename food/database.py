from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DATABASE_PATH: Final[Path] = PROJECT_ROOT / "data" / "healthcoach_food.db"
SCHEMA_VERSION: Final[int] = 1


def current_timestamp() -> str:
    """Return a local ISO 8601 timestamp."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_connection(
    database_path: Path = DATABASE_PATH,
) -> sqlite3.Connection:
    """Open the Food database with foreign keys enabled."""
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the Food subsystem tables and indexes."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS foods (
            food_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            brand TEXT,
            restaurant TEXT,
            food_type TEXT NOT NULL DEFAULT 'food'
                CHECK (
                    food_type IN (
                        'food',
                        'drink',
                        'meal',
                        'recipe'
                    )
                ),
            serving_description TEXT NOT NULL,
            serving_amount REAL NOT NULL
                CHECK (serving_amount > 0),
            serving_unit TEXT NOT NULL,

            verification_status TEXT NOT NULL
                CHECK (
                    verification_status IN (
                        'verified',
                        'estimated',
                        'unverified'
                    )
                ),
            verification_source TEXT,
            source_item_id TEXT,
            source_url TEXT,
            last_verified_at TEXT,
            uses_since_verification INTEGER NOT NULL DEFAULT 0
                CHECK (uses_since_verification >= 0),
            next_verification_due TEXT,

            active_nutrition_version_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            UNIQUE (
                canonical_name,
                brand,
                restaurant,
                serving_description
            )
        );

        CREATE TABLE IF NOT EXISTS nutrition_versions (
            nutrition_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL
                CHECK (version_number > 0),

            calories REAL,
            protein_g REAL,
            carbohydrates_g REAL,
            fat_g REAL,
            fiber_g REAL,
            sugar_g REAL,
            sodium_mg REAL,

            serving_amount REAL NOT NULL
                CHECK (serving_amount > 0),
            serving_unit TEXT NOT NULL,

            verification_status TEXT NOT NULL
                CHECK (
                    verification_status IN (
                        'verified',
                        'estimated',
                        'unverified'
                    )
                ),
            verification_source TEXT,
            source_item_id TEXT,
            source_url TEXT,
            verified_at TEXT,
            created_at TEXT NOT NULL,

            FOREIGN KEY (food_id)
                REFERENCES foods (food_id),

            UNIQUE (food_id, version_number)
        );

        CREATE TABLE IF NOT EXISTS food_entries (
            food_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            meal_category TEXT NOT NULL,
            food_id INTEGER NOT NULL,
            nutrition_version_id INTEGER NOT NULL,

            quantity REAL NOT NULL
                CHECK (quantity > 0),

            original_text TEXT,
            logging_source TEXT NOT NULL
                CHECK (
                    logging_source IN (
                        'telegram_ai',
                        'telegram_manual',
                        'loseit',
                        'barcode',
                        'recipe',
                        'manual'
                    )
                ),

            quantity_is_estimated INTEGER NOT NULL DEFAULT 0
                CHECK (quantity_is_estimated IN (0, 1)),
            user_confirmed INTEGER NOT NULL DEFAULT 0
                CHECK (user_confirmed IN (0, 1)),

            calories REAL,
            protein_g REAL,
            carbohydrates_g REAL,
            fat_g REAL,
            fiber_g REAL,
            sugar_g REAL,
            sodium_mg REAL,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            FOREIGN KEY (food_id)
                REFERENCES foods (food_id),

            FOREIGN KEY (nutrition_version_id)
                REFERENCES nutrition_versions (
                    nutrition_version_id
                )
        );

        CREATE TABLE IF NOT EXISTS portion_profiles (
            portion_profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT NOT NULL,
            food_id INTEGER,
            estimated_amount REAL NOT NULL
                CHECK (estimated_amount > 0),
            estimated_unit TEXT NOT NULL,
            user_confirmed INTEGER NOT NULL DEFAULT 1
                CHECK (user_confirmed IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            FOREIGN KEY (food_id)
                REFERENCES foods (food_id),

            UNIQUE (phrase, food_id)
        );

        CREATE INDEX IF NOT EXISTS idx_foods_name
            ON foods (canonical_name);

        CREATE INDEX IF NOT EXISTS idx_foods_brand
            ON foods (brand);

        CREATE INDEX IF NOT EXISTS idx_foods_restaurant
            ON foods (restaurant);

        CREATE INDEX IF NOT EXISTS idx_food_entries_date
            ON food_entries (entry_date);

        CREATE INDEX IF NOT EXISTS idx_food_entries_meal
            ON food_entries (meal_category);

        CREATE INDEX IF NOT EXISTS idx_food_entries_source
            ON food_entries (logging_source);

        CREATE INDEX IF NOT EXISTS idx_nutrition_food_id
            ON nutrition_versions (food_id);

        CREATE INDEX IF NOT EXISTS idx_portion_phrase
            ON portion_profiles (phrase);
        """
    )


def seed_schema_version(connection: sqlite3.Connection) -> None:
    """Record the initial Food schema version."""
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
            "Initial HealthCoach Food database schema",
        ),
    )


def validate_database(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    """Return a summary proving initialization succeeded."""
    table_rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()

    schema_row = connection.execute(
        """
        SELECT version, applied_at, description
        FROM schema_version
        ORDER BY version DESC
        LIMIT 1
        """
    ).fetchone()

    return {
        "database_path": str(DATABASE_PATH),
        "tables": [row["name"] for row in table_rows],
        "schema_version": (
            dict(schema_row)
            if schema_row
            else None
        ),
    }


def initialize_database(
    database_path: Path = DATABASE_PATH,
) -> dict[str, object]:
    """Create and validate the Food database."""
    with get_connection(database_path) as connection:
        create_schema(connection)
        seed_schema_version(connection)
        connection.commit()

        return validate_database(connection)


def main() -> None:
    """Initialize the Food database and print the result."""
    result = initialize_database()

    print("HealthCoach Food database initialized successfully.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()