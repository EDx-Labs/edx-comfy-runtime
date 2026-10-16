"""Catálogo e resolução declarativa do provisionador.

Este módulo é deliberadamente livre de operações de filesystem da instalação
ComfyUI, downloads, subprocessos e estado persistente. Ele transforma os
manifestos confiáveis em uma resolução determinística que pode ser testada sem
um host ComfyUI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ProvisioningError


@dataclass(frozen=True)
class ProfileResolution:
    """Perfis e recursos deduplicados, em ordem declarada de resolução."""

    requested: str
    profiles: tuple[str, ...]
    components: tuple[str, ...]
    models: tuple[str, ...]
    workflow_sets: tuple[str, ...]


class ManifestCatalog:
    """Fonte única de leitura e resolução dos manifestos do runtime."""

    def __init__(self, manifest_root: Path, *, source_root: Path) -> None:
        self.root = Path(manifest_root)
        self.source_root = Path(source_root)
        self.components = self._load("components")
        self.models = self._load("models", registry=True)
        self.workflow_sets = self._load("workflow_sets")
        self.profiles = self._load("profiles")
        try:
            capability_manifest = json.loads((self.root / "capabilities.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            capability_manifest = {}
        self.capabilities = capability_manifest.get("capabilities", {})
        self.install_groups = capability_manifest.get("install_groups", {})

    def _load(self, kind: str, registry: bool = False) -> dict[str, dict[str, Any]]:
        paths = [self.root / kind / "registry.json"] if registry else sorted((self.root / kind).glob("*.json"))
        result: dict[str, dict[str, Any]] = {}
        for path in paths:
            if not path.is_file():
                continue
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))[kind]
            except (KeyError, json.JSONDecodeError, OSError) as exc:
                raise ProvisioningError(f"manifest inválido: {path}") from exc
            for ident, entry in entries.items():
                if ident in result:
                    raise ProvisioningError(f"ID duplicado em {kind}: {ident}")
                result[ident] = entry
        return result

    def list_profiles(self) -> dict[str, dict[str, Any]]:
        return dict(sorted(self.profiles.items()))

    def list_capabilities(self) -> dict[str, dict[str, Any]]:
        """Metadados de descoberta; não instala providers."""
        return dict(sorted(self.capabilities.items()))

    def list_install_groups(self) -> dict[str, dict[str, Any]]:
        return dict(sorted(self.install_groups.items()))

    @staticmethod
    def unique(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))

    def resolve_profiles(self, requested: str) -> tuple[str, ...]:
        """Resolve dependências recursivas e recusa ciclos no manifesto."""
        ordered: list[str] = []
        active: list[str] = []
        seen: set[str] = set()

        def visit(profile: str) -> None:
            if profile not in self.profiles:
                raise ProvisioningError(f"profile inexistente: {profile}")
            if profile in active:
                raise ProvisioningError("ciclo de profiles: " + " -> ".join(active + [profile]))
            if profile in seen:
                return
            active.append(profile)
            for requirement in self.profiles[profile].get("requires", []):
                visit(requirement)
            active.pop()
            seen.add(profile)
            ordered.append(profile)

        visit(requested)
        return tuple(reversed(ordered))

    def resolve(self, profile_id: str) -> ProfileResolution:
        profiles = self.resolve_profiles(profile_id)
        components = self.unique([
            component
            for profile in profiles
            for component in self.profiles[profile].get("components", [])
        ])
        models = self.unique([
            model
            for profile in profiles
            for model in self.profiles[profile].get("models", [])
        ])
        workflow_sets = self.unique([
            workflow
            for profile in profiles
            for workflow in self.profiles[profile].get("workflow_sets", [])
        ])
        return ProfileResolution(
            requested=profile_id,
            profiles=tuple(profiles),
            components=tuple(components),
            models=tuple(models),
            workflow_sets=tuple(workflow_sets),
        )

    def profile_requirements(self, profile_id: str) -> dict[str, Any]:
        """Calcula requisitos declarados sem consultar o host atual."""
        resolution = self.resolve(profile_id)
        hardware = [self.profiles[profile].get("hardware", {}) for profile in resolution.profiles]
        hardware.extend(self.components[item].get("hardware", {}) for item in resolution.components)
        minimum = max((int(item.get("min_vram", 0)) for item in hardware), default=0)
        recommended = max((int(item.get("recommended_vram", 0)) for item in hardware), default=0)
        backend_sets = [
            {str(backend).lower() for backend in item.get("supported_backends", [])}
            for item in hardware
            if item.get("supported_backends")
        ]
        backends = sorted(set.intersection(*backend_sets)) if backend_sets else []
        strategies = self.unique([
            str(strategy)
            for item in hardware
            for strategy in item.get("execution_strategies", [])
        ])
        known_sizes = [
            int(self.models[item]["size_bytes"])
            for item in resolution.models
            if self.models[item].get("size_bytes")
        ]
        unknown_size_models = [item for item in resolution.models if not self.models[item].get("size_bytes")]
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
            "components": list(resolution.components),
            "models": list(resolution.models),
            "workflow_sets": list(resolution.workflow_sets),
        }

    def profile_provisionability(self, profile_id: str) -> dict[str, Any]:
        """Valida somente fontes declaradas, sem avaliar GPU/VRAM do host."""
        reasons: list[str] = []
        try:
            resolution = self.resolve(profile_id)
        except ProvisioningError:
            return {"installable": False, "reason_codes": ["PROFILE_INVALID"]}

        for ident in resolution.components:
            item = self.components.get(ident)
            if not item or not item.get("source") or not (self.source_root / item["source"]).is_dir():
                reasons.append(f"COMPONENT_UNAVAILABLE:{ident}")
            elif item.get("backend_runtime") and not (
                self.source_root / item["source"] / item["backend_runtime"]["installer"]
            ).is_file():
                reasons.append(f"RUNTIME_INSTALLER_UNAVAILABLE:{ident}")

        for ident in resolution.models:
            item = self.models.get(ident)
            if not item:
                reasons.append(f"MODEL_UNAVAILABLE:{ident}")
            elif item.get("install_mode") == "node-managed":
                if not item.get("repo_id"):
                    reasons.append(f"MODEL_SOURCE_UNAVAILABLE:{ident}")
            elif not item.get("url"):
                reasons.append(f"MODEL_SOURCE_UNAVAILABLE:{ident}")

        for ident in resolution.workflow_sets:
            item = self.workflow_sets.get(ident)
            if not item:
                reasons.append(f"WORKFLOW_SET_UNAVAILABLE:{ident}")
                continue
            for relative in item.get("workflows", []):
                if not (self.source_root / relative).is_file():
                    reasons.append(f"WORKFLOW_UNAVAILABLE:{ident}")
                    break
        return {"installable": not reasons, "reason_codes": reasons}

    def install_group_for(self, capability_id: str) -> str | None:
        requested = str(capability_id).lower()
        for ident, item in self.install_groups.items():
            if requested in {str(member).lower() for member in item.get("members", [])}:
                return ident
        return None

    def install_group_members(self, install_id: str) -> list[str]:
        item = self.install_groups.get(str(install_id).lower())
        return [str(member) for member in item.get("members", [])] if item else []

    def profiles_for_capability(self, capability_id: str) -> list[str]:
        """Resolve capability, alias ou provider a profiles declarados."""
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
