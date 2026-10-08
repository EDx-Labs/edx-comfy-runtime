"""Bootstrap remoto opt-in para instancias gerenciadas pelo Compute Manager.

Este modulo apenas orquestra as APIs existentes de ``runtime`` e
``provisioning``. Importa-lo nao le manifesto, credenciais, rede ou ambiente;
o fluxo so e ativado por ``python -m installer.remote_bootstrap``.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .provisioning import (
    Provisioner,
    ProvisioningCancelled,
    ProvisioningError,
    process_state,
    read_json,
    write_json,
)
from .runtime import BaseRuntimeInstaller, runtime_python


SCHEMA_VERSION = 2
DEFAULT_MANIFEST = Path("/workspace/.edx/bootstrap.json")
DEFAULT_COMFYUI = Path("/workspace/ComfyUI")
TOKEN_ENV = "EDX_COMPUTE_MANAGER_SERVICE_TOKEN"
TOKEN_FILE_ENV = "EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE"
TRUSTED_ORIGINS_ENV = "EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS"
ALLOW_INSECURE_LOCAL_ENV = "EDX_ALLOW_INSECURE_LOCAL_CALLBACK"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RemoteBootstrapError(RuntimeError):
    """Erro seguro para exibicao no entrypoint remoto."""


class ManifestValidationError(RemoteBootstrapError):
    pass


class ReadinessError(RemoteBootstrapError):
    pass


class CallbackError(RemoteBootstrapError):
    def __init__(self, message: str, *, status: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_CALLBACK_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _open_callback(request: urllib.request.Request, *, timeout: float):
    """Nao encaminha o bearer token para redirects, mesmo que o host responda 3xx."""
    return _CALLBACK_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True)
class RemoteManifest:
    instance_id: str
    provider_instance_id: str
    capabilities: tuple[str, ...]
    readiness_url: str
    digest: str


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ManifestValidationError(f"{field} deve ser string ou inteiro")
    normalized = str(value).strip()
    if not IDENTIFIER.fullmatch(normalized):
        raise ManifestValidationError(f"{field} ausente ou invalido")
    return normalized


def _canonical_origin(url: str, *, field: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ManifestValidationError(f"{field} invalida") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ManifestValidationError(f"{field} deve ser uma URL HTTP(S) absoluta")
    if parsed.username is not None or parsed.password is not None:
        raise ManifestValidationError(f"{field} nao pode conter credenciais")
    default_port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname.lower()
    host_display = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme.lower()}://{host_display}" + (f":{port}" if port and port != default_port else "")


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_callback_url(
    url: str,
    instance_id: str,
    environment: Mapping[str, str],
) -> str:
    """Aceita somente a rota exata e uma origem explicitamente confiavel."""
    if not isinstance(url, str) or not url.strip():
        raise ManifestValidationError("callback.readiness_url ausente ou invalida")
    value = url.strip()
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ManifestValidationError("callback.readiness_url invalida") from exc
    origin = _canonical_origin(value, field="callback.readiness_url")
    if parsed.query or parsed.fragment:
        raise ManifestValidationError("callback.readiness_url nao pode conter query ou fragmento")
    expected_path = f"/api/v1/internal/instances/{urllib.parse.quote(instance_id, safe='')}/readiness"
    if parsed.path != expected_path:
        raise ManifestValidationError("callback.readiness_url nao corresponde ao instance_id")
    trusted_raw = environment.get(TRUSTED_ORIGINS_ENV, "")
    trusted: set[str] = set()
    for item in trusted_raw.split(","):
        if item.strip():
            trusted.add(_canonical_origin(item.strip(), field=TRUSTED_ORIGINS_ENV))
    if not trusted:
        raise ManifestValidationError(f"{TRUSTED_ORIGINS_ENV} deve declarar a origem autorizada")
    if origin not in trusted:
        raise ManifestValidationError("origem do callback nao autorizada")
    hostname = parsed.hostname or ""
    if parsed.scheme != "https":
        local_exception = _is_loopback(hostname) and environment.get(ALLOW_INSECURE_LOCAL_ENV) == "1"
        if not local_exception:
            raise ManifestValidationError("callback externo exige HTTPS")
    return value


class Schema2ManifestParser:
    """Adaptador isolado do envelope schema 2 conhecido pelo Runtime.

    O contrato publicado pelo Compute Manager ainda nao esta versionado neste
    repositorio. Os nomes abaixo ficam deliberadamente confinados a esta classe
    para que o adaptador possa ser trocado sem tocar no provisionador.
    """

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = environment if environment is not None else os.environ

    def load(self, path: str | Path) -> RemoteManifest:
        source = Path(path).expanduser()
        if source.is_symlink():
            raise ManifestValidationError("manifesto remoto nao pode ser um link simbolico")
        if not source.is_file():
            raise ManifestValidationError(f"manifesto remoto ausente: {source}")
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise ManifestValidationError("manifesto remoto nao pode ser lido") from exc
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise ManifestValidationError("manifesto remoto vazio ou maior que 1 MiB")
        try:
            raw = source.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestValidationError("manifesto remoto nao e JSON UTF-8 valido") from exc
        if not isinstance(data, dict):
            raise ManifestValidationError("manifesto remoto deve ser um objeto JSON")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ManifestValidationError(f"schema_version incompativel; esperado {SCHEMA_VERSION}")
        instance_id = _require_identifier(data.get("instance_id"), "instance_id")
        provider_id = _require_identifier(data.get("provider_instance_id"), "provider_instance_id")
        requested = data.get("capabilities")
        if not isinstance(requested, list) or not requested:
            raise ManifestValidationError("capabilities deve ser uma lista nao vazia")
        capabilities: list[str] = []
        for index, value in enumerate(requested):
            item = _require_identifier(value, f"capabilities[{index}]").lower()
            if item not in capabilities:
                capabilities.append(item)
        callback = data.get("callback")
        if not isinstance(callback, dict):
            raise ManifestValidationError("callback deve ser um objeto")
        readiness_url = validate_callback_url(callback.get("readiness_url"), instance_id, self.environment)
        digest = hashlib.sha256(raw).hexdigest()
        return RemoteManifest(instance_id, provider_id, tuple(capabilities), readiness_url, digest)


def load_service_token(environment: Mapping[str, str] | None = None) -> str:
    environment = environment if environment is not None else os.environ
    direct = environment.get(TOKEN_ENV)
    filename = environment.get(TOKEN_FILE_ENV)
    if direct and filename:
        raise RemoteBootstrapError(f"defina apenas {TOKEN_ENV} ou {TOKEN_FILE_ENV}")
    if filename:
        path = Path(filename).expanduser()
        if path.is_symlink() or not path.is_file():
            raise RemoteBootstrapError("arquivo de segredo ausente, invalido ou simbolico")
        try:
            info = path.stat()
            if info.st_size <= 0 or info.st_size > MAX_SECRET_BYTES:
                raise RemoteBootstrapError("arquivo de segredo vazio ou grande demais")
            if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
                raise RemoteBootstrapError("arquivo de segredo deve ser acessivel somente pelo proprietario")
            direct = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RemoteBootstrapError("arquivo de segredo nao e UTF-8 valido") from exc
        except OSError as exc:
            raise RemoteBootstrapError("arquivo de segredo nao pode ser lido") from exc
    token = (direct or "").strip()
    if not token:
        raise RemoteBootstrapError("token de servico do Compute Manager ausente")
    if any(ord(character) < 32 or ord(character) == 127 for character in token):
        raise RemoteBootstrapError("token de servico contem caracteres invalidos")
    return token


class ReadinessCallbackClient:
    def __init__(
        self,
        *,
        opener: Callable[..., Any] = _open_callback,
        sleeper: Callable[[float], None] = time.sleep,
        attempts: int = 4,
        timeout: float = 10.0,
    ) -> None:
        self.opener = opener
        self.sleeper = sleeper
        self.attempts = max(1, attempts)
        self.timeout = timeout

    def send(self, manifest: RemoteManifest, token: str) -> int:
        body = json.dumps(
            {"provider_instance_id": manifest.provider_instance_id},
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            manifest.readiness_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        last_error: CallbackError | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    status_code = _response_status(response)
                    response.read(4096)
                if 200 <= status_code < 300:
                    return status_code
                transient = status_code in {408, 425, 429} or status_code >= 500
                last_error = CallbackError(
                    f"callback de readiness rejeitado (HTTP {status_code})",
                    status=status_code,
                    transient=transient,
                )
            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
                transient = status_code in {408, 425, 429} or status_code >= 500
                last_error = CallbackError(
                    f"callback de readiness rejeitado (HTTP {status_code})",
                    status=status_code,
                    transient=transient,
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                last_error = CallbackError("callback de readiness indisponivel", transient=True)
            if not last_error.transient or attempt >= self.attempts:
                raise last_error
            self.sleeper(min(8.0, float(2 ** (attempt - 1))))
        raise last_error or CallbackError("callback de readiness falhou")


class ComfyAPI:
    def __init__(self, opener: Callable[..., Any] = urllib.request.urlopen, *, timeout: float = 5.0) -> None:
        self.opener = opener
        self.timeout = timeout

    def get_json(self, url: str) -> Any:
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        with self.opener(request, timeout=self.timeout) as response:
            if _response_status(response) != 200:
                raise ReadinessError("API do ComfyUI respondeu com status inesperado")
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ReadinessError("resposta da API do ComfyUI excede o limite")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReadinessError("API do ComfyUI retornou JSON invalido") from exc


def _response_status(response: Any) -> int:
    value = getattr(response, "status", None)
    if value is None:
        value = response.getcode()
    return int(value)


class ComfyUIController:
    def __init__(
        self,
        api: ComfyAPI | None = None,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api = api or ComfyAPI()
        self.process_factory = process_factory
        self.sleeper = sleeper

    @staticmethod
    def _state_path(comfy: Path) -> Path:
        return comfy / "user" / "default" / "EDx" / "remote-comfyui-process.json"

    def _system_available(self, base_url: str) -> bool:
        try:
            payload = self.api.get_json(base_url + "/system_stats")
            return isinstance(payload, dict) and bool(payload)
        except Exception:
            return False

    def ensure_running(
        self,
        comfy: Path,
        base_url: str,
        *,
        host: str,
        port: int,
        timeout: float,
        cancel_event=None,
    ) -> dict[str, Any]:
        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise ProvisioningCancelled("inicializacao do ComfyUI cancelada")

        check_cancelled()
        state_path = self._state_path(comfy)
        if self._system_available(base_url):
            previous = read_json(state_path)
            candidate = previous.get("pid")
            pid = candidate if isinstance(candidate, int) and candidate > 0 else None
            if pid is not None and process_state(pid) == "dead":
                pid = None
            return {"reused": True, "pid": pid}
        lock_path = state_path.with_name("remote-comfyui-start.lock")
        deadline = time.monotonic() + timeout
        pid: int | None = None
        tracked_alive = False
        process = None
        while time.monotonic() < deadline:
            check_cancelled()
            if self._system_available(base_url):
                return {"reused": True, "pid": pid}
            previous = read_json(state_path)
            candidate = previous.get("pid")
            if isinstance(candidate, int) and candidate > 0 and process_state(candidate) in {"alive", "unknown"}:
                pid = candidate
                tracked_alive = True
                break
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                lock = read_json(lock_path)
                owner = lock.get("pid")
                owner_state = process_state(owner) if isinstance(owner, int) and owner > 0 else "unknown"
                try:
                    old_empty_lock = not owner and time.time() - lock_path.stat().st_mtime > 30
                except OSError:
                    old_empty_lock = False
                if owner_state == "dead" or old_empty_lock:
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if cancel_event is not None:
                    if cancel_event.wait(0.2):
                        check_cancelled()
                else:
                    self.sleeper(0.2)
                continue
            try:
                os.write(descriptor, json.dumps({"pid": os.getpid(), "created_at": time.time()}).encode("utf-8"))
                if self._system_available(base_url):
                    return {"reused": True, "pid": None}
                previous = read_json(state_path)
                candidate = previous.get("pid")
                if isinstance(candidate, int) and candidate > 0 and process_state(candidate) in {"alive", "unknown"}:
                    pid = candidate
                    tracked_alive = True
                    break
                python = runtime_python(comfy)
                entrypoint = comfy / "main.py"
                if not python.is_file() or not entrypoint.is_file():
                    raise ReadinessError("runtime Python ou entrypoint do ComfyUI ausente")
                log_path = state_path.with_name("remote-comfyui.log")
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_stream = log_path.open("ab")
                try:
                    process = self.process_factory(
                        [str(python), str(entrypoint), "--listen", host, "--port", str(port)],
                        cwd=str(comfy), stdin=subprocess.DEVNULL, stdout=log_stream,
                        stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"),
                    )
                finally:
                    log_stream.close()
                pid = int(process.pid)
                write_json(state_path, {
                    "schema_version": 1, "pid": pid, "host": host, "port": port,
                    "started_at": time.time(), "log": str(log_path),
                })
                break
            finally:
                os.close(descriptor)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
        while time.monotonic() < deadline:
            check_cancelled()
            if process is not None and process.poll() is not None:
                raise ReadinessError("processo do ComfyUI encerrou durante a inicializacao")
            if self._system_available(base_url):
                return {"reused": tracked_alive, "pid": pid}
            if cancel_event is not None:
                if cancel_event.wait(0.5):
                    check_cancelled()
            else:
                self.sleeper(0.5)
        raise ReadinessError("ComfyUI nao ficou disponivel antes do timeout")


def _normalized_module(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


class RemoteBootstrap:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        parser: Schema2ManifestParser | None = None,
        runtime_installer: BaseRuntimeInstaller | None = None,
        provisioner: Provisioner | None = None,
        controller: ComfyUIController | None = None,
        callback_client: ReadinessCallbackClient | None = None,
    ) -> None:
        self.environment = environment if environment is not None else os.environ
        self.parser = parser or Schema2ManifestParser(self.environment)
        self.runtime_installer = runtime_installer or BaseRuntimeInstaller()
        self.provisioner = provisioner
        self.controller = controller or ComfyUIController()
        self.callback_client = callback_client or ReadinessCallbackClient()

    @staticmethod
    def _write_state(path: Path, manifest: RemoteManifest | None, phase: str, **details: Any) -> None:
        payload: dict[str, Any] = {"schema_version": 1, "phase": phase, "updated_at": time.time(), **details}
        if manifest is not None:
            payload.update({
                "manifest_sha256": manifest.digest,
                "instance_id": manifest.instance_id,
                "provider_instance_id": manifest.provider_instance_id,
                "capabilities": list(manifest.capabilities),
            })
        write_json(path, payload)

    @staticmethod
    def _resolve_profiles(provisioner: Provisioner, capabilities: tuple[str, ...]) -> list[str]:
        profiles: list[str] = []
        for capability in capabilities:
            profile = provisioner.resolve_profile(capability)
            if profile not in profiles:
                profiles.append(profile)
        return profiles

    @staticmethod
    def _verify_assets(provisioner: Provisioner, profiles: list[str], comfy: Path) -> None:
        failures: list[str] = []
        for profile in profiles:
            status = provisioner.profile_installation(profile, comfy)
            if not status.get("installed"):
                failures.extend(f"{profile}:{item}" for item in status.get("missing", []))
            requirements = provisioner.profile_requirements(profile)
            for ident in requirements["models"]:
                model = provisioner.models[ident]
                if not provisioner._valid_model(provisioner._model_path(comfy, model), model):
                    failures.append(f"model:{ident}")
            for ident in requirements["components"]:
                component = provisioner.components[ident]
                target = comfy / "custom_nodes" / component["name"]
                if not target.is_dir() or not any(target.iterdir()):
                    failures.append(f"component:{ident}")
                if component.get("backend_runtime") and not provisioner.backend_runtime_status(ident, comfy).get("ready"):
                    failures.append(f"runtime:{ident}")
            for ident in requirements["workflow_sets"]:
                destination = comfy / "user" / "default" / "workflows" / "EDx" / ident
                for relative in provisioner.workflow_sets[ident].get("workflows", []):
                    target = destination / Path(relative).name
                    if not target.is_file() or target.stat().st_size <= 0:
                        failures.append(f"workflow:{ident}/{target.name}")
        if failures:
            raise ReadinessError("assets obrigatorios ausentes ou invalidos: " + ", ".join(dict.fromkeys(failures)))

    @staticmethod
    def _verify_loaded_components(provisioner: Provisioner, profiles: list[str], object_info: Any) -> None:
        if not isinstance(object_info, dict) or not object_info:
            raise ReadinessError("endpoint /object_info vazio ou invalido")
        loaded_modules = {
            _normalized_module(str(item.get("python_module", "")))
            for item in object_info.values() if isinstance(item, dict) and item.get("python_module")
        }
        missing: list[str] = []
        component_ids: list[str] = []
        for profile in profiles:
            for ident in provisioner.profile_requirements(profile)["components"]:
                if ident not in component_ids:
                    component_ids.append(ident)
        for ident in component_ids:
            component = provisioner.components[ident]
            candidates = {
                _normalized_module(component["name"]),
                _normalized_module(Path(component["source"]).name),
            }
            if not any(candidate and any(candidate in module for module in loaded_modules) for candidate in candidates):
                missing.append(ident)
        if missing:
            raise ReadinessError("custom nodes obrigatorios nao carregados: " + ", ".join(missing))

    @staticmethod
    def _verify_hardware(provisioner: Provisioner, profiles: list[str], system_stats: Any, backend: str) -> None:
        runtime_backend = {"nvidia": "cuda"}.get(backend, backend).lower()
        required_vram_mb = 0
        for profile in profiles:
            requirements = provisioner.profile_requirements(profile)
            supported = {str(value).lower() for value in requirements.get("supported_backends", [])}
            if requirements.get("backend_contract_declared") and runtime_backend not in supported:
                raise ReadinessError(f"backend {runtime_backend} nao suporta o profile {profile}")
            required_vram_mb = max(required_vram_mb, int(requirements.get("min_vram_mb", 0)))
        if not required_vram_mb:
            return
        devices = system_stats.get("devices", []) if isinstance(system_stats, dict) else []
        available_bytes = max(
            (int(device.get("vram_total", 0)) for device in devices if isinstance(device, dict)),
            default=0,
        )
        if available_bytes < required_vram_mb * 1024 * 1024:
            raise ReadinessError(
                f"VRAM insuficiente ou nao reportada: minimo {required_vram_mb} MiB"
            )

    def run(
        self,
        manifest_path: str | Path,
        comfy: str | Path,
        *,
        backend: str = "nvidia",
        host: str = "127.0.0.1",
        port: int = 8189,
        startup_timeout: float = 180.0,
        cancel_event=None,
        require_tracked_process: bool = False,
    ) -> dict[str, Any]:
        manifest_source = Path(manifest_path).expanduser()
        state_path = manifest_source.parent / "bootstrap-state.json"
        manifest: RemoteManifest | None = None
        try:
            manifest = self.parser.load(manifest_source)
            token = load_service_token(self.environment)
            self._write_state(state_path, manifest, "VALIDATED")
            comfy_root = Path(comfy).expanduser().resolve()
            runtime_options: dict[str, Any] = {"backend": backend}
            if cancel_event is not None:
                runtime_options["cancel_event"] = cancel_event
            self.runtime_installer.ensure(comfy_root, **runtime_options)
            self._write_state(state_path, manifest, "RUNTIME_READY", comfyui=str(comfy_root))
            provisioner = self.provisioner or Provisioner(python_executable=runtime_python(comfy_root))
            profiles = self._resolve_profiles(provisioner, manifest.capabilities)
            for profile in profiles:
                plan = provisioner.plan(profile, comfy_root)
                install_options = {"cancel_event": cancel_event} if cancel_event is not None else {}
                provisioner.install(plan, **install_options)
            self._verify_assets(provisioner, profiles, comfy_root)
            self._write_state(state_path, manifest, "PROVISIONED", profiles=profiles)
            base_url = f"http://{host}:{port}"
            controller_options: dict[str, Any] = {
                "host": host,
                "port": port,
                "timeout": startup_timeout,
            }
            if cancel_event is not None:
                controller_options["cancel_event"] = cancel_event
            process = self.controller.ensure_running(comfy_root, base_url, **controller_options)
            if require_tracked_process:
                tracked_pid = process.get("pid")
                if not isinstance(tracked_pid, int) or tracked_pid <= 0:
                    raise ReadinessError("processo do ComfyUI esta disponivel, mas nao pode ser supervisionado")
            system_stats = self.controller.api.get_json(base_url + "/system_stats")
            if not isinstance(system_stats, dict) or not system_stats:
                raise ReadinessError("endpoint /system_stats vazio ou invalido")
            self._verify_hardware(provisioner, profiles, system_stats, backend)
            object_info = self.controller.api.get_json(base_url + "/object_info")
            self._verify_loaded_components(provisioner, profiles, object_info)
            self._write_state(state_path, manifest, "OPERATIONALLY_READY", profiles=profiles, process=process)
            if cancel_event is not None and cancel_event.is_set():
                raise ProvisioningCancelled("bootstrap cancelado antes do callback de readiness")
            status = self.callback_client.send(manifest, token)
            self._write_state(state_path, manifest, "READINESS_CONFIRMED", profiles=profiles, callback_status=status)
            return {
                "instance_id": manifest.instance_id,
                "provider_instance_id": manifest.provider_instance_id,
                "profiles": profiles,
                "callback_status": status,
                "comfyui_pid": process.get("pid"),
                "inference_validated": False,
            }
        except Exception as exc:
            safe_message = str(exc) if isinstance(exc, (RemoteBootstrapError, ProvisioningError)) else type(exc).__name__
            if manifest is not None:
                try:
                    self._write_state(state_path, manifest, "FAILED", error=safe_message)
                except OSError:
                    pass
            if isinstance(exc, (RemoteBootstrapError, ProvisioningError)):
                raise
            raise RemoteBootstrapError(f"bootstrap remoto falhou: {type(exc).__name__}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap remoto opt-in do EDx Runtime")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="manifesto schema 2")
    parser.add_argument("--comfyui", type=Path, help="raiz gerenciada do ComfyUI")
    parser.add_argument("--backend", default="nvidia", choices=("nvidia",), help="backend do runtime remoto")
    parser.add_argument("--host", default="127.0.0.1", help="host local do ComfyUI")
    parser.add_argument("--port", type=int, default=8189, help="porta local do ComfyUI")
    parser.add_argument("--startup-timeout", type=float, default=180.0, help="timeout de startup em segundos")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not _is_loopback(args.host):
            parser.error("--host deve ser loopback; exposicao externa nao faz parte deste bootstrap")
        if not 1 <= args.port <= 65535:
            parser.error("--port deve estar entre 1 e 65535")
        if args.startup_timeout <= 0:
            parser.error("--startup-timeout deve ser positivo")
        comfy = args.comfyui or (Path(os.environ["COMFYUI_PATH"]) if os.environ.get("COMFYUI_PATH") else DEFAULT_COMFYUI)
        result = RemoteBootstrap().run(
            args.manifest, comfy, backend=args.backend, host=args.host,
            port=args.port, startup_timeout=args.startup_timeout,
        )
        print(
            f"[EDx] readiness confirmada para instance_id={result['instance_id']} "
            f"(validacao operacional; inferencia nao executada)"
        )
        return 0
    except (RemoteBootstrapError, ProvisioningError) as exc:
        print(f"[EDx] bootstrap remoto falhou: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
