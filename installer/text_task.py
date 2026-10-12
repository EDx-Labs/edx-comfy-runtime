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
CONTEXT_EXCEEDED = "CONTEXT_EXCEEDED"
RUNTIME_CONFIG_INVALID = "RUNTIME_CONFIG_INVALID"
CANCELLED = "CANCELLED"
JOB_LOST = "JOB_LOST"

# Waiting policy (see TextTaskClient.run). Queue time never counts; silence only counts once the prompt is running, and
# even then it is only REPORTED (STALLED_SUSPECTED): the client never cancels a task by itself because of silence.
QUEUE_INTERVAL_S = 2.0        # queue reconciliation while waiting in the queue / before it is known to run
RUNNING_QUEUE_INTERVAL_S = 15.0
LOST_CONFIRMATIONS = 2        # consecutive probes where the server knows the prompt neither in queue nor in history
LOST_MIN_AGE_S = 10.0


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
    # Inactivity threshold, counted only while the prompt is RUNNING in ComfyUI; time spent queued behind other jobs never
    # counts. Reaching it marks the task as suspected-stalled (reported through ``on_state``) and the client keeps waiting
    # for as long as the server keeps the job: it is never cancelled because of silence. Only the caller can stop it
    # (``cancel_event``), and that cancels ONLY this task's own prompt.
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
            raise TextTaskError(COMFY_REJECTED, f"ComfyUI HTTP {error.code}",
                                details={"body": parsed, "status": error.code}) from error
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

    # -- cancellation: ONLY this client's own prompt -----------------------------------------------------------------
    def _cancel(self, prompt_id: str) -> None:
        """Best-effort cancel of ``prompt_id`` and nothing else.

        Never ``POST /interrupt`` without a ``prompt_id``: ComfyUI treats that as a GLOBAL interrupt of whatever is running,
        which killed other projects' 15-minute video jobs when a queued text task timed out (run-d499577ba91b).
        ComfyUI >= 0.33 has ``POST /api/jobs/{id}/cancel`` (atomic: dequeues a pending job, interrupts a running one only
        while it is still the one running, no-op for finished/unknown ids). Older servers fall back to deleting the pending
        item or to a targeted interrupt.
        """
        try:
            self.transport.post_json(f"/api/jobs/{prompt_id}/cancel", {})
            return
        except TextTaskError as error:
            if error.code != COMFY_REJECTED or error.details.get("status") not in (404, 405):
                return  # unreachable or refused: do not improvise a broader action
        try:
            location = self._locate(prompt_id)[0]
            if location == "pending":
                self.transport.post_json("/queue", {"delete": [prompt_id]})
            elif location == "running":
                self.transport.post_json("/interrupt", {"prompt_id": prompt_id})
        except TextTaskError:
            pass

    # -- observation ------------------------------------------------------------------------------------------------
    def _locate(self, prompt_id: str) -> tuple[str, int | None, int | None]:
        """('pending'|'running'|'absent'|'unknown', 1-based position among pending, jobs ahead incl. the running one)."""
        try:
            queue = self.transport.get_json("/queue")
        except TextTaskError:
            return "unknown", None, None
        if not isinstance(queue, dict) or "queue_running" not in queue or "queue_pending" not in queue:
            return "unknown", None, None

        def pid(item: Any) -> Any:
            return item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else None

        running = list(queue.get("queue_running") or [])
        if any(pid(item) == prompt_id for item in running):
            return "running", None, None
        pending = sorted(queue.get("queue_pending") or [],
                         key=lambda item: item[0] if isinstance(item, (list, tuple)) and item and isinstance(item[0], (int, float)) else 0)
        for index, item in enumerate(pending):
            if pid(item) == prompt_id:
                return "pending", index + 1, len(running) + index
        return "absent", None, None

    @staticmethod
    def _emit(on_state: Callable[..., None] | None, state: str, **info: Any) -> None:
        if on_state is not None:
            try:
                on_state(state, info)
            except Exception:  # telemetry must never change the outcome of the task
                pass

    def run(self, request: TextTaskRequest, *, cancel_event: threading.Event | None = None,
            on_state: Callable[[str, dict[str, Any]], None] | None = None) -> TextTaskResult:
        """Submit, wait and return the result.

        Waiting policy: while the prompt is queued behind other jobs nothing times out (the wait is only reported through
        ``on_state``). ``timeout_s`` is an inactivity threshold that runs from the moment the prompt is seen RUNNING; when it
        is reached the task is reported STALLED_SUSPECTED and the client keeps waiting: silence, a flat server or a lost
        connection never cancel it. It ends by result, by ``cancel_event`` (which cancels just its own prompt) or, if the
        server no longer knows the prompt (restart), with JOB_LOST.
        If the queue cannot be read at all, the clock starts at submission (the only safe approximation).
        """
        request.validate()
        workflow = build_workflow(request, self.workflow_path)
        submitted = self.transport.post_json("/prompt", {"prompt": workflow, "client_id": uuid.uuid4().hex})
        if submitted.get("node_errors"):
            raise TextTaskError(COMFY_REJECTED, "ComfyUI rejected the workflow", details={"node_errors": submitted["node_errors"]})
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            raise TextTaskError(COMFY_REJECTED, "ComfyUI did not return a prompt_id", details={"response": submitted})

        timeout_s = float(request.timeout_s)
        queued_at = self.clock()
        started_at: float | None = None
        queue_known = False
        stalled = False
        absent = 0
        position: tuple[int | None, int | None] = (None, None)
        next_queue = queued_at
        self._emit(on_state, "QUEUED", prompt_id=prompt_id)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._cancel(prompt_id)
                self._emit(on_state, "CANCELLED", prompt_id=prompt_id)
                raise TextTaskError(CANCELLED, "text task cancelled", details={"prompt_id": prompt_id})
            entry = (self.transport.get_json(f"/history/{prompt_id}") or {}).get(prompt_id)
            if entry:
                try:
                    result = self._finish(prompt_id, entry)
                except TextTaskError as error:
                    self._emit(on_state, "CANCELLED" if error.code == CANCELLED else "FAILED", prompt_id=prompt_id, code=error.code)
                    raise
                self._emit(on_state, "COMPLETED", prompt_id=prompt_id)
                return result
            now = self.clock()
            location = "unknown"
            if now >= next_queue:
                location, ahead_position, ahead = self._locate(prompt_id)
                next_queue = now + (RUNNING_QUEUE_INTERVAL_S if started_at is not None and not stalled else QUEUE_INTERVAL_S)
                if location != "unknown":
                    queue_known = True
                if location == "pending":
                    absent = 0
                    if (ahead_position, ahead) != position:
                        position = (ahead_position, ahead)
                        self._emit(on_state, "QUEUED", prompt_id=prompt_id, position=ahead_position, ahead=ahead,
                                   waited_seconds=round(now - queued_at, 1))
                elif location == "running":
                    absent = 0
                    if started_at is None:
                        started_at = now
                        self._emit(on_state, "RUNNING", prompt_id=prompt_id, waited_seconds=round(now - queued_at, 1))
                elif location == "absent":
                    absent += 1
                    if absent >= LOST_CONFIRMATIONS and now - queued_at >= LOST_MIN_AGE_S:
                        self._emit(on_state, "FAILED", prompt_id=prompt_id, code=JOB_LOST)
                        raise TextTaskError(JOB_LOST, "ComfyUI no longer knows this text task (server restarted?)",
                                            details={"prompt_id": prompt_id})
            # the inference clock: starts when the prompt runs; if the queue can never be read, at submission
            origin = started_at if started_at is not None else (queued_at if not queue_known else None)
            if origin is not None:
                silence = now - origin
                if silence >= timeout_s and not stalled:
                    stalled = True
                    self._emit(on_state, "STALLED_SUSPECTED", prompt_id=prompt_id, silence_seconds=round(silence, 1))
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
                details={"prompt_id": prompt_id, **(error.get("details") or {})},
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
