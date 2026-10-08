"""Supervisor opt-in do bootstrap remoto e do processo ComfyUI.

Este modulo e o PID de aplicacao do container remoto (sob ``tini``). Ele nao e
importado pelos entrypoints desktop e nao altera a instalacao local.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .provisioning import ProvisioningCancelled, ProvisioningError, process_state, read_json
from .remote_bootstrap import (
    DEFAULT_COMFYUI,
    DEFAULT_MANIFEST,
    TOKEN_ENV,
    TOKEN_FILE_ENV,
    RemoteBootstrap,
    RemoteBootstrapError,
    load_service_token,
)


WAIT_TIMEOUT_ENV = "EDX_BOOTSTRAP_WAIT_TIMEOUT"
POLL_INTERVAL_ENV = "EDX_BOOTSTRAP_POLL_INTERVAL"
SHUTDOWN_TIMEOUT_ENV = "EDX_COMFYUI_SHUTDOWN_TIMEOUT"


class RemoteSupervisorError(RemoteBootstrapError):
    """Falha operacional segura do entrypoint remoto."""


def _process_identity(pid: int) -> str | None:
    """Identidade Linux resistente a reutilizacao de PID dentro do container."""
    if os.name == "nt" or pid <= 0:
        return None
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing_parenthesis = stat_line.rfind(")")
        fields_from_state = stat_line[closing_parenthesis + 2:].split()
        start_time = fields_from_state[19]  # campo 22; a lista comeca no campo 3
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return f"{boot_id}:{start_time}"
    except (OSError, IndexError, UnicodeError):
        return None


class SupervisorLock:
    """Lock de PID simples que rejeita outro supervisor vivo e recupera stale."""

    def __init__(
        self,
        path: Path,
        *,
        state: Callable[[int], str] = process_state,
        identity: Callable[[int], str | None] = _process_identity,
    ) -> None:
        self.path = path
        self.state = state
        self.identity = identity
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                existing = read_json(self.path)
                owner = existing.get("pid")
                owner_alive = isinstance(owner, int) and owner > 0 and self.state(owner) in {"alive", "unknown"}
                recorded_identity = existing.get("process_identity")
                current_identity = self.identity(owner) if owner_alive else None
                reused_pid = bool(recorded_identity and current_identity and recorded_identity != current_identity)
                if owner_alive and not reused_pid:
                    raise RemoteSupervisorError("outro supervisor remoto ja esta ativo")
                try:
                    fresh_uninitialized_lock = not owner and time.time() - self.path.stat().st_mtime < 30
                except OSError:
                    fresh_uninitialized_lock = True
                if fresh_uninitialized_lock:
                    raise RemoteSupervisorError("outro supervisor remoto esta adquirindo o lock")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                payload = {
                    "pid": os.getpid(),
                    "process_identity": self.identity(os.getpid()),
                    "created_at": time.time(),
                }
                os.write(descriptor, json.dumps(payload).encode("utf-8"))
            finally:
                os.close(descriptor)
            self.acquired = True
            return
        raise RemoteSupervisorError("nao foi possivel adquirir o lock do supervisor remoto")

    def release(self) -> None:
        if not self.acquired:
            return
        current = read_json(self.path)
        if current.get("pid") == os.getpid() and current.get("process_identity") == self.identity(os.getpid()):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _configured_float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RemoteSupervisorError(f"{name} deve ser numerico") from exc


def _credential_configuration(environment: Mapping[str, str]) -> tuple[str, Path | None]:
    direct = bool(environment.get(TOKEN_ENV, "").strip())
    filename = environment.get(TOKEN_FILE_ENV, "").strip()
    if direct and filename:
        raise RemoteSupervisorError(f"defina apenas {TOKEN_ENV} ou {TOKEN_FILE_ENV}")
    if direct:
        return "environment", None
    if filename:
        return "file", Path(filename).expanduser()
    raise RemoteSupervisorError(
        f"credencial remota nao configurada; defina {TOKEN_FILE_ENV} para um arquivo privado"
    )


def _default_terminate_process(pid: int, timeout: float) -> None:
    """Encerra somente o processo/grupo rastreado, com escalonamento limitado."""
    if pid <= 0 or process_state(pid) == "dead":
        return
    if os.name == "nt":
        os.kill(pid, signal.SIGTERM)
    else:
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            return
        if group == pid:
            os.killpg(group, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_state(pid) == "dead":
            return
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return
        except (AttributeError, ChildProcessError):
            pass
        time.sleep(0.1)
    if process_state(pid) == "dead":
        return
    if os.name == "nt":
        os.kill(pid, signal.SIGTERM)
    else:
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            return
        if group == pid:
            os.killpg(group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)


class RemoteSupervisor:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        bootstrap: Any | None = None,
        process_state_fn: Callable[[int], str] = process_state,
        terminate_process: Callable[[int, float], None] = _default_terminate_process,
    ) -> None:
        self.environment = environment if environment is not None else os.environ
        self.bootstrap = bootstrap or RemoteBootstrap(environment=self.environment)
        self.process_state = process_state_fn
        self.terminate_process = terminate_process

    @staticmethod
    def _state_pid(comfy: Path) -> int | None:
        state = read_json(comfy / "user" / "default" / "EDx" / "remote-comfyui-process.json")
        pid = state.get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None

    @staticmethod
    def _state_identity(comfy: Path) -> tuple[int, int] | None:
        path = comfy / "user" / "default" / "EDx" / "remote-comfyui-process.json"
        try:
            info = path.stat()
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    @staticmethod
    def _nonempty_file(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _wait_for_inputs(
        self,
        manifest: Path,
        stop_event: threading.Event,
        *,
        timeout: float,
        poll_interval: float,
    ) -> None:
        credential_kind, secret_path = _credential_configuration(self.environment)
        deadline = time.monotonic() + timeout if timeout > 0 else None
        announced_manifest = False
        announced_secret = False
        while True:
            if stop_event.is_set():
                raise ProvisioningCancelled("supervisor encerrado enquanto aguardava configuracao remota")
            manifest_ready = self._nonempty_file(manifest)
            secret_ready = credential_kind == "environment" or bool(
                secret_path and self._nonempty_file(secret_path)
            )
            if manifest_ready and secret_ready:
                # Valida permissoes e conteudo sem manter ou registrar o segredo.
                load_service_token(self.environment)
                return
            if not manifest_ready and not announced_manifest:
                print(f"[EDx] aguardando manifesto remoto em {manifest}", flush=True)
                announced_manifest = True
            if not secret_ready and not announced_secret:
                print("[EDx] aguardando arquivo privado de credencial remota", flush=True)
                announced_secret = True
            if deadline is not None and time.monotonic() >= deadline:
                missing = "manifesto" if not manifest_ready else "arquivo de credencial"
                raise RemoteSupervisorError(f"timeout aguardando {missing} remoto")
            stop_event.wait(poll_interval)

    def run(
        self,
        manifest: str | Path,
        comfy: str | Path,
        *,
        stop_event: threading.Event,
        wait_timeout: float = 0,
        poll_interval: float = 1,
        shutdown_timeout: float = 20,
        startup_timeout: float = 180,
    ) -> dict[str, Any]:
        manifest_path = Path(manifest).expanduser()
        comfy_root = Path(comfy).expanduser().resolve()
        lock = SupervisorLock(
            manifest_path.parent / "remote-supervisor.lock",
            state=self.process_state,
        )
        managed_pid: int | None = None
        state_before = self._state_identity(comfy_root)
        with lock:
            try:
                self._wait_for_inputs(
                    manifest_path,
                    stop_event,
                    timeout=wait_timeout,
                    poll_interval=poll_interval,
                )
                print("[EDx] configuracao remota recebida; iniciando bootstrap", flush=True)
                result = self.bootstrap.run(
                    manifest_path,
                    comfy_root,
                    backend="nvidia",
                    host="127.0.0.1",
                    port=8189,
                    startup_timeout=startup_timeout,
                    cancel_event=stop_event,
                    require_tracked_process=True,
                )
                candidate = result.get("comfyui_pid")
                managed_pid = candidate if isinstance(candidate, int) and candidate > 0 else self._state_pid(comfy_root)
                if managed_pid is None or self.process_state(managed_pid) == "dead":
                    raise RemoteSupervisorError("processo rastreado do ComfyUI nao esta ativo apos o bootstrap")
                print("[EDx] bootstrap confirmado; supervisionando ComfyUI", flush=True)
                while not stop_event.wait(poll_interval):
                    if self.process_state(managed_pid) == "dead":
                        raise RemoteSupervisorError("processo do ComfyUI encerrou inesperadamente")
                return result
            finally:
                state_changed = self._state_identity(comfy_root) != state_before
                if managed_pid is None and state_changed:
                    managed_pid = self._state_pid(comfy_root)
                if managed_pid is not None and self.process_state(managed_pid) in {"alive", "unknown"}:
                    print("[EDx] encerrando ComfyUI", flush=True)
                    self.terminate_process(managed_pid, shutdown_timeout)


def build_parser(environment: Mapping[str, str] | None = None) -> argparse.ArgumentParser:
    environment = environment if environment is not None else os.environ
    parser = argparse.ArgumentParser(description="Supervisor remoto opt-in do EDx Runtime")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--comfyui",
        type=Path,
        default=Path(environment.get("COMFYUI_PATH") or DEFAULT_COMFYUI),
    )
    parser.add_argument("--wait-timeout", type=float, default=_configured_float(environment, WAIT_TIMEOUT_ENV, 0))
    parser.add_argument("--poll-interval", type=float, default=_configured_float(environment, POLL_INTERVAL_ENV, 1))
    parser.add_argument("--shutdown-timeout", type=float, default=_configured_float(environment, SHUTDOWN_TIMEOUT_ENV, 20))
    parser.add_argument("--startup-timeout", type=float, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.wait_timeout < 0:
            parser.error("--wait-timeout deve ser zero (ilimitado) ou positivo")
        if args.poll_interval <= 0 or args.shutdown_timeout <= 0 or args.startup_timeout <= 0:
            parser.error("intervalos e timeouts de shutdown/startup devem ser positivos")
        stop_event = threading.Event()

        def request_shutdown(signum, frame) -> None:
            del signum, frame
            stop_event.set()

        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        RemoteSupervisor().run(
            args.manifest,
            args.comfyui,
            stop_event=stop_event,
            wait_timeout=args.wait_timeout,
            poll_interval=args.poll_interval,
            shutdown_timeout=args.shutdown_timeout,
            startup_timeout=args.startup_timeout,
        )
        return 0
    except ProvisioningCancelled as exc:
        print(f"[EDx] supervisor remoto encerrado: {exc}", file=sys.stderr)
        return 0
    except (RemoteBootstrapError, ProvisioningError) as exc:
        print(f"[EDx] supervisor remoto falhou: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
