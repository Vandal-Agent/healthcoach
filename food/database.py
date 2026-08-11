from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DATABASE_PATH: Final[Path] = PROJECT_ROOT / "data" / "healthcoach_food.db"

INITIAL_SCHEMA_VERSION: Final[int] = 1
SCHEMA_VERSION: Final[int] = 4


class ClosingConnection(sqlite3.Connection):
    """Close the database when a with block finishes."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def current_timestamp() -> str:
    """Return a local ISO 8601 timestamp."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_connection(
    database_path: Path = DATABASE_PATH,
) -> sqlite3.Connection:
    """Open the Food database with foreign keys enabled."""
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        database_path,
        factory=ClosingConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def normalize_key_part(value: str | None) -> str:
    """
    Convert text into a stable search-key component.

    Examples:
        "McDonald's" -> "mcdonalds"
        "Big Mac" -> "big_mac"
        "20 oz." -> "20_oz"
    """
    if value is None:
        return ""

    cleaned = value.strip().lower()
    cleaned = cleaned.replace("’", "").replace("'", "")
    cleaned = cleaned.replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)

    return cleaned.strip("_")


def build_search_key(
    *,
    canonical_name: str,
    serving_description: str | None = None,
    brand: str | None = None,
    restaurant: str | None = None,
) -> str:
    """
    Build a deterministic Food search key.

    Restaurant is preferred over brand because restaurant menu items
    generally belong to the restaurant namespace.

    Example:
        mcdonalds|big_mac|standard
    """
    namespace = (
        normalize_key_part(restaurant)
        or normalize_key_part(brand)
        or "generic"
    )

    food_name = normalize_key_part(canonical_name)

    if not food_name:
        raise ValueError(
            "canonical_name must produce a valid search-key component."
        )

    serving = normalize_key_part(serving_description) or "standard"

    return f"{namespace}|{food_name}|{serving}"


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    """Return True when a table exists."""
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def column_exists(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    """Return True when a table contains the requested column."""
    if not table_exists(connection, table_name):
        return False

    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return any(row["name"] == column_name for row in rows)


def get_schema_version(
    connection: sqlite3.Connection,
) -> int:
    """Return the highest installed Food schema version."""
    if not table_exists(connection, "schema_version"):
        return 0

    row = connection.execute(
        """
        SELECT MAX(version) AS version
        FROM schema_version
        """
    ).fetchone()

    if row is None or row["version"] is None:
        return 0

    return int(row["version"])


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the current Food subsystem schema."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS foods (
            food_id INTEGER PRIMARY KEY AUTOINCREMENT,

            search_key TEXT NOT NULL UNIQUE,
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

        CREATE TABLE IF NOT EXISTS unresolved_foods (
            unresolved_food_id INTEGER PRIMARY KEY AUTOINCREMENT,

            entry_date TEXT NOT NULL,
            meal_category TEXT,

            original_text TEXT NOT NULL,

            food_name TEXT,
            brand TEXT,
            restaurant TEXT,
            size TEXT,
            quantity REAL,
            quantity_description TEXT,

            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (
                    status IN (
                        'pending',
                        'resolved',
                        'cancelled'
                    )
                ),

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_unresolved_foods_status
            ON unresolved_foods (status);

        CREATE INDEX IF NOT EXISTS idx_unresolved_foods_date
            ON unresolved_foods (entry_date);

        CREATE TABLE IF NOT EXISTS food_favorites (
            food_favorite_id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_id INTEGER NOT NULL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            meal_category TEXT NOT NULL
                CHECK (
                    meal_category IN (
                        'before breakfast',
                        'breakfast',
                        'school snack',
                        'lunch',
                        'afternoon snack',
                        'dinner',
                        'dessert'
                    )
                ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            FOREIGN KEY (food_id)
                REFERENCES foods (food_id)
                ON DELETE CASCADE,

            UNIQUE (food_id, quantity, meal_category)
        );

        CREATE INDEX IF NOT EXISTS idx_food_favorites_food_id
            ON food_favorites (food_id);

        CREATE TABLE IF NOT EXISTS food_aliases (
            food_alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_id INTEGER NOT NULL,

            alias_text TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            FOREIGN KEY (food_id)
                REFERENCES foods (food_id)
                ON DELETE CASCADE,

            UNIQUE (food_id, normalized_alias)
        );

        CREATE INDEX IF NOT EXISTS idx_foods_search_key
            ON foods (search_key);

        CREATE INDEX IF NOT EXISTS idx_foods_name
            ON foods (canonical_name);

        CREATE INDEX IF NOT EXISTS idx_foods_brand
            ON foods (brand);

        CREATE INDEX IF NOT EXISTS idx_foods_restaurant
            ON foods (restaurant);

        CREATE INDEX IF NOT EXISTS idx_food_aliases_normalized
            ON food_aliases (normalized_alias);

        CREATE INDEX IF NOT EXISTS idx_food_aliases_food_id
            ON food_aliases (food_id);

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


def record_schema_version(
    connection: sqlite3.Connection,
    *,
    version: int,
    description: str,
) -> None:
    """Record one applied Food schema version."""
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
            version,
            current_timestamp(),
            description,
        ),
    )


def create_initial_database(
    connection: sqlite3.Connection,
) -> None:
    """Create a new database directly at the current schema."""
    create_schema(connection)

    record_schema_version(
        connection,
        version=INITIAL_SCHEMA_VERSION,
        description="Initial HealthCoach Food database schema",
    )

    record_schema_version(
        connection,
        version=2,
        description=(
            "Add normalized Food search keys and Food aliases"
        ),
    )

    record_schema_version(
        connection,
        version=3,
        description=(
            "Add persistent unresolved Food queue"
        ),
    )

    record_schema_version(
        connection,
        version=SCHEMA_VERSION,
        description="Add saved Food favorites",
    )


def create_alias_schema(
    connection: sqlite3.Connection,
) -> None:
    """Create the Food alias table and indexes."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS food_aliases (
            food_alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_id INTEGER NOT NULL,

            alias_text TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            FOREIGN KEY (food_id)
                REFERENCES foods (food_id)
                ON DELETE CASCADE,

            UNIQUE (food_id, normalized_alias)
        );

        CREATE INDEX IF NOT EXISTS idx_food_aliases_normalized
            ON food_aliases (normalized_alias);

        CREATE INDEX IF NOT EXISTS idx_food_aliases_food_id
            ON food_aliases (food_id);
        """
    )


