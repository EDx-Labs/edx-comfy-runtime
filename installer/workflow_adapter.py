"""Adapter de instalação dos workflow sets no filesystem do ComfyUI."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable


Digest = Callable[[Path], str]


class WorkflowInstaller:
    """Instala workflows declarados sem interpretar ou reescrever seus JSONs."""

    def __init__(self, *, source_root: Path, workflow_sets: dict[str, dict[str, Any]], digest: Digest) -> None:
        self.source_root = Path(source_root)
        self.workflow_sets = workflow_sets
        self.digest = digest

    def install(self, ident: str, comfy: Path, state: dict[str, Any]) -> None:
        destination = Path(comfy) / "user" / "default" / "workflows" / "EDx" / ident
        destination.mkdir(parents=True, exist_ok=True)
        records = state.setdefault("workflows", {})
        for relative in self.workflow_sets[ident].get("workflows", []):
            source = self.source_root / relative
            target = destination / source.name
            source_hash = self.digest(source)
            known = records.get(str(target), {})
            if target.exists() and known and known.get("hash") != self.digest(target):
                print(f"[EDx] preservando workflow editado: {target.name}")
                continue
            if not target.exists() or self.digest(target) != source_hash:
                shutil.copy2(source, target)
                print(f"[EDx] workflow -> {target}")
            records[str(target)] = {
                "workflow_set": ident,
                "source": relative,
                "hash": source_hash,
                "updated_at": time.time(),
            }
