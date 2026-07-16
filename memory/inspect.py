from __future__ import annotations

import json

from memory.database import DATABASE_PATH, get_connection, initialize_database


def inspect_memory() -> dict[str, object]:
    """Return a read-only summary of HealthCoach Memory."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        status_rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM cases
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()

        type_rows = connection.execute(
            """
            SELECT case_type, COUNT(*) AS count
            FROM cases
            GROUP BY case_type
            ORDER BY case_type
            """
        ).fetchall()

        recent_rows = connection.execute(
            """
            SELECT
                case_id,
                case_date,
                status,
                case_type,
                observation_code,
                recommendation_code,
                successful,
                created_at,
                closed_at
            FROM cases
            ORDER BY case_id DESC
            LIMIT 10
            """
        ).fetchall()

        recommendation_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM recommendations
            WHERE enabled = 1
            """
        ).fetchone()[0]

    return {
        "database_path": str(DATABASE_PATH),
        "case_counts_by_status": {
            row["status"]: row["count"] for row in status_rows
        },
        "case_counts_by_type": {
            row["case_type"]: row["count"] for row in type_rows
        },
        "enabled_recommendations": recommendation_count,
        "recent_cases": [dict(row) for row in recent_rows],
    }


def main() -> None:
    """Print the current Memory summary."""
    print(json.dumps(inspect_memory(), indent=2))


if __name__ == "__main__":
    main()
