"""RT2.2 simulated Runtime host for isolated CM6/E1 integration.

This process consumes CM5 bootstrap environments from a private atomic spool,
materialises them with the RT1 implementation, and uses the real CM4 readiness
client.  It deliberately does not import or start the remote supervisor,
ComfyUI, model provisioning, inference, CUDA, or provider tooling.

The spool is transport-only and is not a new public contract.  Each input is a
0600 JSON file containing the exact public and secret environment maps from the
CM5 create call plus the FakeProvider's ``CONTAINER_ID``.  The scoped token is
removed from the spool as soon as RT1 has written its private credential file.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .remote_bootstrap import (
    ALLOW_INSECURE_LOCAL_ENV,
    CallbackError,
    ReadinessCallbackClient,
    RemoteBootstrapError,
    Schema2ManifestParser,
    TOKEN_FILE_ENV,
    TRUSTED_ORIGINS_ENV,
    load_service_token,
)
from .remote_env import (
    CONSUMED,
    PROVIDER_ID_ENV,
    REQUIRED,
    materialize,
    private_environment,
)

ACKNOWLEDGEMENT = "I_UNDERSTAND_SIMULATED_RUNTIME_RUNS_NO_COMFYUI_OR_GPU"
SAFE_ENVIRONMENTS = frozenset({"local", "test", "integration"})
OUTCOMES = frozenset({"ready", "absent", "invalid_token"})
ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 128 * 1024
MAX_DELAY_SECONDS = 300.0

ENABLED_ENV = "EDX_SIMULATED_RUNTIME_ENABLED"
ACK_ENV = "EDX_SIMULATED_RUNTIME_ACKNOWLEDGEMENT"
DEPLOYMENT_ENV = "EDX_SIMULATED_RUNTIME_ENVIRONMENT"
SPOOL_ENV = "EDX_SIMULATED_RUNTIME_SPOOL_DIR"
STATE_ENV = "EDX_SIMULATED_RUNTIME_STATE_DIR"
SECRETS_ENV = "EDX_SIMULATED_RUNTIME_SECRETS_DIR"
OUTCOME_ENV = "EDX_SIMULATED_RUNTIME_OUTCOME"
DELAY_ENV = "EDX_SIMULATED_RUNTIME_STARTUP_DELAY_SECONDS"
POLL_ENV = "EDX_SIMULATED_RUNTIME_POLL_INTERVAL_SECONDS"
CALLBACK_TIMEOUT_ENV = "EDX_SIMULATED_RUNTIME_CALLBACK_TIMEOUT_SECONDS"


class SimulatedHostError(RuntimeError):
    """Safe operational error.  Messages never contain delivered values."""


def _number(environment: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = str(environment.get(name, default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise SimulatedHostError(f"{name} must be numeric") from exc
    if value < minimum or value > maximum:
        raise SimulatedHostError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _absolute_path(environment: Mapping[str, str], name: str, default: str) -> Path:
    value = Path(str(environment.get(name, default)).strip()).expanduser()
    if not value.is_absolute():
        raise SimulatedHostError(f"{name} must be an absolute path")
    return value


@dataclass(frozen=True)
class HostConfig:
    spool_dir: Path
    state_dir: Path
    secrets_dir: Path
    outcome: str
    startup_delay: float
    poll_interval: float
    callback_timeout: float
    allow_insecure_local: bool

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "HostConfig":
        env = os.environ if environment is None else environment
        if env.get(ENABLED_ENV) != "true":
            raise SimulatedHostError(f"{ENABLED_ENV}=true is required")
        if env.get(ACK_ENV) != ACKNOWLEDGEMENT:
            raise SimulatedHostError(f"{ACK_ENV} is missing")
        if str(env.get(DEPLOYMENT_ENV, "")).strip().lower() not in SAFE_ENVIRONMENTS:
            raise SimulatedHostError(f"{DEPLOYMENT_ENV} must be local, test, or integration")
        outcome = str(env.get(OUTCOME_ENV, "ready")).strip().lower()
        if outcome not in OUTCOMES:
            raise SimulatedHostError(f"{OUTCOME_ENV} is invalid")
        for gpu_name, allowed in (
            ("NVIDIA_VISIBLE_DEVICES", {"", "none", "void"}),
            ("CUDA_VISIBLE_DEVICES", {"", "-1"}),
        ):
            if gpu_name in env and str(env[gpu_name]).strip().lower() not in allowed:
                raise SimulatedHostError(f"{gpu_name} must disable GPU access")
        config = cls(
            spool_dir=_absolute_path(env, SPOOL_ENV, "/run/edx-runtime-bootstrap"),
            state_dir=_absolute_path(env, STATE_ENV, "/workspace/.edx/simulated"),
            secrets_dir=_absolute_path(env, SECRETS_ENV, "/run/secrets/edx-simulated-runtime"),
            outcome=outcome,
            startup_delay=_number(env, DELAY_ENV, 0, minimum=0, maximum=MAX_DELAY_SECONDS),
            poll_interval=_number(env, POLL_ENV, 1, minimum=0.05, maximum=60),
            callback_timeout=_number(env, CALLBACK_TIMEOUT_ENV, 10, minimum=0.1, maximum=60),
            allow_insecure_local=env.get(ALLOW_INSECURE_LOCAL_ENV) == "1",
        )
        if len({config.spool_dir, config.state_dir, config.secrets_dir}) != 3:
            raise SimulatedHostError("spool, state, and secrets directories must be distinct")
        return config


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SimulatedHostError("a simulated Runtime directory cannot be a symbolic link")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise SimulatedHostError("a simulated Runtime path is not a directory")
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SimulatedHostError("bootstrap envelope has an invalid instance id") from exc
    canonical = str(parsed)
    if value != canonical:
        raise SimulatedHostError("bootstrap envelope has a non-canonical instance id")
    return canonical


def _load_envelope(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise SimulatedHostError("bootstrap envelope must be a regular non-symbolic file")
    info = path.stat()
    if info.st_size <= 0 or info.st_size > MAX_ENVELOPE_BYTES:
        raise SimulatedHostError("bootstrap envelope is empty or too large")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise SimulatedHostError("bootstrap envelope must be accessible only by its owner")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SimulatedHostError("bootstrap envelope is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "environment"} or payload.get("version") != ENVELOPE_VERSION:
        raise SimulatedHostError("bootstrap envelope shape or version is invalid")
    environment = payload.get("environment")
    if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
        raise SimulatedHostError("bootstrap envelope environment must contain string pairs")
    allowed = set(CONSUMED) | {PROVIDER_ID_ENV}
    if set(environment) - allowed:
        raise SimulatedHostError("bootstrap envelope contains variables outside the CM5 contract")
    if set(REQUIRED) - set(environment) or TRUSTED_ORIGINS_ENV not in environment or PROVIDER_ID_ENV not in environment:
        raise SimulatedHostError("bootstrap envelope is missing required CM5 variables")
    instance_id = _canonical_uuid(environment["EDX_BOOTSTRAP_INSTANCE_ID"])
    expected_name = path.name.removesuffix(".processing").removesuffix(".json")
    if expected_name != instance_id:
        raise SimulatedHostError("bootstrap envelope filename does not match its instance id")
    return dict(environment)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JsonLogger:
    def emit(self, level: str, event: str, **fields: Any) -> None:
        record = {"timestamp": _utc_now(), "level": level, "event": event, **fields}
        print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


class SimulatedRuntimeHost:
    def __init__(
        self,
        config: HostConfig,
        *,
        callback_factory: Callable[[], ReadinessCallbackClient] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        logger: JsonLogger | None = None,
    ) -> None:
        self.config = config
        self.callback_factory = callback_factory or (
            lambda: ReadinessCallbackClient(timeout=config.callback_timeout, sleeper=sleeper)
        )
        self.sleeper = sleeper
        self.log = logger or JsonLogger()
        for directory in (config.spool_dir, config.state_dir, config.secrets_dir):
            _ensure_private_directory(directory)

    def _paths(self, instance_id: str) -> tuple[Path, Path, Path]:
        root = self.config.state_dir / instance_id
        return root / "bootstrap.json", self.config.secrets_dir / f"{instance_id}.token", root / "state.json"

    def _state(
        self,
        path: Path,
        *,
        instance_id: str,
        provider_instance_id: str,
        trusted_origins: str,
        status: str,
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        value: dict[str, Any] = {
            "version": 1,
            "instance_id": instance_id,
            "provider_instance_id": provider_instance_id,
            "trusted_origins": trusted_origins,
            "status": status,
            "retryable": retryable,
            "updated_at": _utc_now(),
        }
        if http_status is not None:
            value["http_status"] = http_status
        _atomic_json(path, value)

    def _runtime_environment(self, delivered: Mapping[str, str]) -> dict[str, str]:
        environment = dict(delivered)
        if self.config.allow_insecure_local:
            environment[ALLOW_INSECURE_LOCAL_ENV] = "1"
        return environment

    def _send(self, manifest_path: Path, token_path: Path, state_path: Path) -> bool:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        instance_id = state["instance_id"]
        provider_id = state["provider_instance_id"]
        trusted_origins = state["trusted_origins"]
        safe_environment = {TRUSTED_ORIGINS_ENV: trusted_origins}
        if self.config.allow_insecure_local:
            safe_environment[ALLOW_INSECURE_LOCAL_ENV] = "1"
        manifest = Schema2ManifestParser(safe_environment).load(manifest_path)
        token = load_service_token({TOKEN_FILE_ENV: str(token_path)})
        if self.config.outcome == "absent":
            self._state(
                state_path,
                instance_id=instance_id,
                provider_instance_id=provider_id,
                trusted_origins=trusted_origins,
                status="readiness_suppressed",
            )
            self.log.emit("INFO", "readiness_suppressed", instance_id=instance_id, provider_instance_id=provider_id)
            return True
        callback_token = token if self.config.outcome == "ready" else token + ".invalid"
        try:
            status = self.callback_factory().send(manifest, callback_token)
        except CallbackError as exc:
            self._state(
                state_path,
                instance_id=instance_id,
                provider_instance_id=provider_id,
                trusted_origins=trusted_origins,
                status="callback_failed" if exc.transient else "callback_rejected",
                retryable=exc.transient,
                http_status=exc.status,
            )
            self.log.emit(
                "WARNING",
                "readiness_callback_failed",
                instance_id=instance_id,
                provider_instance_id=provider_id,
                transient=exc.transient,
                http_status=exc.status,
            )
            return False
        self._state(
            state_path,
            instance_id=instance_id,
            provider_instance_id=provider_id,
            trusted_origins=trusted_origins,
            status="ready",
            http_status=status,
        )
        self.log.emit("INFO", "readiness_confirmed", instance_id=instance_id, provider_instance_id=provider_id, http_status=status)
        return True

    def _process_envelope(self, path: Path) -> bool:
        environment = _load_envelope(path)
        runtime_environment = self._runtime_environment(environment)
        instance_id = environment["EDX_BOOTSTRAP_INSTANCE_ID"]
        manifest_path, token_path, state_path = self._paths(instance_id)
        materialized = materialize(runtime_environment, manifest_path, token_path)
        # Reuse RT1's sanitized mapping and validate that the published files
        # are sufficient before deleting the only transport copy.
        private = private_environment(runtime_environment, token_path)
        parsed = Schema2ManifestParser(private).load(manifest_path)
        load_service_token(private)
        self._state(
            state_path,
            instance_id=parsed.instance_id,
            provider_instance_id=parsed.provider_instance_id,
            trusted_origins=environment[TRUSTED_ORIGINS_ENV],
            status="prepared",
        )
        path.unlink()
        self.log.emit(
            "INFO",
            "bootstrap_materialized",
            instance_id=materialized.instance_id,
            provider_instance_id=materialized.provider_instance_id,
            capability_count=len(materialized.capabilities),
        )
        if self.config.startup_delay:
            self.sleeper(self.config.startup_delay)
        return self._send(manifest_path, token_path, state_path)

    def _replay(self) -> int:
        count = 0
        for state_path in sorted(self.config.state_dir.glob("*/state.json")):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("status") == "readiness_suppressed" and self.config.outcome == "absent":
                    continue
                manifest_path, token_path, expected_state = self._paths(_canonical_uuid(state["instance_id"]))
                if expected_state != state_path or not manifest_path.is_file() or not token_path.is_file():
                    raise SimulatedHostError("persisted simulated Runtime state is incomplete")
                self._send(manifest_path, token_path, state_path)
                count += 1
            except (OSError, ValueError, KeyError, RemoteBootstrapError, SimulatedHostError) as exc:
                self.log.emit("ERROR", "restart_recovery_failed", error_type=type(exc).__name__)
        return count

    def run_once(self, *, replay_existing: bool = False) -> dict[str, int]:
        result = {"replayed": self._replay() if replay_existing else 0, "processed": 0, "failed": 0}
        paths = sorted(self.config.spool_dir.glob("*.processing")) + sorted(self.config.spool_dir.glob("*.json"))
        for source in paths:
            claimed = source if source.suffix == ".processing" else source.with_suffix(".processing")
            try:
                if source != claimed:
                    if claimed.exists():
                        continue
                    source.rename(claimed)
                if self._process_envelope(claimed):
                    result["processed"] += 1
                else:
                    result["failed"] += 1
            except (OSError, ValueError, KeyError, RemoteBootstrapError, SimulatedHostError) as exc:
                result["failed"] += 1
                self.log.emit("ERROR", "bootstrap_processing_failed", error_type=type(exc).__name__)
        self.heartbeat()
        return result

    def heartbeat(self) -> None:
        _atomic_json(self.config.state_dir / "heartbeat.json", {"version": 1, "updated_at": _utc_now()})

    def run_forever(self) -> None:
        stopping = False

        def stop(_signum, _frame) -> None:
            nonlocal stopping
            stopping = True

        for name in (signal.SIGTERM, signal.SIGINT):
            signal.signal(name, stop)
        self.log.emit("INFO", "simulated_runtime_started", outcome=self.config.outcome)
        self.run_once(replay_existing=True)
        while not stopping:
            self.sleeper(self.config.poll_interval)
            if not stopping:
                self.run_once()
        self.log.emit("INFO", "simulated_runtime_stopped")


def _healthcheck(config: HostConfig) -> int:
    heartbeat = config.state_dir / "heartbeat.json"
    if not heartbeat.is_file():
        return 1
    age = time.time() - heartbeat.stat().st_mtime
    return 0 if age <= max(10.0, config.poll_interval * 3) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EDx isolated simulated Runtime host")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = HostConfig.from_environment()
        if args.healthcheck:
            return _healthcheck(config)
        host = SimulatedRuntimeHost(config)
        if args.once:
            result = host.run_once(replay_existing=True)
            return 0 if result["failed"] == 0 else 2
        host.run_forever()
        return 0
    except (OSError, RemoteBootstrapError, SimulatedHostError) as exc:
        print(json.dumps({"timestamp": _utc_now(), "level": "ERROR", "event": "simulated_runtime_failed", "error_type": type(exc).__name__}, separators=(",", ":")), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
