"""Thread-safe storage for web ↔ graph bridge data."""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_inputs: dict[str, Any] = {}
_outputs: dict[str, dict[str, Any]] = {}


def set_input(session_key: str, payload: Any) -> None:
    with _lock:
        _inputs[session_key] = payload


def get_input(session_key: str) -> Any:
    with _lock:
        return _inputs.get(session_key)


def clear_input(session_key: str) -> None:
    with _lock:
        _inputs.pop(session_key, None)


def set_output_images(session_key: str, images: list[dict[str, str]]) -> None:
    with _lock:
        cur = _outputs.get(session_key, {})
        cur["images"] = images
        _outputs[session_key] = cur


def set_output_text(session_key: str, text: str) -> None:
    with _lock:
        cur = _outputs.get(session_key, {})
        cur["text"] = text if isinstance(text, str) else str(text)
        _outputs[session_key] = cur


def get_output(session_key: str) -> dict[str, Any] | None:
    with _lock:
        return _outputs.get(session_key)


def clear_output(session_key: str) -> None:
    with _lock:
        _outputs.pop(session_key, None)
