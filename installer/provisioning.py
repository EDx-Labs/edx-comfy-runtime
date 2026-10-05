"""API reutilizável do provisionador modular EDx."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import http.client
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
CHUNK = 8 * 1024 * 1024
_DOWNLOAD_LOCKS: dict[str, threading.Lock] = {}
_DOWNLOAD_LOCKS_GUARD = threading.Lock()

Progress = Callable[[dict[str, Any]], None]

class ProvisioningError(RuntimeError): pass
_URL_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")

class ProvisioningCancelled(ProvisioningError):
    """Cooperative cancellation that keeps completed and resumable assets."""
    cancelled = True

class _TruncatedDownload(OSError):
    """Resposta terminou antes de size_bytes; transitória, partial é mantido."""

def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return default or {}

def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)

_VALIDATION_WRITE_LOCK = threading.Lock()
_HASH_LOCKS: dict[str, threading.Lock] = {}
_HASH_LOCKS_GUARD = threading.Lock()

def _hash_lock(path: Path) -> threading.Lock:
    with _HASH_LOCKS_GUARD: return _HASH_LOCKS.setdefault(str(path), threading.Lock())

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""): digest.update(chunk)
    return digest.hexdigest()

def fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(directory.rglob("*")):
        if item.is_file() and ".git" not in item.parts and "__pycache__" not in item.parts:
            stat = item.stat(); digest.update(f"{item.relative_to(directory)}\0{stat.st_size}\0{stat.st_mtime_ns}".encode())
    return digest.hexdigest()

def link_or_copy(source: str, target: str) -> str:
    """Evita duplicar bytes locais; fallback seguro para outro volume/FS."""
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target

@dataclass
class InstallPlan:
    profile_id: str; profiles: list[str]; components: list[str]; models: list[str]; workflow_sets: list[str]; comfy: Path
    already_installed: list[str] = field(default_factory=list)
    to_install: list[str] = field(default_factory=list)
    models_to_download: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimated_bytes: int = 0
    download_size_complete: bool = True
    reused_assets: list[str] = field(default_factory=list)
    pending_assets: list[str] = field(default_factory=list)
    def format(self) -> str:
        def part(name: str, values: list[str]) -> str: return name + "\n" + ("\n".join("  - " + x for x in values) if values else "  - nenhum")
        size = f"{self.estimated_bytes / 2**30:.2f} GiB" if self.estimated_bytes else "desconhecido"
        return "\n".join((part("PROFILE", [self.profile_id]), part("DEPENDENCIES", self.profiles[1:]), part("ALREADY INSTALLED", self.already_installed), part("TO INSTALL", self.to_install), part("MODELS TO DOWNLOAD", self.models_to_download), f"ESTIMATED DOWNLOAD/DISK\n  - {size}", part("WARNINGS", self.warnings)))

class Provisioner:
    def __init__(
        self, manifest_root: Path | None = None, *,
        python_executable: str | Path | None = None,
        opener=urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = Path(manifest_root or ROOT / "installer")
        self.python_executable = str(Path(python_executable or sys.executable).resolve())
        self.opener = opener
        self.sleeper = sleeper
        self.components = self._load("components")
        self.models = self._load("models", registry=True)
        self.workflow_sets = self._load("workflow_sets")
        self.profiles = self._load("profiles")
        capability_manifest = read_json(self.root / "capabilities.json")
        self.capabilities = capability_manifest.get("capabilities", {})
        self.install_groups = capability_manifest.get("install_groups", {})
    def _load(self, kind: str, registry: bool = False) -> dict[str, dict[str, Any]]:
        paths = [self.root / kind / "registry.json"] if registry else sorted((self.root / kind).glob("*.json"))
        result: dict[str, dict[str, Any]] = {}
        for path in paths:
            if not path.is_file(): continue
            try: entries = json.loads(path.read_text(encoding="utf-8"))[kind]
            except (KeyError, json.JSONDecodeError) as exc: raise ProvisioningError(f"manifest inválido: {path}") from exc
            for ident, entry in entries.items():
                if ident in result: raise ProvisioningError(f"ID duplicado em {kind}: {ident}")
                result[ident] = entry
        return result
    def list_profiles(self) -> dict[str, dict[str, Any]]: return dict(sorted(self.profiles.items()))
    def list_capabilities(self) -> dict[str, dict[str, Any]]:
        """Metadados de descoberta para o EDx Studio; não instala providers."""
        return dict(sorted(self.capabilities.items()))
    def list_install_groups(self) -> dict[str, dict[str, Any]]:
        return dict(sorted(self.install_groups.items()))
    def profile_requirements(self, profile_id: str) -> dict[str, Any]:
        """Return normalized requirements derived only from trusted manifests."""
        profiles = self._resolved_profiles(profile_id)
        components = self._unique([
            component for profile in profiles
            for component in self.profiles[profile].get("components", [])
        ])
        models = self._unique([
            model for profile in profiles
            for model in self.profiles[profile].get("models", [])
        ])
        hardware = [self.profiles[profile].get("hardware", {}) for profile in profiles]
        hardware.extend(self.components[item].get("hardware", {}) for item in components)
        minimum = max((int(item.get("min_vram", 0)) for item in hardware), default=0)
        recommended = max((int(item.get("recommended_vram", 0)) for item in hardware), default=0)
        backend_sets = [
            {str(backend).lower() for backend in item.get("supported_backends", [])}
            for item in hardware if item.get("supported_backends")
        ]
        backends = sorted(set.intersection(*backend_sets)) if backend_sets else []
        strategies = self._unique([
            str(strategy)
            for item in hardware for strategy in item.get("execution_strategies", [])
        ])
        known_sizes = [int(self.models[item]["size_bytes"]) for item in models if self.models[item].get("size_bytes")]
        unknown_size_models = [item for item in models if not self.models[item].get("size_bytes")]
        return {
            "min_vram_mb": minimum * 1024,
            "recommended_vram_mb": recommended * 1024,
            "supported_backends": backends,
            "backend_contract_declared": bool(backend_sets),
            "backend_conflict": bool(backend_sets and not backends),
            "execution_strategies": strategies,
            "download_bytes": sum(known_sizes),
            "download_size_complete": not unknown_size_models,
            "unknown_size_models": unknown_size_models,
            "components": components,
            "models": models,
            "workflow_sets": self._unique([
                workflow for profile in profiles
                for workflow in self.profiles[profile].get("workflow_sets", [])
            ]),
        }
    def profile_provisionability(self, profile_id: str) -> dict[str, Any]:
        """Validate whether trusted manifests can provision a profile.

        This deliberately does not evaluate the current machine's GPU/VRAM;
        provisioning assets and executing them are separate contracts.
        """
        reasons: list[str] = []
        try:
            profiles = self._resolved_profiles(profile_id)
        except ProvisioningError:
            return {"installable": False, "reason_codes": ["PROFILE_INVALID"]}
        components = self._unique([
            component for profile in profiles
            for component in self.profiles[profile].get("components", [])
        ])
        models = self._unique([
            model for profile in profiles
            for model in self.profiles[profile].get("models", [])
        ])
        workflow_sets = self._unique([
            workflow for profile in profiles
            for workflow in self.profiles[profile].get("workflow_sets", [])
        ])
        for ident in components:
            item = self.components.get(ident)
            if not item or not item.get("source") or not (ROOT / item["source"]).is_dir():
                reasons.append(f"COMPONENT_UNAVAILABLE:{ident}")
        for ident in models:
            item = self.models.get(ident)
            if not item:
                reasons.append(f"MODEL_UNAVAILABLE:{ident}")
            elif item.get("install_mode") == "node-managed":
                if not item.get("repo_id"):
                    reasons.append(f"MODEL_SOURCE_UNAVAILABLE:{ident}")
            elif not item.get("url"):
                reasons.append(f"MODEL_SOURCE_UNAVAILABLE:{ident}")
        for ident in workflow_sets:
            item = self.workflow_sets.get(ident)
            if not item:
                reasons.append(f"WORKFLOW_SET_UNAVAILABLE:{ident}")
                continue
            for relative in item.get("workflows", []):
                if not (ROOT / relative).is_file():
                    reasons.append(f"WORKFLOW_UNAVAILABLE:{ident}")
                    break
        return {"installable": not reasons, "reason_codes": reasons}
    def profile_installation(self, profile_id: str, comfy: Path) -> dict[str, Any]:
        """Inspect a profile without mutating the managed ComfyUI installation."""
        comfy = Path(comfy)
        if not (comfy / "models").is_dir():
            return {"installed": False, "profile_id": profile_id, "missing": ["base_runtime"]}
        state = read_json(self._state_path(comfy), {})
        plan = self.plan(profile_id, comfy)
        has_profile_state = profile_id in state.get("profiles", {})
        missing = ([] if has_profile_state else ["profile_state"])
        missing.extend(plan.to_install)
        missing.extend(f"model:{item}" for item in plan.models_to_download)
        return {
            "installed": has_profile_state and not plan.to_install and not plan.models_to_download,
            "profile_id": profile_id,
            "missing": missing,
            "warnings": plan.warnings,
            "reused_assets": plan.reused_assets,
            "pending_assets": plan.pending_assets,
            "downloaded_bytes": 0,
            "total_bytes": plan.estimated_bytes if plan.download_size_complete else None,
        }
    def capability_catalog(self, comfy: Path | None = None) -> list[dict[str, Any]]:
        """Normalized capability catalog consumed by orchestrators such as EDx Studio."""
        result: list[dict[str, Any]] = []
        for category, item in sorted(self.capabilities.items()):
            aliases = [str(value) for value in item.get("aliases", [])]
            providers = [str(value) for value in item.get("providers", []) if value in self.profiles]
            profiles = []
            for profile_id in providers:
                profile = self.profiles[profile_id]
                details = {
                    "id": profile_id,
                    "description": str(profile.get("description", "")),
                    "requirements": self.profile_requirements(profile_id),
                    "provisioning": self.profile_provisionability(profile_id),
                    "warnings": list(profile.get("warnings", [])),
                }
                if comfy is not None:
                    details["installation"] = self.profile_installation(profile_id, comfy)
                profiles.append(details)
            capability_id = aliases[0] if aliases else category
            result.append({
                "id": capability_id,
                "runtime_id": category,
                "name": str(item.get("name") or category.replace("-", " ").title()),
                "description": str(item.get("description") or (profiles[0]["description"] if profiles else "")),
                "category": category,
                "aliases": aliases,
                "providers": profiles,
                "install_group": self._install_group_for(capability_id),
                "operations": {
                    "install": any(profile["provisioning"]["installable"] for profile in profiles),
                    "repair": any(profile["provisioning"]["installable"] for profile in profiles),
                    "remove": False,
                },
            })
        return result
    def install_group_catalog(self, comfy: Path | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for ident, item in sorted(self.install_groups.items()):
            profile = str(item.get("profile") or ident)
            provisioning = self.profile_provisionability(profile)
            details = {
                "id": ident,
                "name": str(item.get("name") or ident.replace("-", " ").title()),
                "description": str(item.get("description", "")),
                "profile": profile,
                "members": [str(member) for member in item.get("members", [])],
                "requirements": self.profile_requirements(profile),
                "provisioning": provisioning,
                "operations": {
                    "install": provisioning["installable"],
                    "repair": provisioning["installable"],
                    "remove": False,
                },
            }
            if comfy is not None:
                details["installation"] = self.profile_installation(profile, comfy)
            result.append(details)
        return result
    def _install_group_for(self, capability_id: str) -> str | None:
        requested = str(capability_id).lower()
        for ident, item in self.install_groups.items():
            if requested in {str(member).lower() for member in item.get("members", [])}:
                return ident
        return None
    def install_group_members(self, install_id: str) -> list[str]:
        item = self.install_groups.get(str(install_id).lower())
        return [str(member) for member in item.get("members", [])] if item else []
    def profiles_for_capability(self, capability_id: str) -> list[str]:
        """Resolve a trusted capability/alias/provider to declared profiles."""
        requested = str(capability_id).strip().lower()
        if requested in self.profiles:
            return [requested]
        for ident, item in self.capabilities.items():
            names = {ident.lower(), *(str(value).lower() for value in item.get("aliases", []))}
            providers = [str(value) for value in item.get("providers", [])]
            if requested in names or requested in {value.lower() for value in providers}:
                return [value for value in providers if value in self.profiles]
        raise ProvisioningError(f"capability inexistente: {capability_id}")
    def resolve_profile(self, capability_id: str) -> str:
        profiles = self.profiles_for_capability(capability_id)
        if not profiles:
            raise ProvisioningError(f"capability sem profile instalável: {capability_id}")
        return profiles[0]
    @staticmethod
    def _unique(items: list[str]) -> list[str]: return list(dict.fromkeys(items))
    def _resolved_profiles(self, requested: str) -> list[str]:
        ordered: list[str] = []; active: list[str] = []; seen: set[str] = set()
        def visit(profile: str) -> None:
            if profile not in self.profiles: raise ProvisioningError(f"profile inexistente: {profile}")
            if profile in active: raise ProvisioningError("ciclo de profiles: " + " -> ".join(active + [profile]))
            if profile in seen: return
            active.append(profile)
            for requirement in self.profiles[profile].get("requires", []): visit(requirement)
            active.pop(); seen.add(profile); ordered.append(profile)
        visit(requested); return list(reversed(ordered))
    def _state_path(self, comfy: Path) -> Path: return comfy / "user" / "default" / "EDx" / "provisioning-state.json"
    def _model_path(self, comfy: Path, model: dict[str, Any]) -> Path: return comfy / "models" / model["directory"] / model["filename"]
    @staticmethod
    def _validation_cache_path(path: Path) -> Path | None:
        # <comfy>/models/... -> <comfy>/user/default/EDx/model-validation.json
        for parent in path.parents:
            if parent.name == "models": return parent.parent / "user" / "default" / "EDx" / "model-validation.json"
        return None
    @staticmethod
    def _file_stamp(path: Path) -> dict[str, int]:
        info = path.stat(); return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}
    def _cached_valid(self, path: Path, model: dict[str, Any]) -> bool:
        cache = self._validation_cache_path(path)
        if cache is None: return False
        entry = read_json(cache, {}).get("models", {}).get(str(path))
        return bool(entry) and entry.get("sha256") == model["sha256"] and {k: entry.get(k) for k in ("size", "mtime_ns")} == self._file_stamp(path)
    def _remember_model(self, path: Path, model: dict[str, Any]) -> None:
        """Persist a successful SHA-256 validation (atomic write) keyed by path + size + mtime_ns."""
        cache = self._validation_cache_path(path)
        if cache is None or not model.get("sha256") or not path.is_file(): return
        with _VALIDATION_WRITE_LOCK:
            data = read_json(cache, {"schema_version": 1, "models": {}}); data.setdefault("models", {})
            data["models"][str(path)] = {"sha256": model["sha256"], "size_bytes": model.get("size_bytes"), **self._file_stamp(path), "validated_at": time.time()}
            write_json(cache, data)
    def _forget_model(self, path: Path) -> None:
        cache = self._validation_cache_path(path)
        if cache is None: return
        with _VALIDATION_WRITE_LOCK:
            data = read_json(cache, {})
            if data.get("models", {}).pop(str(path), None) is not None: write_json(cache, data)
    def _valid_model(self, path: Path, model: dict[str, Any], *, force: bool = False, record: bool = True) -> bool:
        """force=True ignora o cache e relê o arquivo (install/repair); record=False não persiste (ex.: .partial)."""
        if model.get("install_mode") == "node-managed": return path.is_dir() and any(path.iterdir())
        if not path.is_file() or path.stat().st_size == 0: return False
        if model.get("size_bytes") and path.stat().st_size != int(model["size_bytes"]): return False
        if not model.get("sha256"): return True
        if not force and self._cached_valid(path, model): return True
        with _hash_lock(path):  # single-flight: consultas simultâneas aguardam o mesmo hash
            if not force and self._cached_valid(path, model): return True
            if sha256(path) != model["sha256"]: self._forget_model(path); return False
            if record: self._remember_model(path, model)
            return True
    def plan(self, profile_id: str, comfy: Path) -> InstallPlan:
        if not (comfy / "models").is_dir(): raise ProvisioningError(f"não parece ComfyUI (models ausente): {comfy}")
        profiles = self._resolved_profiles(profile_id)
        components = self._unique([x for p in profiles for x in self.profiles[p].get("components", [])])
        models = self._unique([x for p in profiles for x in self.profiles[p].get("models", [])])
        sets = self._unique([x for p in profiles for x in self.profiles[p].get("workflow_sets", [])])
        for label, ids, registry in (("component", components, self.components), ("model", models, self.models), ("workflow_set", sets, self.workflow_sets)):
            for ident in ids:
                if ident not in registry: raise ProvisioningError(f"{label} inexistente: {ident}")
        result = InstallPlan(profile_id, profiles, components, models, sets, comfy)
        for ident in components:
            component = self.components[ident]; source = ROOT / component["source"]; target = comfy / "custom_nodes" / component["name"]; marker = target / ".edx-component.json"
            if not source.is_dir(): raise ProvisioningError(f"fonte local ausente: {component['source']}")
            if marker.is_file() and read_json(marker).get("fingerprint") == fingerprint(source):
                result.already_installed.append("component:" + ident); result.reused_assets.append("component:" + ident)
            elif target.exists() and not marker.exists(): result.warnings.append(f"component:{ident} já existe e não é gerenciado pelo EDx; será preservado")
            else:
                result.to_install.append("component:" + ident); result.pending_assets.append("component:" + ident)
        for ident in models:
            model = self.models[ident]; target = self._model_path(comfy, model)
            if self._valid_model(target, model):
                result.already_installed.append("model:" + ident); result.reused_assets.append("model:" + ident)
            elif model.get("install_mode") == "node-managed": result.warnings.append(f"model:{ident} é baixado pelo Loader do node ({model['repo_id']})")
            elif model.get("url"):
                result.models_to_download.append(ident); result.pending_assets.append("model:" + ident)
                size = int(model.get("size_bytes", 0))
                if size:
                    partial = target.with_suffix(target.suffix + ".partial")
                    present = min(partial.stat().st_size, size) if partial.is_file() else 0
                    result.estimated_bytes += size - present
                else:
                    result.download_size_complete = False
            else: result.warnings.append(f"model:{ident} sem URL conhecida; pendência documentada")
        for ident in sets:
            missing = False; destination = comfy / "user" / "default" / "workflows" / "EDx" / ident
            for rel in self.workflow_sets[ident].get("workflows", []):
                source = ROOT / rel
                if not source.is_file(): raise ProvisioningError(f"workflow ausente: {rel}")
                missing |= not (destination / source.name).is_file()
            asset = "workflow_set:" + ident
            (result.to_install if missing else result.already_installed).append(asset)
            (result.pending_assets if missing else result.reused_assets).append(asset)
        for profile in profiles: result.warnings.extend(self.profiles[profile].get("warnings", []))
        return result
    @staticmethod
    def _emit(callback: Progress | None, phase: str, **details: Any) -> None:
        if callback: callback({"phase": phase, **details})
    @staticmethod
    def _check_cancelled(cancel_event, message: str = "instalação cancelada") -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ProvisioningCancelled(message)
    @staticmethod
    def _sanitize_command(command: list[str]) -> str:
        parts = []
        for part in command:
            part = _URL_CREDENTIALS.sub(r"***@", str(part))
            parts.append(part)
        return " ".join(parts)
    @staticmethod
    def _tail(text: str, limit: int = 4000) -> str:
        text = _URL_CREDENTIALS.sub(r"***@", text.strip())
        return text if len(text) <= limit else "..." + text[-limit:]
    @staticmethod
    def _run_cancellable(command: list[str], cancel_event=None, label: str = "") -> None:
        Provisioner._check_cancelled(cancel_event)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(command, stdout=out, stderr=err)
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.1):
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                    raise ProvisioningCancelled("instalação de dependências cancelada")
                if cancel_event is None:
                    process.wait(); break
            if process.returncode:
                out.seek(0); err.seek(0)
                stdout = out.read().decode("utf-8", "replace"); stderr = err.read().decode("utf-8", "replace")
                detail = Provisioner._tail(stderr) or Provisioner._tail(stdout)
                message = f"{label or 'command'} setup failed (exit code {process.returncode}): {detail}\ncommand: {Provisioner._sanitize_command(command)}"
                if stderr.strip() and stdout.strip():
                    message += "\nstdout (tail): " + Provisioner._tail(stdout, 1500)
                raise ProvisioningError(message)
    def _component(self, ident: str, comfy: Path, install_deps: bool, state: dict[str, Any], progress: Progress | None = None, cancel_event=None) -> None:
        self._check_cancelled(cancel_event)
        item = self.components[ident]; source = ROOT / item["source"]; target = comfy / "custom_nodes" / item["name"]; marker = target / ".edx-component.json"; version = fingerprint(source)
        if target.exists() and not marker.exists(): print(f"[EDx] preservando custom node externo: {target.name}"); return
        if read_json(marker).get("fingerprint") != version:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True, copy_function=link_or_copy,
                            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            write_json(marker, {"component": ident, "version": item.get("version", "0"), "fingerprint": version, "installed_at": time.time()}); print(f"[EDx] component local -> {target}")
        old = state.setdefault("components", {}).get(ident, {})
        if install_deps and old.get("fingerprint") != version:
            requirements = target / "requirements.txt"
            # "install": "requirements" -> só requirements.txt (sem pip install -e); omitido mantém o comportamento legado.
            method = item.get("install", "auto")
            pip = [self.python_executable, "-m", "pip", "install"]
            if method == "requirements": command = pip + ["-r", str(requirements)] if requirements.is_file() else None
            elif method == "editable": command = pip + ["-e", str(target)] if (target / "pyproject.toml").is_file() else None
            else: command = pip + ["-r", str(requirements)] if requirements.is_file() else pip + ["-e", str(target)] if (target / "pyproject.toml").is_file() else None
            self._emit(progress, "INSTALLING_COMPONENT", component=ident)
            if command: self._run_cancellable(command, cancel_event, item.get("name", ident))
        self._check_cancelled(cancel_event)
        state["components"][ident] = {"version": item.get("version", "0"), "fingerprint": version, "updated_at": time.time()}
    @contextmanager
    def _asset_lock(self, target: Path, cancel_event=None):
        """Serialize a model install across Studio processes, not only threads."""
        lock_path = target.with_suffix(target.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if cancel_event is not None and cancel_event.is_set():
                    raise ProvisioningCancelled("download cancelado enquanto aguardava outro provisionamento")
                try:
                    owner = json.loads(lock_path.read_text(encoding="utf-8"))
                    owner_pid = int(owner.get("pid", 0))
                    owner_alive = owner_pid > 0
                    if owner_alive:
                        try:
                            os.kill(owner_pid, 0)
                        except ProcessLookupError:
                            owner_alive = False
                        except PermissionError:
                            owner_alive = True
                        except (OSError, ValueError):
                            owner_alive = False
                    if not owner_alive:
                        lock_path.unlink(missing_ok=True)
                        continue
                except (OSError, ValueError, json.JSONDecodeError):
                    try:
                        if time.time() - lock_path.stat().st_mtime > 24 * 60 * 60:
                            lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        continue
                self.sleeper(0.25)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"pid": os.getpid(), "created_at": time.time()}))
            break
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)
    @contextmanager
    def _thread_asset_lock(self, target: Path, cancel_event=None):
        key = str(target.resolve())
        with _DOWNLOAD_LOCKS_GUARD:
            lock = _DOWNLOAD_LOCKS.setdefault(key, threading.Lock())
        while not lock.acquire(timeout=0.1):
            self._check_cancelled(
                cancel_event, "download cancelado enquanto aguardava outro provisionamento",
            )
        try:
            yield
        finally:
            lock.release()

    TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})

    @classmethod
    def _is_transient(cls, exc: BaseException) -> bool:
        """Falha de rede que vale uma nova tentativa; HTTP 4xx definitivo (404/401/403...) não."""
        if isinstance(exc, urllib.error.HTTPError): return exc.code in cls.TRANSIENT_HTTP
        return isinstance(exc, (urllib.error.URLError, OSError, TimeoutError, ConnectionError, http.client.HTTPException, _TruncatedDownload))

    def _backoff(self, delay: float, cancel_event=None) -> None:
        """Espera do retry; acorda imediatamente no cancelamento."""
        if cancel_event is None: self.sleeper(delay)
        elif cancel_event.wait(delay): raise ProvisioningCancelled("download cancelado durante a espera de retry; partial preservado")

    def _download(self, ident: str, target: Path, model: dict[str, Any], *, progress: Progress | None = None, cancel_event=None, retries: int = 3) -> bool:
        attempts = max(1, retries)
        with self._thread_asset_lock(target, cancel_event), self._asset_lock(target, cancel_event):
            self._check_cancelled(cancel_event, f"download cancelado antes de {ident}")
            if self._valid_model(target, model): return False
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".partial")
            expected = int(model.get("size_bytes", 0)) or None
            for attempt in range(attempts):
                self._check_cancelled(cancel_event, f"download cancelado antes de {ident}")
                current = partial.stat().st_size if partial.exists() else 0
                if expected and current > expected: partial.unlink(missing_ok=True); current = 0
                headers = {"User-Agent": "EDx-Provisioner/2.0"}
                if current: headers["Range"] = f"bytes={current}-"
                info = {"attempt": attempt + 1, "max_attempts": attempts}
                self._emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=current, bytes_total=expected, progress=(current / expected if expected else None), **info)
                try:
                    response = self.opener(urllib.request.Request(model["url"], headers=headers), timeout=120)
                    if current and not self._resumes_at(response, current):
                        # servidor ignorou/alterou o Range: reinicia o asset do zero (sem fingir resume)
                        partial.unlink(missing_ok=True); current = 0
                        response.close()
                        response = self.opener(urllib.request.Request(model["url"], headers={"User-Agent": "EDx-Provisioner/2.0"}), timeout=120)
                    with response, partial.open("ab" if current else "wb") as stream:
                        while block := response.read(CHUNK):
                            if cancel_event is not None and cancel_event.is_set():
                                raise ProvisioningCancelled(f"download cancelado em {ident}; partial preservado")
                            stream.write(block); current += len(block)
                            self._emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=current, bytes_total=expected, progress=(min(1.0, current / expected) if expected else None), **info)
                    if expected and current < expected:
                        raise _TruncatedDownload(f"conexão encerrada em {current}/{expected} bytes")  # mantém o partial para resume
                    self._emit(
                        progress, "VERIFYING_ASSET", asset=ident,
                        bytes_downloaded=current, bytes_total=expected,
                        progress=(min(1.0, current / expected) if expected else None),
                    )
                    if not self._valid_model(partial, model, force=True, record=False):
                        partial.unlink(missing_ok=True)
                        raise ProvisioningError(f"download inválido para {ident}")
                    os.replace(partial, target)
                    self._remember_model(target, model)  # reaproveita o SHA já verificado do .partial
                    self._emit(
                        progress, "ASSET_READY", asset=ident,
                        bytes_downloaded=current, bytes_total=expected,
                        progress=(1.0 if expected else None),
                    )
                    return True
                except (ProvisioningError, ProvisioningCancelled):
                    raise
                except Exception as exc:
                    if not self._is_transient(exc):
                        raise ProvisioningError(f"download de {ident} falhou sem possibilidade de retry: {exc}") from exc
                    if attempt + 1 >= attempts:
                        raise ProvisioningError(f"download interrompido em {ident}; partial preservado") from exc
                    delay = min(8.0, 0.5 * (2 ** attempt))
                    self._emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=current, bytes_total=expected, progress=(current / expected if expected else None), retry_in=delay, message=f"Falha transitória; nova tentativa em {delay:g}s", **info)
                    self._backoff(delay, cancel_event)
    @staticmethod
    def _resumes_at(response: Any, offset: int) -> bool:
        if getattr(response, "status", None) != 206: return False
        content_range = getattr(response, "headers", None) and response.headers.get("Content-Range")
        if not content_range: return True  # 206 sem header (servidores simples/mocks): confia no status
        match = re.match(r"bytes (\d+)-", content_range)
        return bool(match) and int(match.group(1)) == offset
    def _workflows(self, ident: str, comfy: Path, state: dict[str, Any]) -> None:
        destination = comfy / "user" / "default" / "workflows" / "EDx" / ident; destination.mkdir(parents=True, exist_ok=True); records = state.setdefault("workflows", {})
        for rel in self.workflow_sets[ident].get("workflows", []):
            source = ROOT / rel; target = destination / source.name; source_hash = sha256(source); known = records.get(str(target), {})
            if target.exists() and known and known.get("hash") != sha256(target): print(f"[EDx] preservando workflow editado: {target.name}"); continue
            if not target.exists() or sha256(target) != source_hash: shutil.copy2(source, target); print(f"[EDx] workflow -> {target}")
            records[str(target)] = {"workflow_set": ident, "source": rel, "hash": source_hash, "updated_at": time.time()}
    def install(
        self, plan: InstallPlan, *, skip_models: bool = False,
        skip_deps: bool = False, progress: Progress | None = None,
        cancel_event=None, before_commit: Callable[[], None] | None = None,
        force_verify: bool = False,
    ) -> None:
        state_path = self._state_path(plan.comfy); state = read_json(state_path, {"schema_version": 1, "profiles": {}, "components": {}, "models": {}, "workflows": {}})
        self._check_cancelled(cancel_event)
        pending_models = [
            ident for ident in plan.models
            if self.models[ident].get("url")
            and not self._valid_model(self._model_path(plan.comfy, self.models[ident]), self.models[ident], force=force_verify)
        ]
        baselines: dict[str, int] = {}
        remaining_sizes: dict[str, int | None] = {}
        for ident in pending_models:
            model = self.models[ident]; target = self._model_path(plan.comfy, model)
            expected = int(model.get("size_bytes", 0))
            partial = target.with_suffix(target.suffix + ".partial")
            baseline = min(partial.stat().st_size, expected) if expected and partial.is_file() else 0
            baselines[ident] = baseline
            remaining_sizes[ident] = expected - baseline if expected else None
        download_size_complete = all(value is not None for value in remaining_sizes.values())
        remaining_total = (
            sum(int(value or 0) for value in remaining_sizes.values())
            if download_size_complete else None
        )
        live_reused = list(plan.reused_assets)
        live_pending = list(plan.pending_assets)
        for ident in plan.models:
            asset = "model:" + ident
            valid = self._valid_model(self._model_path(plan.comfy, self.models[ident]), self.models[ident])
            if valid:
                if asset not in live_reused: live_reused.append(asset)
                if asset in live_pending: live_pending.remove(asset)
            elif self.models[ident].get("url") and asset not in live_pending:
                live_pending.append(asset)
        self._emit(
            progress, "PLANNING_ASSETS", reused_assets=live_reused,
            pending_assets=live_pending, bytes_downloaded=0,
            bytes_total=remaining_total,
            progress=(0.0 if remaining_total else None),
        )
        for ident in plan.components:
            self._component(ident, plan.comfy, not skip_deps, state, progress, cancel_event)
        if not skip_models:
            pending_positions = {ident: index for index, ident in enumerate(pending_models, 1)}
            completed_bytes = 0
            for ident in plan.models:
                self._check_cancelled(cancel_event)
                model = self.models[ident]; target = self._model_path(plan.comfy, model)
                if model.get("url") and not self._valid_model(target, model):
                    def model_progress(event: dict[str, Any], *, _ident=ident) -> None:
                        if progress:
                            current = max(0, int(event.get("bytes_downloaded") or 0) - baselines.get(_ident, 0))
                            total = remaining_total
                            aggregate = completed_bytes + current
                            progress({
                                **event,
                                "current_asset": _ident,
                                "bytes_downloaded": aggregate,
                                "bytes_total": total,
                                "progress": (min(1.0, aggregate / total) if total else None),
                                "reused_assets": live_reused,
                                "pending_assets": live_pending,
                                "model_index": pending_positions[_ident],
                                "model_total": len(pending_models),
                            })
                    downloaded = self._download(ident, target, model, progress=model_progress, cancel_event=cancel_event)
                    if downloaded is not False and remaining_sizes.get(ident) is not None:
                        completed_bytes += int(remaining_sizes[ident] or 0)
                if self._valid_model(target, model): state.setdefault("models", {})[ident] = {"version": model.get("version", "0"), "path": str(target), "updated_at": time.time()}
        for ident in plan.workflow_sets:
            self._check_cancelled(cancel_event)
            self._emit(progress, "INSTALLING_COMPONENT", component=f"workflow:{ident}")
            self._workflows(ident, plan.comfy, state)
        self._check_cancelled(cancel_event)
        if before_commit:
            before_commit()
        for profile_id in plan.profiles:
            state.setdefault("profiles", {})[profile_id] = {
                "version": self.profiles[profile_id].get("version", "0"),
                "resolved_profiles": self._resolved_profiles(profile_id),
                "provisioned_at": time.time(),
            }
        write_json(state_path, state); print(f"[EDx] perfil {plan.profile_id} provisionado. Reinicie o ComfyUI.")
