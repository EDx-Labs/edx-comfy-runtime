"""Objetos de dados produzidos pelo planejamento do provisionador."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InstallPlan:
    """Plano de instalação: dados declarativos + observações somente leitura."""

    profile_id: str
    profiles: list[str]
    components: list[str]
    models: list[str]
    workflow_sets: list[str]
    comfy: Path
    already_installed: list[str] = field(default_factory=list)
    to_install: list[str] = field(default_factory=list)
    models_to_download: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimated_bytes: int = 0
    download_size_complete: bool = True
    reused_assets: list[str] = field(default_factory=list)
    pending_assets: list[str] = field(default_factory=list)

    def format(self) -> str:
        def part(name: str, values: list[str]) -> str:
            return name + "\n" + ("\n".join("  - " + x for x in values) if values else "  - nenhum")

        size = f"{self.estimated_bytes / 2**30:.2f} GiB" if self.estimated_bytes else "desconhecido"
        return "\n".join((
            part("PROFILE", [self.profile_id]),
            part("DEPENDENCIES", self.profiles[1:]),
            part("ALREADY INSTALLED", self.already_installed),
            part("TO INSTALL", self.to_install),
            part("MODELS TO DOWNLOAD", self.models_to_download),
            f"ESTIMATED DOWNLOAD/DISK\n  - {size}",
            part("WARNINGS", self.warnings),
        ))