def create_unresolved_food_schema(
    connection: sqlite3.Connection,
) -> None:
    """Create the persistent unresolved Food queue."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS unresolved_foods (
            unresolved_food_id INTEGER PRIMARY KEY AUTOINCREMENT,

            entry_date TEXT NOT NULL,
            meal_category TEXT,

            original_text TEXT NOT NULL,

            food_name TEXT,
            brand TEXT,
            restaurant TEXT,
            size TEXT,
            quantity REAL,
            quantity_description TEXT,

            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (
                    status IN (
                        'pending',
                        'resolved',
                        'cancelled'
                    )
                ),

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_unresolved_foods_status
            ON unresolved_foods (status);

        CREATE INDEX IF NOT EXISTS idx_unresolved_foods_date
            ON unresolved_foods (entry_date);
        """
    )


def create_food_favorites_schema(
    connection: sqlite3.Connection,
) -> None:
    """Create saved Food favorites and supporting indexes."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS food_favorites (
            food_favorite_id INTEGER PRIMARY KEY AUTOINCREMENT,
            food_id INTEGER NOT NULL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            meal_category TEXT NOT NULL
                CHECK (
                    meal_category IN (
                        'before breakfast',
                        'breakfast',
                        'school snack',
                        'lunch',
                        'afternoon snack',
                        'dinner',
                        'dessert'
                    )
                ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            FOREIGN KEY (food_id)
                REFERENCES foods (food_id)
                ON DELETE CASCADE,

            UNIQUE (food_id, quantity, meal_category)
        );

        CREATE INDEX IF NOT EXISTS idx_food_favorites_food_id
            ON food_favorites (food_id);
        """
    )


