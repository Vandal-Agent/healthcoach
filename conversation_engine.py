from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
CONVERSATION_FILE = PROJECT_ROOT / "data" / "conversations.json"
CONVERSATION_TIMEOUT_HOURS = 24


def current_timestamp() -> str:
    """Return a local ISO 8601 timestamp."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp."""
    return datetime.fromisoformat(value)


def load_conversations() -> dict[str, Any]:
    """Load persistent conversation data."""
    if not CONVERSATION_FILE.exists():
        return {"conversations": {}}

    try:
        with CONVERSATION_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {"conversations": {}}

    if not isinstance(data, dict):
        return {"conversations": {}}

    conversations = data.get("conversations")

    if not isinstance(conversations, dict):
        return {"conversations": {}}

    return {"conversations": conversations}


def save_conversations(data: dict[str, Any]) -> None:
    """Save conversation data using an atomic file replacement."""
    CONVERSATION_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary_file = CONVERSATION_FILE.with_suffix(".json.tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)

    os.replace(temporary_file, CONVERSATION_FILE)


def conversation_is_expired(
    conversation: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Return True when a conversation has been inactive for 24 hours."""
    reference_time = now or datetime.now().astimezone()
    updated_at = conversation.get("updated_at")

    if not updated_at:
        return True

    try:
        last_update = parse_timestamp(updated_at)
    except (TypeError, ValueError):
        return True

    expiration_time = last_update + timedelta(
        hours=CONVERSATION_TIMEOUT_HOURS
    )

    return reference_time >= expiration_time


def get_active_conversation(
    chat_id: int | str,
) -> dict[str, Any] | None:
    """Return one active, unexpired conversation for a Telegram chat."""
    data = load_conversations()
    key = str(chat_id)
    conversation = data["conversations"].get(key)

    if conversation is None:
        return None

    if conversation_is_expired(conversation):
        del data["conversations"][key]
        save_conversations(data)
        return None

    return conversation


def start_conversation(
    *,
    chat_id: int | str,
    conversation_type: str,
    current_step: str,
    known_data: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
    original_message: str | None = None,
) -> dict[str, Any]:
    """Start or replace the active conversation for one chat."""
    if not conversation_type.strip():
        raise ValueError("conversation_type is required.")

    if not current_step.strip():
        raise ValueError("current_step is required.")

    timestamp = current_timestamp()

    conversation = {
        "conversation_type": conversation_type,
        "status": "active",
        "current_step": current_step,
        "known_data": known_data or {},
        "missing_fields": missing_fields or [],
        "original_message": original_message,
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    data = load_conversations()
    data["conversations"][str(chat_id)] = conversation
    save_conversations(data)

    return conversation


def update_conversation(
    *,
    chat_id: int | str,
    current_step: str | None = None,
    known_data: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Update fields on an existing active conversation."""
    data = load_conversations()
    key = str(chat_id)
    conversation = data["conversations"].get(key)

    if conversation is None:
        raise ValueError("No active conversation exists.")

    if conversation_is_expired(conversation):
        del data["conversations"][key]
        save_conversations(data)
        raise ValueError("The conversation has expired.")

    if current_step is not None:
        conversation["current_step"] = current_step

    if known_data is not None:
        conversation["known_data"].update(known_data)

    if missing_fields is not None:
        conversation["missing_fields"] = missing_fields

    if status is not None:
        conversation["status"] = status

    conversation["updated_at"] = current_timestamp()

    save_conversations(data)

    return conversation


def cancel_conversation(chat_id: int | str) -> bool:
    """Remove the active conversation for one chat."""
    data = load_conversations()
    key = str(chat_id)

    if key not in data["conversations"]:
        return False

    del data["conversations"][key]
    save_conversations(data)

    return True


def complete_conversation(
    chat_id: int | str,
) -> dict[str, Any] | None:
    """Return and remove a completed conversation."""
    conversation = get_active_conversation(chat_id)

    if conversation is None:
        return None

    completed = dict(conversation)
    completed["status"] = "completed"
    completed["completed_at"] = current_timestamp()

    cancel_conversation(chat_id)

    return completed