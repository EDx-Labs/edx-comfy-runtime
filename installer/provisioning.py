"""API reutilizável do provisionador modular EDx."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHUNK = 8 * 1024 * 1024

class ProvisioningError(RuntimeError): pass

def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return default or {}

def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)

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
    def format(self) -> str:
        def part(name: str, values: list[str]) -> str: return name + "\n" + ("\n".join("  - " + x for x in values) if values else "  - nenhum")
        size = f"{self.estimated_bytes / 2**30:.2f} GiB" if self.estimated_bytes else "desconhecido"
        return "\n".join((part("PROFILE", [self.profile_id]), part("DEPENDENCIES", self.profiles[1:]), part("ALREADY INSTALLED", self.already_installed), part("TO INSTALL", self.to_install), part("MODELS TO DOWNLOAD", self.models_to_download), f"ESTIMATED DOWNLOAD/DISK\n  - {size}", part("WARNINGS", self.warnings)))

class Provisioner:
    def __init__(self, manifest_root: Path | None = None) -> None:
        self.root = Path(manifest_root or ROOT / "installer")
        self.components = self._load("components")
        self.models = self._load("models", registry=True)
        self.workflow_sets = self._load("workflow_sets")
        self.profiles = self._load("profiles")
        self.capabilities = read_json(self.root / "capabilities.json").get("capabilities", {})
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
    def _valid_model(self, path: Path, model: dict[str, Any]) -> bool:
        if model.get("install_mode") == "node-managed": return path.is_dir() and any(path.iterdir())
        if not path.is_file() or path.stat().st_size == 0: return False
        return not model.get("sha256") or sha256(path) == model["sha256"]
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
            if marker.is_file() and read_json(marker).get("fingerprint") == fingerprint(source): result.already_installed.append("component:" + ident)
            elif target.exists() and not marker.exists(): result.warnings.append(f"component:{ident} já existe e não é gerenciado pelo EDx; será preservado")
            else: result.to_install.append("component:" + ident)
        for ident in models:
            model = self.models[ident]; target = self._model_path(comfy, model)
            if self._valid_model(target, model): result.already_installed.append("model:" + ident)
            elif model.get("install_mode") == "node-managed": result.warnings.append(f"model:{ident} é baixado pelo Loader do node ({model['repo_id']})")
            elif model.get("url"): result.models_to_download.append(ident); result.estimated_bytes += int(model.get("size_bytes", 0))
            else: result.warnings.append(f"model:{ident} sem URL conhecida; pendência documentada")
        for ident in sets:
            missing = False; destination = comfy / "user" / "default" / "workflows" / "EDx" / ident
            for rel in self.workflow_sets[ident].get("workflows", []):
                source = ROOT / rel
                if not source.is_file(): raise ProvisioningError(f"workflow ausente: {rel}")
                missing |= not (destination / source.name).is_file()
            (result.to_install if missing else result.already_installed).append("workflow_set:" + ident)
        for profile in profiles: result.warnings.extend(self.profiles[profile].get("warnings", []))
        return result
    def _component(self, ident: str, comfy: Path, install_deps: bool, state: dict[str, Any]) -> None:
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
            command = [sys.executable, "-m", "pip", "install", "-r", str(requirements)] if requirements.is_file() else [sys.executable, "-m", "pip", "install", "-e", str(target)] if (target / "pyproject.toml").is_file() else None
            if command: subprocess.run(command, check=True)
        state["components"][ident] = {"version": item.get("version", "0"), "fingerprint": version, "updated_at": time.time()}
    def _download(self, ident: str, target: Path, model: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True); partial = target.with_suffix(target.suffix + ".partial"); current = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "EDx-Provisioner/1.0"};
        if current: headers["Range"] = f"bytes={current}-"
        try:
            response = urllib.request.urlopen(urllib.request.Request(model["url"], headers=headers), timeout=120)
            if current and getattr(response, "status", None) != 206: partial.unlink(missing_ok=True); return self._download(ident, target, model)
            with response, partial.open("ab" if current else "wb") as stream:
                while block := response.read(CHUNK): stream.write(block); current += len(block); print(f"[EDx] {ident}: {current / 2**20:.0f} MiB", flush=True)
        except Exception as exc: raise ProvisioningError(f"download interrompido em {ident}; partial preservado: {exc}") from exc
        if not self._valid_model(partial, model): raise ProvisioningError(f"download inválido para {ident}; partial preservado para retomada")
        partial.replace(target); print(f"[EDx] modelo -> {target}")
    def _workflows(self, ident: str, comfy: Path, state: dict[str, Any]) -> None:
        destination = comfy / "user" / "default" / "workflows" / "EDx" / ident; destination.mkdir(parents=True, exist_ok=True); records = state.setdefault("workflows", {})
        for rel in self.workflow_sets[ident].get("workflows", []):
            source = ROOT / rel; target = destination / source.name; source_hash = sha256(source); known = records.get(str(target), {})
            if target.exists() and known and known.get("hash") != sha256(target): print(f"[EDx] preservando workflow editado: {target.name}"); continue
            if not target.exists() or sha256(target) != source_hash: shutil.copy2(source, target); print(f"[EDx] workflow -> {target}")
            records[str(target)] = {"workflow_set": ident, "source": rel, "hash": source_hash, "updated_at": time.time()}
    def install(self, plan: InstallPlan, *, skip_models: bool = False, skip_deps: bool = False) -> None:
        state_path = self._state_path(plan.comfy); state = read_json(state_path, {"schema_version": 1, "profiles": {}, "components": {}, "models": {}, "workflows": {}})
        for ident in plan.components: self._component(ident, plan.comfy, not skip_deps, state)
        if not skip_models:
            for ident in plan.models:
                model = self.models[ident]; target = self._model_path(plan.comfy, model)
                if model.get("url") and not self._valid_model(target, model): self._download(ident, target, model)
                if self._valid_model(target, model): state.setdefault("models", {})[ident] = {"version": model.get("version", "0"), "path": str(target), "updated_at": time.time()}
        for ident in plan.workflow_sets: self._workflows(ident, plan.comfy, state)
        state.setdefault("profiles", {})[plan.profile_id] = {"version": self.profiles[plan.profile_id].get("version", "0"), "resolved_profiles": plan.profiles, "provisioned_at": time.time()}
        write_json(state_path, state); print(f"[EDx] perfil {plan.profile_id} provisionado. Reinicie o ComfyUI.")