def migrate_version_1_to_2(
    connection: sqlite3.Connection,
) -> None:
    """
    Add stable Food identity and alias support.

    Existing foods are assigned deterministic search keys. If two
    legacy records generate the same key, the Food ID is appended so
    no data is lost and every key remains unique.
    """
    if not column_exists(connection, "foods", "search_key"):
        connection.execute(
            """
            ALTER TABLE foods
            ADD COLUMN search_key TEXT
            """
        )

    rows = connection.execute(
        """
        SELECT
            food_id,
            canonical_name,
            serving_description,
            brand,
            restaurant,
            search_key
        FROM foods
        ORDER BY food_id
        """
    ).fetchall()

    used_keys: set[str] = {
        row["search_key"]
        for row in rows
        if row["search_key"]
    }

    for row in rows:
        if row["search_key"]:
            continue

        base_key = build_search_key(
            canonical_name=row["canonical_name"],
            serving_description=row["serving_description"],
            brand=row["brand"],
            restaurant=row["restaurant"],
        )

        search_key = base_key

        if search_key in used_keys:
            search_key = f"{base_key}|legacy_{row['food_id']}"

        used_keys.add(search_key)

        connection.execute(
            """
            UPDATE foods
            SET search_key = ?
            WHERE food_id = ?
            """,
            (
                search_key,
                row["food_id"],
            ),
        )

    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_foods_search_key
        ON foods (search_key)
        """
    )

    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_foods_search_key_insert
        BEFORE INSERT ON foods
        FOR EACH ROW
        WHEN NEW.search_key IS NULL
          OR trim(NEW.search_key) = ''
        BEGIN
            SELECT RAISE(
                ABORT,
                'foods.search_key is required'
            );
        END;

        CREATE TRIGGER IF NOT EXISTS trg_foods_search_key_update
        BEFORE UPDATE OF search_key ON foods
        FOR EACH ROW
        WHEN NEW.search_key IS NULL
          OR trim(NEW.search_key) = ''
        BEGIN
            SELECT RAISE(
                ABORT,
                'foods.search_key is required'
            );
        END;
        """
    )

    create_alias_schema(connection)

    record_schema_version(
        connection,
        version=2,
        description=(
            "Add normalized Food search keys and Food aliases"
        ),
    )


def migrate_version_2_to_3(
    connection: sqlite3.Connection,
) -> None:
    """Add the persistent unresolved Food queue."""
    create_unresolved_food_schema(connection)

    record_schema_version(
        connection,
        version=3,
        description="Add persistent unresolved Food queue",
    )


def migrate_version_3_to_4(
    connection: sqlite3.Connection,
) -> None:
    """Add saved Food favorites."""
    create_food_favorites_schema(connection)

    record_schema_version(
        connection,
        version=4,
        description="Add saved Food favorites",
    )


def apply_migrations(
    connection: sqlite3.Connection,
) -> None:
    """Bring an existing Food database to the current schema."""
    version = get_schema_version(connection)

    if version == 0:
        create_initial_database(connection)
        return

    if version < 2:
        migrate_version_1_to_2(connection)
        version = 2

    if version < 3:
        migrate_version_2_to_3(connection)
        version = 3

    if version < 4:
        migrate_version_3_to_4(connection)
        version = 4

    if version > SCHEMA_VERSION:
        raise RuntimeError(
            "The Food database schema is newer than this code supports. "
            f"Database version: {version}. "
            f"Supported version: {SCHEMA_VERSION}."
        )

    create_alias_schema(connection)
    create_unresolved_food_schema(connection)
    create_food_favorites_schema(connection)


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

    required_tables = {
        "schema_version",
        "foods",
        "nutrition_versions",
        "food_entries",
        "portion_profiles",
        "food_aliases",
        "unresolved_foods",
        "food_favorites",
    }

    actual_tables = {
        row["name"]
        for row in table_rows
    }

    missing_tables = sorted(required_tables - actual_tables)

    if missing_tables:
        raise RuntimeError(
            "Food database initialization is incomplete. "
            "Missing tables: "
            + ", ".join(missing_tables)
        )

    if not column_exists(connection, "foods", "search_key"):
        raise RuntimeError(
            "Food database initialization is incomplete. "
            "foods.search_key is missing."
        )

    installed_version = (
        int(schema_row["version"])
        if schema_row
        else 0
    )

    if installed_version != SCHEMA_VERSION:
        raise RuntimeError(
            "Food database schema version mismatch. "
            f"Installed: {installed_version}. "
            f"Expected: {SCHEMA_VERSION}."
        )

    return {
        "database_path": str(DATABASE_PATH),
        "tables": sorted(actual_tables),
        "schema_version": (
            dict(schema_row)
            if schema_row
            else None
        ),
    }


