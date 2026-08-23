from __future__ import annotations

from typing import Any


def sanitize_optional_heart_healthy_picks(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """
    Preserve at most one explained Heart-Healthy Pick.

    A recommendation set may have no designation when the available
    evidence is insufficient. Ambiguous or unexplained designations are
    removed from the complete set rather than guessed or repaired.
    """
    cleaned = [dict(candidate) for candidate in candidates]
    selected = [
        candidate
        for candidate in cleaned
        if bool(candidate.get("heart_healthy_pick"))
    ]

    status = "valid"
    if not selected:
        status = "none"
    elif len(selected) > 1:
        status = "multiple"
    elif not str(
        selected[0].get("heart_healthy_reason") or ""
    ).strip():
        status = "missing_reason"

    if status != "valid":
        for candidate in cleaned:
            candidate["heart_healthy_pick"] = False
            candidate["heart_healthy_reason"] = None
        return cleaned, status

    selected_candidate = selected[0]
    selected_candidate["heart_healthy_reason"] = str(
        selected_candidate["heart_healthy_reason"]
    ).strip()
    for candidate in cleaned:
        if candidate is not selected_candidate:
            candidate["heart_healthy_pick"] = False
            candidate["heart_healthy_reason"] = None
    return cleaned, status
