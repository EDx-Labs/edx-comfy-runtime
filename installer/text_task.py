"""Runtime contract for domain-neutral text tasks executed by Qwen through ComfyUI.

The Runtime only knows instructions, input, an optional output format and
generation limits. Callers own any editorial/product semantics.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .provisioning import Provisioner, read_json

ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_ID = "text-task"
WORKFLOW_PATH = ROOT / "workflows" / "text" / "qwen-text-task-api.json"
NODE_CLASS = "QwenTextTaskLocal"
RESULT_KEY = "edx_text_task"
RESULT_SCHEMA = "edx.text-task.result/v1"
RESPONSE_FORMATS = ("text", "json")

# Error codes (stable contract).
INVALID_REQUEST = "INVALID_REQUEST"
RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
COMFY_REJECTED = "COMFY_REJECTED"
COMFY_EXECUTION_ERROR = "COMFY_EXECUTION_ERROR"
MODEL_EXECUTION_FAILED = "MODEL_EXECUTION_FAILED"
INVALID_STRUCTURED_OUTPUT = "INVALID_STRUCTURED_OUTPUT"
INVALID_RESULT = "INVALID_RESULT"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"


class TextTaskError(RuntimeError):
    """Structured, serializable failure."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class TextTaskRequest:
    user_input: str
    instructions: str = ""
    response_format: str = "text"
    json_schema: dict[str, Any] | None = None
    max_tokens: int = 2048
    temperature: float = 0.2
    seed: int = 0
    timeout_s: float = 600.0

    def validate(self) -> None:
        if not isinstance(self.user_input, str) or not self.user_input.strip():
            raise TextTaskError(INVALID_REQUEST, "user_input must not be empty")
        if self.response_format not in RESPONSE_FORMATS:
            raise TextTaskError(INVALID_REQUEST, f"response_format must be one of {RESPONSE_FORMATS}")
        if self.json_schema is not None and self.response_format != "json":
            raise TextTaskError(INVALID_REQUEST, "json_schema requires response_format=json")
        if not 16 <= int(self.max_tokens) <= 8192:
            raise TextTaskError(INVALID_REQUEST, "max_tokens must be between 16 and 8192")
        if not 0.0 <= float(self.temperature) <= 2.0:
            raise TextTaskError(INVALID_REQUEST, "temperature must be between 0 and 2")
        if int(self.seed) < 0:
            raise TextTaskError(INVALID_REQUEST, "seed must be >= 0")
        if float(self.timeout_s) <= 0:
            raise TextTaskError(INVALID_REQUEST, "timeout_s must be > 0")


@dataclass(frozen=True)
class TextTaskResult:
    text: str
    structured: Any
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": True, "text": self.text, "structured": self.structured, "metadata": self.metadata}


class Transport(Protocol):
    def get_json(self, path: str) -> Any: ...
    def post_json(self, path: str, payload: dict[str, Any]) -> Any: ...


class HttpTransport:
    """Minimal ComfyUI HTTP transport (the only network dependency)."""

    def __init__(self, base_url: str, *, timeout: float = 30.0, opener=urllib.request.urlopen) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener

    def _call(self, request: urllib.request.Request) -> Any:
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(detail)
            except json.JSONDecodeError:
                parsed = detail
            raise TextTaskError(COMFY_REJECTED, f"ComfyUI HTTP {error.code}", details={"body": parsed}) from error
        except (urllib.error.URLError, OSError) as error:
            raise TextTaskError(RUNTIME_UNAVAILABLE, "ComfyUI is not reachable", details={"reason": str(error)}) from error
        return json.loads(body.decode("utf-8")) if body else {}

    def get_json(self, path: str) -> Any:
        return self._call(urllib.request.Request(self.base_url + path))

    def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        return self._call(urllib.request.Request(
            self.base_url + path, data=data, method="POST", headers={"Content-Type": "application/json"},
        ))


def default_base_url(manifest: Path | None = None) -> str:
    comfy = read_json(Path(manifest or ROOT / "installer" / "runtime.json")).get("comfyui", {})
    return f"http://{comfy.get('listen_host', '127.0.0.1')}:{comfy.get('default_port', 8189)}"


def build_workflow(request: TextTaskRequest, template: Path = WORKFLOW_PATH) -> dict[str, Any]:
    workflow = copy.deepcopy(json.loads(Path(template).read_text(encoding="utf-8")))
    node = next((n for n in workflow.values() if n.get("class_type") == NODE_CLASS), None)
    if node is None:
        raise TextTaskError(INVALID_REQUEST, f"workflow template has no {NODE_CLASS} node")
    node["inputs"].update({
        "instructions": request.instructions,
        "user_input": request.user_input,
        "response_format": request.response_format,
        "json_schema": json.dumps(request.json_schema) if request.json_schema is not None else "",
        "max_tokens": int(request.max_tokens),
        "temperature": float(request.temperature),
        "seed": int(request.seed),
    })
    return workflow