def initialize_database(
    database_path: Path = DATABASE_PATH,
) -> dict[str, object]:
    """Create, migrate, and validate the Food database."""
    with get_connection(database_path) as connection:
        apply_migrations(connection)
        connection.commit()

        return validate_database(connection)


def save_food_alias(
    *,
    food_id: int,
    alias_text: str,
) -> dict[str, object]:
    """Create or refresh one deterministic alias for a saved Food."""
    initialize_database()

    cleaned_alias = alias_text.strip()

    if not cleaned_alias:
        raise ValueError("alias_text is required.")

    normalized_alias = normalize_key_part(cleaned_alias)

    if not normalized_alias:
        raise ValueError(
            "alias_text did not contain a usable alias."
        )

    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        food = connection.execute(
            """
            SELECT food_id
            FROM foods
            WHERE food_id = ?
            """,
            (int(food_id),),
        ).fetchone()

        if food is None:
            raise ValueError(
                f"Food not found: {food_id}"
            )

        connection.execute(
            """
            INSERT INTO food_aliases (
                food_id,
                alias_text,
                normalized_alias,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(food_id, normalized_alias)
            DO UPDATE SET
                alias_text = excluded.alias_text,
                updated_at = excluded.updated_at
            """,
            (
                int(food_id),
                cleaned_alias,
                normalized_alias,
                timestamp,
                timestamp,
            ),
        )

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM food_aliases
            WHERE food_id = ?
              AND normalized_alias = ?
            LIMIT 1
            """,
            (
                int(food_id),
                normalized_alias,
            ),
        ).fetchone()

    return dict(row)


def get_portion_profile(
    *,
    food_id: int,
    phrase: str,
) -> dict[str, object] | None:
    """Return one saved portion profile for a Food and phrase."""
    initialize_database()

    normalized_phrase = phrase.strip().lower()

    if not normalized_phrase:
        raise ValueError("phrase is required.")

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM portion_profiles
            WHERE food_id = ?
              AND lower(phrase) = lower(?)
            LIMIT 1
            """,
            (
                int(food_id),
                normalized_phrase,
            ),
        ).fetchone()

    return dict(row) if row else None


def save_portion_profile(
    *,
    food_id: int,
    phrase: str,
    estimated_amount: float,
    estimated_unit: str,
    user_confirmed: bool = True,
) -> dict[str, object]:
    """Create or update one user-confirmed portion profile."""
    initialize_database()

    normalized_phrase = phrase.strip().lower()
    unit = estimated_unit.strip()

    if not normalized_phrase:
        raise ValueError("phrase is required.")

    if estimated_amount <= 0:
        raise ValueError(
            "estimated_amount must be greater than zero."
        )

    if not unit:
        raise ValueError("estimated_unit is required.")

    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO portion_profiles (
                phrase,
                food_id,
                estimated_amount,
                estimated_unit,
                user_confirmed,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (phrase, food_id)
            DO UPDATE SET
                estimated_amount = excluded.estimated_amount,
                estimated_unit = excluded.estimated_unit,
                user_confirmed = excluded.user_confirmed,
                updated_at = excluded.updated_at
            """,
            (
                normalized_phrase,
                int(food_id),
                float(estimated_amount),
                unit,
                1 if user_confirmed else 0,
                timestamp,
                timestamp,
            ),
        )

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM portion_profiles
            WHERE food_id = ?
              AND lower(phrase) = lower(?)
            LIMIT 1
            """,
            (
                int(food_id),
                normalized_phrase,
            ),
        ).fetchone()

    if row is None:
        raise RuntimeError(
            "Portion profile could not be read after saving."
        )

    return dict(row)


