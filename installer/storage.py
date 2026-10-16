"""Persistência JSON atômica usada pelos adapters do runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default or {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