class TextTaskClient:
    def __init__(
        self,
        transport: Transport | None = None,
        *,
        workflow_path: Path = WORKFLOW_PATH,
        poll_interval: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport or HttpTransport(default_base_url())
        self.workflow_path = workflow_path
        self.poll_interval = poll_interval
        self.sleeper = sleeper
        self.clock = clock

    def _interrupt(self) -> None:
        try:
            self.transport.post_json("/interrupt", {})
        except TextTaskError:
            pass

    def run(self, request: TextTaskRequest, *, cancel_event: threading.Event | None = None) -> TextTaskResult:
        request.validate()
        workflow = build_workflow(request, self.workflow_path)
        submitted = self.transport.post_json("/prompt", {"prompt": workflow, "client_id": uuid.uuid4().hex})
        if submitted.get("node_errors"):
            raise TextTaskError(COMFY_REJECTED, "ComfyUI rejected the workflow", details={"node_errors": submitted["node_errors"]})
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            raise TextTaskError(COMFY_REJECTED, "ComfyUI did not return a prompt_id", details={"response": submitted})
        deadline = self.clock() + float(request.timeout_s)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._interrupt()
                raise TextTaskError(CANCELLED, "text task cancelled", details={"prompt_id": prompt_id})
            entry = (self.transport.get_json(f"/history/{prompt_id}") or {}).get(prompt_id)
            if entry:
                return self._finish(prompt_id, entry)
            if self.clock() >= deadline:
                self._interrupt()
                raise TextTaskError(TIMEOUT, f"text task exceeded {request.timeout_s:g}s", details={"prompt_id": prompt_id})
            self.sleeper(self.poll_interval)

    @staticmethod
    def _execution_error(entry: dict[str, Any]) -> dict[str, Any]:
        for name, payload in (entry.get("status") or {}).get("messages", []):
            if name == "execution_error":
                return {k: payload.get(k) for k in ("node_type", "exception_type", "exception_message") if k in payload}
            if name == "execution_interrupted":
                return {"interrupted": True}
        return {}

    def _finish(self, prompt_id: str, entry: dict[str, Any]) -> TextTaskResult:
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            details = {"prompt_id": prompt_id, **self._execution_error(entry)}
            if details.get("interrupted"):
                raise TextTaskError(CANCELLED, "text task interrupted", details=details)
            raise TextTaskError(COMFY_EXECUTION_ERROR, details.get("exception_message") or "ComfyUI execution failed", details=details)
        for output in (entry.get("outputs") or {}).values():
            if RESULT_KEY in output:
                return self._parse(output[RESULT_KEY], prompt_id)
        raise TextTaskError(INVALID_RESULT, "ComfyUI produced no text task result", details={"prompt_id": prompt_id})

    @staticmethod
    def _parse(values: Any, prompt_id: str) -> TextTaskResult:
        try:
            envelope = json.loads(values[0])
        except (TypeError, IndexError, json.JSONDecodeError) as error:
            raise TextTaskError(INVALID_RESULT, "text task result is not valid JSON", details={"prompt_id": prompt_id}) from error
        if not isinstance(envelope, dict) or envelope.get("schema") != RESULT_SCHEMA:
            raise TextTaskError(INVALID_RESULT, "unexpected text task result schema", details={"prompt_id": prompt_id})
        if not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise TextTaskError(
                error.get("code") or MODEL_EXECUTION_FAILED, error.get("message") or "text task failed",
                details={"prompt_id": prompt_id},
            )
        metadata = dict(envelope.get("metadata") or {})
        metadata["prompt_id"] = prompt_id
        return TextTaskResult(str(envelope.get("text", "")), envelope.get("structured"), metadata)


def describe_availability(provisioner: Provisioner, comfy: Path | None) -> dict[str, Any]:
    """Capability discovery: unavailable | not_installed | installed."""
    entry = next((c for c in provisioner.capability_catalog(comfy) if c["runtime_id"] == "text-generation"), None)
    provider = entry["providers"][0] if entry and entry["providers"] else None
    if provider is None or not provider["provisioning"]["installable"]:
        return {"capability": CAPABILITY_ID, "status": "unavailable", "provider": provider and provider["id"],
                "reasons": (provider or {}).get("provisioning", {}).get("reason_codes", ["NO_PROVIDER"])}
    installation = provider.get("installation") or {}
    status = "installed" if installation.get("installed") else "not_installed"
    return {"capability": CAPABILITY_ID, "status": status, "provider": provider["id"],
            "missing": installation.get("missing", []), "runtime_dependencies": installation.get("runtime_dependencies", []), "requirements": provider["requirements"]}