def main() -> None:
    """Initialize the Food database and print the result."""
    result = initialize_database()

    print("HealthCoach Food database initialized successfully.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()


def save_unresolved_food(
    *,
    entry_date: str,
    original_text: str,
    meal_category: str | None = None,
    food_name: str | None = None,
    brand: str | None = None,
    restaurant: str | None = None,
    size: str | None = None,
    quantity: float | None = None,
    quantity_description: str | None = None,
) -> dict[str, object]:
    """Save one unresolved Food for later nutrition completion."""
    initialize_database()

    if not entry_date.strip():
        raise ValueError("entry_date is required.")

    if not original_text.strip():
        raise ValueError("original_text is required.")

    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO unresolved_foods (
                entry_date,
                meal_category,
                original_text,
                food_name,
                brand,
                restaurant,
                size,
                quantity,
                quantity_description,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                entry_date.strip(),
                meal_category.strip() if meal_category else None,
                original_text.strip(),
                food_name.strip() if food_name else None,
                brand.strip() if brand else None,
                restaurant.strip() if restaurant else None,
                size.strip() if size else None,
                float(quantity) if quantity is not None else None,
                (
                    quantity_description.strip()
                    if quantity_description
                    else None
                ),
                timestamp,
                timestamp,
            ),
        )

        unresolved_food_id = cursor.lastrowid
        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM unresolved_foods
            WHERE unresolved_food_id = ?
            """,
            (unresolved_food_id,),
        ).fetchone()

    return dict(row)


def get_pending_unresolved_foods() -> list[dict[str, object]]:
    """Return unresolved Foods waiting for nutrition completion."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM unresolved_foods
            WHERE status = 'pending'
            ORDER BY entry_date, unresolved_food_id
            """
        ).fetchall()

    return [dict(row) for row in rows]


def get_unresolved_food(
    unresolved_food_id: int,
) -> dict[str, object] | None:
    """Return one unresolved Food queue record by ID."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM unresolved_foods
            WHERE unresolved_food_id = ?
            """,
            (int(unresolved_food_id),),
        ).fetchone()

    return dict(row) if row is not None else None


def set_unresolved_food_status(
    unresolved_food_id: int,
    *,
    status: str,
) -> dict[str, object]:
    """Set one unresolved Food queue record's lifecycle status."""
    initialize_database()

    normalized_status = status.strip().lower()

    if normalized_status not in {
        "pending",
        "resolved",
        "cancelled",
    }:
        raise ValueError("Unsupported unresolved Food status.")

    timestamp = current_timestamp()
    resolved_at = (
        timestamp if normalized_status == "resolved" else None
    )

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            UPDATE unresolved_foods
            SET status = ?,
                updated_at = ?,
                resolved_at = ?
            WHERE unresolved_food_id = ?
            """,
            (
                normalized_status,
                timestamp,
                resolved_at,
                int(unresolved_food_id),
            ),
        )

        if cursor.rowcount != 1:
            raise ValueError("Unresolved Food was not found.")

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM unresolved_foods
            WHERE unresolved_food_id = ?
            """,
            (int(unresolved_food_id),),
        ).fetchone()

    return dict(row)


def update_unresolved_food_details(
    unresolved_food_id: int,
    *,
    original_text: str,
    meal_category: str | None,
    food_name: str | None,
    brand: str | None,
    restaurant: str | None,
    size: str | None,
    quantity: float | None,
    quantity_description: str | None,
) -> dict[str, object]:
    """Replace the interpreted details for one pending queue item."""
    initialize_database()

    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            UPDATE unresolved_foods
            SET original_text = ?,
                meal_category = ?,
                food_name = ?,
                brand = ?,
                restaurant = ?,
                size = ?,
                quantity = ?,
                quantity_description = ?,
                updated_at = ?
            WHERE unresolved_food_id = ?
              AND status = 'pending'
            """,
            (
                original_text.strip(),
                meal_category.strip() if meal_category else None,
                food_name.strip() if food_name else None,
                brand.strip() if brand else None,
                restaurant.strip() if restaurant else None,
                size.strip() if size else None,
                float(quantity) if quantity is not None else None,
                (
                    quantity_description.strip()
                    if quantity_description
                    else None
                ),
                timestamp,
                int(unresolved_food_id),
            ),
        )

        if cursor.rowcount != 1:
            raise ValueError("Pending unresolved Food was not found.")

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM unresolved_foods
            WHERE unresolved_food_id = ?
            """,
            (int(unresolved_food_id),),
        ).fetchone()

    return dict(row)
