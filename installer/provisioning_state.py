"""Estado persistente específico de uma instalação ComfyUI gerenciada."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import read_json, write_json


def empty_state() -> dict[str, Any]:
    """Retorna um novo estado inicial; nunca compartilha dicionários mutáveis."""
    return {"schema_version": 1, "profiles": {}, "components": {}, "models": {}, "workflows": {}}


class ProvisioningStateStore:
    """Adapter de leitura/gravação do estado do provisionador.

    A classe não migra nem interpreta registros: mantém o schema existente e
    deixa as regras de ownership/validação nos consumidores especializados.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        return read_json(self.path, empty_state() if default is None else default)

    def save(self, state: dict[str, Any]) -> None:
        write_json(self.path, state)
