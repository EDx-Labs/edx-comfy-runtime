"""Domain-neutral structured text task helpers for the Qwen node.

Pure Python (no ComfyUI imports) so the contract can be unit tested.
"""
from __future__ import annotations
import json
from typing import Any

RESPONSE_FORMATS = ("text", "json")
ENVELOPE_SCHEMA = "edx.text-task.result/v1"
MODEL_LABEL = "qwen3.8-27b"


class TextTaskInputError(ValueError):
    """Invalid caller input; maps to the INVALID_REQUEST error code."""


class StructuredOutputError(RuntimeError):
    """The model answer is not valid JSON; maps to INVALID_STRUCTURED_OUTPUT."""


def parse_json_schema(raw: str) -> dict[str, Any] | None:
    if not raw or not raw.strip():
        return None
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TextTaskInputError(f"json_schema is not valid JSON: {error.msg}") from error
    if not isinstance(schema, dict):
        raise TextTaskInputError("json_schema must be a JSON object")
    return schema


def build_response_format(response_format: str, json_schema: str = "") -> dict[str, Any] | None:
    if response_format not in RESPONSE_FORMATS:
        raise TextTaskInputError(f"response_format must be one of {RESPONSE_FORMATS}")
    schema = parse_json_schema(json_schema)
    if response_format == "text":
        if schema is not None:
            raise TextTaskInputError("json_schema requires response_format=json")
        return None
    if schema is None:
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}}


def build_messages(instructions: str, user_input: str) -> list[dict[str, str]]:
    if not user_input.strip():
        raise TextTaskInputError("user_input must not be empty")
    messages: list[dict[str, str]] = []
    if instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.append({"role": "user", "content": user_input})
    return messages


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def parse_structured(text: str) -> Any:
    try:
        return json.loads(_strip_fence(text))
    except json.JSONDecodeError as error:
        raise StructuredOutputError(f"model output is not valid JSON: {error.msg}") from error


def build_envelope(text: str, *, response_format: str, usage: dict[str, int],
                   model: str, backend: str, seed: int, max_tokens: int,
                   elapsed_s: float) -> dict[str, Any]:
    structured = parse_structured(text) if response_format == "json" else None
    return {
        "schema": ENVELOPE_SCHEMA,
        "ok": True,
        "text": text,
        "structured": structured,
        "metadata": {
            "model": model,
            "backend": backend,
            "response_format": response_format,
            "seed": seed,
            "max_tokens": max_tokens,
            "usage": dict(usage or {}),
            "elapsed_s": round(elapsed_s, 3),
        },
    }
