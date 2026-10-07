"""Domain-neutral structured text task helpers for the Qwen node.

Pure Python (no ComfyUI imports) so the contract can be unit tested.
"""
from __future__ import annotations
import json
import os
from typing import Any, Mapping

RESPONSE_FORMATS = ("text", "json")
ENVELOPE_SCHEMA = "edx.text-task.result/v1"
# Default model of the text-only path (qwen-text / QwenTextTaskLocal): Qwen3-14B Q4_K_M, text-only (no vision projector).
# The multimodal H3 node (QwenH3PromptLocal / qwen-h3) is unchanged: it keeps the Qwen3.8-27B + mmproj in models/LLM/Qwen3.8.
TEXT_MODEL_SUBDIR = "Qwen3-14B"
TEXT_MODEL_FILENAME = "Qwen3-14B-Q4_K_M.gguf"

# Inference defaults of the text-only path (qwen-text / QwenTextTaskLocal), validated on an RTX 3050 6 GB:
# 32768 ctx + fit-target 1536 offloaded 11/41 layers; 8192 + 768 offloads 18/41 with ~750 MiB real margin.
# The multimodal H3 node does NOT use these: it keeps runtime.DEFAULT_CONTEXT_SIZE and the runtime_config fit target.
TEXT_CONTEXT_SIZE_DEFAULT = 8192
TEXT_FIT_TARGET_MIB_DEFAULT = 768
CONTEXT_SIZE_ENV = "EDX_QWEN_TEXT_CTX_SIZE"
FIT_TARGET_ENV = "EDX_QWEN_TEXT_FIT_TARGET_MIB"
CONTEXT_SIZE_RANGE = (2048, 262144)
FIT_TARGET_RANGE = (0, 16384)
CONTEXT_CHECK_MARGIN = 32  # tokens: chat-template extras (e.g. disabled-thinking block) that /apply-template may not add


class TextTaskInputError(ValueError):
    """Invalid caller input; maps to the INVALID_REQUEST error code."""


class RuntimeConfigError(ValueError):
    """Invalid operator override of the text inference defaults; maps to RUNTIME_CONFIG_INVALID."""


class ContextExceededError(RuntimeError):
    """prompt tokens + max_tokens do not fit the server context; maps to CONTEXT_EXCEEDED."""

    def __init__(self, prompt_tokens: int, max_tokens: int, context_size: int):
        self.prompt_tokens, self.max_tokens, self.context_size = prompt_tokens, max_tokens, context_size
        super().__init__(
            f"Contexto insuficiente: prompt de {prompt_tokens} tokens + max_tokens {max_tokens} "
            f"excede o contexto de {context_size} tokens. Reduza a entrada/max_tokens ou aumente "
            f"{CONTEXT_SIZE_ENV}."
        )

    def details(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens, "max_tokens": self.max_tokens, "context_size": self.context_size}


class StructuredOutputError(RuntimeError):
    """The model answer is not valid JSON; maps to INVALID_STRUCTURED_OUTPUT."""


def _int_override(environ: Mapping[str, str], name: str, default: int, bounds: tuple[int, int]) -> int:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as error:
        raise RuntimeConfigError(f"{name} deve ser um inteiro (recebido {raw!r})") from error
    if not bounds[0] <= value <= bounds[1]:
        raise RuntimeConfigError(f"{name}={value} fora do intervalo permitido {bounds[0]}..{bounds[1]}")
    return value


def resolve_inference_options(environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """Effective text-path llama.cpp options: defaults 8192 / 768 MiB, overridable by environment.

    n_gpu_layers is intentionally absent: llama.cpp's --fit picks the largest offload that fits.
    """
    environ = os.environ if environ is None else environ
    return {
        "context_size": _int_override(environ, CONTEXT_SIZE_ENV, TEXT_CONTEXT_SIZE_DEFAULT, CONTEXT_SIZE_RANGE),
        "fit_target_mib": _int_override(environ, FIT_TARGET_ENV, TEXT_FIT_TARGET_MIB_DEFAULT, FIT_TARGET_RANGE),
    }


def ensure_fits_context(prompt_tokens: int, max_tokens: int, context_size: int) -> None:
    """Raise ContextExceededError instead of letting llama.cpp truncate the answer silently."""
    if prompt_tokens + max_tokens + CONTEXT_CHECK_MARGIN > context_size:
        raise ContextExceededError(prompt_tokens, max_tokens, context_size)


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
                   elapsed_s: float, context_size: int | None = None,
                   fit_target_mib: int | None = None, prompt_tokens: int | None = None) -> dict[str, Any]:
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
            "context_size": context_size,
            "fit_target_mib": fit_target_mib,
            "prompt_tokens": prompt_tokens,
            "elapsed_s": round(elapsed_s, 3),
        },
    }
