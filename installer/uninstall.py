"""Desinstalação segura de capabilities/profiles gerenciados pelo Runtime.

Princípio: só remove o que consegue PROVAR que (1) é gerenciado pelo EDx, (2) pertence à capability removida,
(3) nenhuma outra capability instalada precisa, (4) não é conteúdo externo/manual. Na dúvida, preserva.
`plan_uninstall` é somente leitura; `uninstall` revalida o plano contra o filesystem antes de remover.
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import provisioning
from .provisioning import ProvisioningError, read_json, write_json

REMOVE = "REMOVE"
KEEP_SHARED = "KEEP_SHARED"        # usado por outra capability instalada (não relacionada por dependência)
KEEP_REQUIRED = "KEEP_REQUIRED"    # a capability removida é dependência de outra capability instalada
KEEP_UNMANAGED = "KEEP_UNMANAGED"  # sem prova de ownership (manual, editado, externo, link desconhecido)
MARKER = ".edx-component.json"


@dataclass
class UninstallItem:
    kind: str                    # model | partial | component | component_link | workflow_set | file
    ident: str
    path: str
    action: str
    reason: str
    size_bytes: int = 0          # bytes efetivamente liberados (hardlinks liberam 0)
    managed_files: list[str] = field(default_factory=list)  # component/workflow_set: caminhos relativos provados como gerenciados

    @property
    def key(self) -> tuple[str, str, str]: return (self.kind, self.ident, self.path)


@dataclass
class UninstallPlan:
    target: str
    comfy: str
    profiles: list[str]
    items: list[UninstallItem] = field(default_factory=list)
    installed_others: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int: return sum(item.size_bytes for item in self.items if item.action == REMOVE)
    def by_action(self, action: str) -> list[UninstallItem]: return [item for item in self.items if item.action == action]
    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "freed_bytes": self.freed_bytes}


@dataclass
class UninstallResult:
    target: str
    removed: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    freed_bytes: int = 0
    @property
    def ok(self) -> bool: return not self.errors


def _is_link(path: Path) -> bool:
    """Symlink ou junction/reparse point (Windows). Nunca seguido."""
    try: info = os.lstat(path)
    except OSError: return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _inside(root: Path, path: Path) -> bool:
    """path está dentro de root, resolvendo o PAI (o último componente pode ser um link e não é seguido)."""
    try:
        base = os.path.realpath(root); parent = os.path.realpath(path.parent)
        return os.path.commonpath([base, parent]) == base and path.name not in ("", ".", "..")
    except ValueError:
        return False


def _safe_name(value: str) -> bool:
    """Rejeita traversal/absolutos vindos de manifests."""
    parts = Path(str(value)).parts
    return bool(parts) and not Path(str(value)).is_absolute() and ".." not in parts and ":" not in str(value)


class Uninstaller:
    def __init__(self, provisioner: "provisioning.Provisioner") -> None:
        self.p = provisioner

    # ------------------------------------------------------------------ resolução
    def _target(self, target: str) -> tuple[str, list[str]]:
        """-> (profile id, perfis do grupo). install_group resolve para seu profile e membros."""
        key = str(target).strip().lower()
        group = self.p.install_groups.get(key)
        if group: return str(group.get("profile") or key), [str(m) for m in group.get("members", [])]
        if target in self.p.profiles: return target, []
        raise ProvisioningError(f"profile/install_group desconhecido: {target}")

    def _closure(self, profiles: list[str]) -> list[str]:
        ordered: list[str] = []
        for profile in profiles:
            for item in self.p._resolved_profiles(profile):
                if item not in ordered: ordered.append(item)
        return ordered

    def _resources(self, closure: list[str], kind: str) -> list[str]:
        return self.p._unique([x for profile in closure for x in self.p.profiles[profile].get(kind, [])])

    def _paths(self, comfy: Path, closure: list[str]) -> dict[str, dict[str, str]]:
        """caminho gerenciado -> {kind: ident} dos recursos de um conjunto de profiles."""
        result: dict[str, dict[str, str]] = {}
        for ident in self._resources(closure, "models"): result.setdefault(str(self._model_path(comfy, ident)), {})["model"] = ident
        for ident in self._resources(closure, "components"): result.setdefault(str(self._component_path(comfy, ident)), {})["component"] = ident
        for ident in self._resources(closure, "workflow_sets"): result.setdefault(str(self._workflow_path(comfy, ident)), {})["workflow_set"] = ident
        return result

    def _model_path(self, comfy: Path, ident: str) -> Path:
        model = self.p.models[ident]
        if not (_safe_name(model["directory"]) and _safe_name(model["filename"])): raise ProvisioningError(f"caminho inseguro no registry: {ident}")
        return self.p._model_path(comfy, model)

    def _component_path(self, comfy: Path, ident: str) -> Path:
        name = self.p.components[ident]["name"]
        if not _safe_name(name): raise ProvisioningError(f"nome inseguro no manifest: {ident}")
        return comfy / "custom_nodes" / name

    def _workflow_path(self, comfy: Path, ident: str) -> Path:
        if not _safe_name(ident): raise ProvisioningError(f"workflow_set inseguro: {ident}")
        return comfy / "user" / "default" / "workflows" / "EDx" / ident

    def _installed_others(self, comfy: Path, excluded: set[str]) -> list[str]:
        """Profiles realmente instalados (filesystem + estado) e explicitamente desejados, fora do alvo."""
        state = read_json(self.p._state_path(comfy), {}).get("profiles", {})
        result = []
        for profile_id, record in state.items():
            if profile_id in excluded or profile_id not in self.p.profiles: continue
            # Registro legado sem "explicit" é tratado como instalação explícita (preserva). Só deps puras ficam de fora.
            if not record.get("explicit", True): continue
            try:
                if self.p.profile_installation(profile_id, comfy)["installed"]: result.append(profile_id)
            except ProvisioningError: continue
        return result

    # ------------------------------------------------------------------ plano (somente leitura)
    def plan(self, target: str, comfy: Path) -> UninstallPlan:
        comfy = Path(comfy)
        if not (comfy / "models").is_dir(): raise ProvisioningError(f"não parece ComfyUI (models ausente): {comfy}")
        profile, members = self._target(target)
        closure = self._closure([profile, *members])
        plan = UninstallPlan(str(target), str(comfy), closure)
        others = self._installed_others(comfy, {profile, *members})
        plan.installed_others = others
        needed: dict[str, tuple[str, bool]] = {}  # caminho -> (profile que precisa, alvo é dependência dele?)
        for other in others:
            other_closure = self._closure([other])
            depends_on_target = bool({profile, *members} & set(other_closure))
            for path in self._paths(comfy, other_closure): needed.setdefault(path, (other, depends_on_target))
        state = read_json(self.p._state_path(comfy), {})
        for ident in self._resources(closure, "models"): self._plan_model(plan, comfy, ident, needed, state)
        for ident in self._resources(closure, "components"): self._plan_component(plan, comfy, ident, needed)
        for ident in self._resources(closure, "workflow_sets"): self._plan_workflows(plan, comfy, ident, needed, state)
        plan.warnings.append("dependências Python (pip) e o cache global não são desinstalados")
        return plan

    @staticmethod
    def _keep_for(needed: dict[str, tuple[str, bool]], path: Path) -> tuple[str, str] | None:
        hit = needed.get(str(path))
        if not hit: return None
        other, dependent = hit
        return (KEEP_REQUIRED, f"requerido por {other} (instalado)") if dependent else (KEEP_SHARED, f"compartilhado com {other} (instalado)")

    def _owned(self, state: dict[str, Any], ident: str, path: Path) -> bool:
        """Provado: o Runtime criou ESTE arquivo (owned + mesmo tamanho/mtime_ns). 'Registrado' não basta."""
        record = state.get("models", {}).get(ident, {})
        stamp = record.get("owned_stamp") or {}
        try: return record.get("owned") is True and record.get("path") == str(path) and stamp == self.p._file_stamp(path)
        except OSError: return False

    def _plan_model(self, plan: UninstallPlan, comfy: Path, ident: str, needed, state: dict[str, Any]) -> None:
        model = self.p.models[ident]; path = self._model_path(comfy, ident)
        partial = path.with_suffix(path.suffix + ".partial"); lock = path.with_suffix(path.suffix + ".lock")
        keep = self._keep_for(needed, path)
        if model.get("install_mode") == "node-managed":
            if os.path.lexists(path): plan.items.append(UninstallItem("model", ident, str(path), KEEP_UNMANAGED, "baixado e gerenciado pelo Loader do node"))
            return
        if os.path.lexists(path):
            if keep: plan.items.append(UninstallItem("model", ident, str(path), *keep))
            elif not _inside(comfy / "models", path): plan.items.append(UninstallItem("model", ident, str(path), KEEP_UNMANAGED, "fora da raiz de modelos"))
            elif _is_link(path) or not path.is_file(): plan.items.append(UninstallItem("model", ident, str(path), KEEP_UNMANAGED, "link ou não é arquivo regular"))
            elif not self._owned(state, ident, path): plan.items.append(UninstallItem("model", ident, str(path), KEEP_UNMANAGED, "propriedade não comprovada: arquivo preexistente/reutilizado, manual ou estado legado sem ownership"))
            elif not self.p._valid_model(path, model): plan.items.append(UninstallItem("model", ident, str(path), KEEP_UNMANAGED, "conteúdo não confere com o registry"))
            else: plan.items.append(UninstallItem("model", ident, str(path), REMOVE, "modelo gerenciado, exclusivo desta capability", path.stat().st_size))
        if os.path.lexists(partial) and not keep and not os.path.lexists(lock) and _inside(comfy / "models", partial) and not _is_link(partial):
            plan.items.append(UninstallItem("partial", ident, str(partial), REMOVE, "download parcial do provisionador, sem lock ativo", partial.stat().st_size))
        elif os.path.lexists(partial):
            plan.items.append(UninstallItem("partial", ident, str(partial), keep[0] if keep else KEEP_UNMANAGED, keep[1] if keep else "download parcial com lock ativo ou fora da raiz"))

    def _source_files(self, source: Path) -> dict[str, Path]:
        return {item.relative_to(source).as_posix(): item for item in provisioning._local_files(source)}

    def _plan_component(self, plan: UninstallPlan, comfy: Path, ident: str, needed) -> None:
        item = self.p.components[ident]; source = provisioning.ROOT / item["source"]; path = self._component_path(comfy, ident)
        if not os.path.lexists(path): return
        keep = self._keep_for(needed, path)
        if keep: plan.items.append(UninstallItem("component", ident, str(path), *keep)); return
        if not _inside(comfy / "custom_nodes", path): plan.items.append(UninstallItem("component", ident, str(path), KEEP_UNMANAGED, "fora de custom_nodes")); return
        if _is_link(path):
            try: ours = source.is_dir() and os.path.samefile(source, path)
            except OSError: ours = False
            plan.items.append(UninstallItem("component_link", ident, str(path), REMOVE if ours else KEEP_UNMANAGED,
                                            "link de desenvolvimento para a fonte; só o link é removido" if ours else "link para destino desconhecido"))
            return
        marker = read_json(path / MARKER)
        if not (path / MARKER).is_file() or marker.get("component") != ident:
            plan.items.append(UninstallItem("component", ident, str(path), KEEP_UNMANAGED, "sem marcador do EDx para este component (externo/manual)")); return
        managed, unmanaged, size = self._classify_tree(path, self._source_files(source), marker_owned=True)
        if not (marker.get("uninstalled_at") and managed == [MARKER]):  # marcador-resíduo de uma desinstalação anterior: nada a remover
            plan.items.append(UninstallItem("component", ident, str(path), REMOVE, "component gerenciado: só arquivos provados como do EDx", size, managed))
        for rel, why in unmanaged: plan.items.append(UninstallItem("file", ident, str(path / rel), KEEP_UNMANAGED, why))

    def _classify_tree(self, root: Path, source_files: dict[str, Path], marker_owned: bool) -> tuple[list[str], list[tuple[str, str]], int]:
        managed: list[str] = []; unmanaged: list[tuple[str, str]] = []; size = 0; pending_pyc: list[str] = []
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            for name in list(dirnames):
                full = Path(current) / name
                if _is_link(full):
                    unmanaged.append(((full.relative_to(root)).as_posix(), "link de diretório não é seguido")); dirnames.remove(name)
            for name in filenames:
                full = Path(current) / name; rel = full.relative_to(root).as_posix()
                if rel == MARKER:
                    if marker_owned: managed.append(rel)
                    continue
                src = source_files.get(rel)
                if _is_link(full):
                    try: ours = src is not None and os.path.samefile(src, full)
                    except OSError: ours = False
                    (managed.append(rel) if ours else unmanaged.append((rel, "link desconhecido")))
                    continue
                if src is not None:
                    try: same = os.path.samefile(src, full) or (src.stat().st_size == full.stat().st_size and provisioning.sha256(src) == provisioning.sha256(full))
                    except OSError: same = False
                    if same:
                        managed.append(rel); info = full.stat(); size += info.st_size if info.st_nlink == 1 else 0
                    else: unmanaged.append((rel, "arquivo modificado em relação à fonte"))
                elif full.parent.name == "__pycache__" and full.suffix == ".pyc": pending_pyc.append(rel)
                else: unmanaged.append((rel, "arquivo extra não gerenciado"))
        owned = set(managed)
        for rel in pending_pyc:  # bytecode derivado de um .py comprovadamente nosso
            pyc = Path(rel); module = pyc.name.split(".")[0]
            if (pyc.parent.parent / f"{module}.py").as_posix() in owned:
                managed.append(rel); size += (root / rel).stat().st_size
            else: unmanaged.append((rel, "bytecode sem fonte gerenciada"))
        return sorted(managed), sorted(unmanaged), size

    def _plan_workflows(self, plan: UninstallPlan, comfy: Path, ident: str, needed, state: dict[str, Any]) -> None:
        path = self._workflow_path(comfy, ident)
        if not os.path.lexists(path): return
        keep = self._keep_for(needed, path)
        if keep: plan.items.append(UninstallItem("workflow_set", ident, str(path), *keep)); return
        if not _inside(comfy / "user" / "default" / "workflows" / "EDx", path) or _is_link(path):
            plan.items.append(UninstallItem("workflow_set", ident, str(path), KEEP_UNMANAGED, "link ou fora da raiz de workflows")); return
        records = state.get("workflows", {}); managed: list[str] = []; size = 0; names: set[str] = set()
        for rel in self.p.workflow_sets[ident].get("workflows", []):
            target = path / Path(rel).name; names.add(target.name)
            if not target.is_file() or _is_link(target): continue
            record = records.get(str(target), {})
            if record.get("workflow_set") == ident and record.get("hash") == provisioning.sha256(target):
                managed.append(target.name); size += target.stat().st_size
            else: plan.items.append(UninstallItem("file", ident, str(target), KEEP_UNMANAGED, "workflow editado ou sem registro"))
        for extra in sorted(path.iterdir()):
            if extra.name not in names: plan.items.append(UninstallItem("file", ident, str(extra), KEEP_UNMANAGED, "arquivo extra não gerenciado"))
        if managed: plan.items.append(UninstallItem("workflow_set", ident, str(path), REMOVE, "workflows gerenciados e não editados", size, managed))

    # ------------------------------------------------------------------ execução
    def execute(self, target: str, comfy: Path, plan: UninstallPlan | None = None) -> UninstallResult:
        comfy = Path(comfy)
        fresh = self.plan(target, comfy)  # revalida premissas contra o filesystem atual
        if plan is not None and (plan.target != str(target) or Path(plan.comfy) != comfy):
            raise ProvisioningError("plano não corresponde ao alvo/instalação informados")
        fresh_by_key = {item.key: item for item in fresh.items}
        result = UninstallResult(str(target)); removed_items: list[UninstallItem] = []
        wanted = [item for item in (plan.items if plan is not None else fresh.items) if item.action == REMOVE]
        try:
            for planned in wanted:
                current = fresh_by_key.get(planned.key)
                if current is None or current.action != REMOVE:
                    result.skipped.append({"path": planned.path, "reason": "mudou desde o plano; preservado"}); continue
                files = sorted(set(current.managed_files) & set(planned.managed_files)) if planned.managed_files else []
                freed = self._remove(comfy, current, files)
                result.removed.append(planned.path); result.freed_bytes += freed; removed_items.append(current)
        except OSError as exc:
            result.errors.append({"path": planned.path, "reason": f"{type(exc).__name__}: {exc}"})  # para; o restante fica preservado
        finally:
            self._update_state(comfy, fresh, removed_items)
        return result

    def _remove(self, comfy: Path, item: UninstallItem, files: list[str]) -> int:
        path = Path(item.path); freed = 0
        if item.kind in ("model", "partial"):
            if not _inside(comfy / "models", path) or _is_link(path): raise OSError(f"caminho fora da raiz: {path}")
            size = path.stat().st_size; os.unlink(path); return size
        if item.kind == "component_link":
            if not _inside(comfy / "custom_nodes", path) or not _is_link(path): raise OSError(f"não é link gerenciado: {path}")
            try: os.unlink(path)
            except OSError: os.rmdir(path)  # junction/symlink de diretório no Windows: remove só o link
            return 0
        root = comfy / "custom_nodes" if item.kind == "component" else comfy / "user" / "default" / "workflows" / "EDx"
        if not _inside(root, path) or _is_link(path): raise OSError(f"caminho fora da raiz: {path}")
        ordered = [f for f in files if f != MARKER]  # o marcador só sai se mais nada sobrar
        extras_left = lambda: any(entry.name != MARKER for entry in path.iterdir()) if path.is_dir() else False
        for rel in ordered:
            full = path / rel
            if not _inside(path, full): raise OSError(f"travessia bloqueada: {rel}")
            try: info = os.lstat(full)
            except FileNotFoundError: continue
            freed += info.st_size if info.st_nlink == 1 and not stat.S_ISLNK(info.st_mode) else 0
            os.unlink(full)  # remove só o link/arquivo gerenciado; a fonte de um hardlink permanece intacta
            parent = full.parent
            while parent != path and not any(parent.iterdir()): parent.rmdir(); parent = parent.parent
        if MARKER in files and not extras_left() and (path / MARKER).is_file(): os.unlink(path / MARKER)
        if path.is_dir():
            remaining = [entry.name for entry in path.iterdir()]
            if not remaining: path.rmdir()
            elif remaining == [MARKER] or (MARKER in remaining and item.kind == "component"):
                # sobraram arquivos que não são nossos: o marcador deixa de afirmar "instalado" (fingerprint nulo)
                marker = read_json(path / MARKER); marker.update({"fingerprint": None, "uninstalled_at": time.time()}); write_json(path / MARKER, marker)
        return freed

    def _update_state(self, comfy: Path, plan: UninstallPlan, removed: list[UninstallItem]) -> None:
        state_path = self.p._state_path(comfy); state = read_json(state_path, {})
        if not state: return
        for item in removed:
            if item.kind == "model": state.get("models", {}).pop(item.ident, None); self.p._forget_model(Path(item.path))
            elif item.kind in ("component", "component_link"): state.get("components", {}).pop(item.ident, None)
            elif item.kind == "workflow_set":
                for key in [k for k, v in state.get("workflows", {}).items() if v.get("workflow_set") == item.ident and Path(k).parent == Path(item.path)]:
                    state["workflows"].pop(key, None)
        profile, members = self._target(plan.target)
        for profile_id in (profile, *members):  # deixa de ser desejado explicitamente; a validade continua vindo do filesystem
            if profile_id in state.get("profiles", {}): state["profiles"][profile_id]["explicit"] = False
        state["last_uninstall"] = {"target": plan.target, "at": time.time(), "removed": [item.path for item in removed]}
        write_json(state_path, state)
