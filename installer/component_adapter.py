"""Operações concretas de instalação de custom nodes declarados."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .storage import read_json, write_json


Fingerprint = Callable[[Path], str]
FileLinker = Callable[[str, str], str]
PhysicalTreeCheck = Callable[[Path, Path], bool]
CommandRunner = Callable[[list[str], Any, str], None]
CancellationCheck = Callable[[Any], None]
ProgressEmitter = Callable[..., None]


@dataclass(frozen=True)
class ComponentInstallResult:
    """Resultado filesystem da instalação, sem carregar estado do provisionador."""

    ident: str
    target: Path
    fingerprint: str | None
    managed: bool


class ComponentAdapter:
    """Instala um componente local usando dependências operacionais explícitas.

    O adapter não conhece ``Provisioner`` nem o schema do estado persistente.
    Ele prepara o destino, executa o comando de dependências e devolve um
    resultado para o orquestrador registrar.
    """

    def __init__(
        self,
        source_root: Path,
        components: Mapping[str, Mapping[str, Any]],
        *,
        python_executable: str,
        run_command: CommandRunner,
        fingerprint: Fingerprint,
        link_or_copy: FileLinker,
        same_physical_tree: PhysicalTreeCheck,
        check_cancelled: CancellationCheck,
        emit: ProgressEmitter,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source_root = Path(source_root)
        self.components = components
        self.python_executable = python_executable
        self.run_command = run_command
        self.fingerprint = fingerprint
        self.link_or_copy = link_or_copy
        self.same_physical_tree = same_physical_tree
        self.check_cancelled = check_cancelled
        self.emit = emit
        self.clock = clock

    def install(
        self,
        ident: str,
        comfy: Path,
        *,
        install_deps: bool,
        previous_fingerprint: str | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_event=None,
    ) -> ComponentInstallResult:
        self.check_cancelled(cancel_event)
        item = self.components[ident]
        source = self.source_root / item["source"]
        target = Path(comfy) / "custom_nodes" / item["name"]
        marker = target / ".edx-component.json"
        version = self.fingerprint(source)
        dev_link = target.exists() and os.path.samefile(source, target)

        if target.exists() and not marker.exists() and not dev_link:
            print(f"[EDx] preservando custom node externo: {target.name}")
            return ComponentInstallResult(ident, target, None, False)

        marker_data = read_json(marker)
        if dev_link:
            print(f"[EDx] component local ligado à fonte (dev) -> {target}")
        elif marker_data.get("fingerprint") != version and self.same_physical_tree(source, target):
            # A cópia pode usar hardlinks: atualiza apenas a prova de versão.
            write_json(marker, {
                "component": ident,
                "version": item.get("version", "0"),
                "fingerprint": version,
                "installed_at": self.clock(),
            })
        elif marker_data.get("fingerprint") != version:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                source,
                target,
                dirs_exist_ok=True,
                copy_function=self.link_or_copy,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            write_json(marker, {
                "component": ident,
                "version": item.get("version", "0"),
                "fingerprint": version,
                "installed_at": self.clock(),
            })
            print(f"[EDx] component local -> {target}")

        if install_deps and previous_fingerprint != version:
            requirements = target / "requirements.txt"
            method = item.get("install", "auto")
            pip = [self.python_executable, "-m", "pip", "install"]
            if method == "requirements":
                command = pip + ["-r", str(requirements)] if requirements.is_file() else None
            elif method == "editable":
                command = pip + ["-e", str(target)] if (target / "pyproject.toml").is_file() else None
            else:
                command = (
                    pip + ["-r", str(requirements)] if requirements.is_file()
                    else pip + ["-e", str(target)] if (target / "pyproject.toml").is_file()
                    else None
                )
            self.emit(progress, "INSTALLING_COMPONENT", component=ident)
            if command:
                self.run_command(command, cancel_event, item.get("name", ident))

        self.check_cancelled(cancel_event)
        return ComponentInstallResult(ident, target, version, True)
