"""Adapter para runtimes de backend associados a custom nodes."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


RuntimeModuleLoader = Callable[[Path], Any]
RuntimeDownloader = Callable[..., Path]
ProgressEmitter = Callable[..., None]
CancellationCheck = Callable[[Any], None]


@dataclass(frozen=True)
class RuntimeInstallResult:
    """Resultado do runtime sem assumir o schema de estado do provisionador."""

    backend_record: dict[str, Any] | None


class BackendRuntimeAdapter:
    """Coordena installers declarativos de runtime sem depender de ``Provisioner``."""

    def __init__(
        self,
        source_root: Path,
        components: Mapping[str, Mapping[str, Any]],
        *,
        load_installer: RuntimeModuleLoader,
        download_asset: RuntimeDownloader,
        emit: ProgressEmitter,
        check_cancelled: CancellationCheck,
        cancelled_errors: tuple[type[BaseException], ...],
        error_type: type[BaseException],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.source_root = Path(source_root)
        self.components = components
        self.load_installer = load_installer
        self.download_asset = download_asset
        self.emit = emit
        self.check_cancelled = check_cancelled
        self.cancelled_errors = cancelled_errors
        self.error_type = error_type
        self.clock = clock

    def target(self, ident: str, comfy: Path) -> Path:
        return Path(comfy) / "custom_nodes" / self.components[ident]["name"]

    def module(self, ident: str) -> Any:
        item = self.components[ident]
        return self.load_installer(self.source_root / item["source"] / item["backend_runtime"]["installer"])

    def status(self, ident: str, comfy: Path) -> dict[str, Any]:
        """Readiness estrutural; não executa o runtime instalado."""
        try:
            status = dict(self.module(ident).runtime_status(self.target(ident, comfy)))
        except Exception as exc:
            status = {
                "ready": False,
                "reason": "RUNTIME_INSTALLER_UNAVAILABLE",
                "detail": str(exc),
            }
        status["id"] = self.components[ident]["backend_runtime"].get("id", ident)
        status["component"] = ident
        return status

    def downloader(self, ident: str, progress, cancel_event):
        """Adapta o downloader comum ao contrato ``Asset -> arquivo`` do installer."""
        def fetch(asset, cache_dir: Path, offline: bool) -> Path:
            target = Path(cache_dir) / asset.name
            if offline and not target.is_file():
                raise self.error_type(f"cache offline sem {asset.name}")

            def relay(event: dict[str, Any]) -> None:
                if progress:
                    progress({**event, "kind": "runtime", "component": ident})

            self.download_asset(
                f"runtime:{ident}:{asset.name}",
                target,
                {"url": asset.url, "sha256": asset.sha256},
                progress=relay,
                cancel_event=cancel_event,
            )
            return target

        return fetch

    def install(
        self,
        ident: str,
        comfy: Path,
        *,
        progress=None,
        cancel_event=None,
    ) -> RuntimeInstallResult:
        item = self.components[ident]
        if not item.get("backend_runtime"):
            return RuntimeInstallResult(None)
        self.check_cancelled(cancel_event)
        target = self.target(ident, comfy)
        try:
            dev_link = target.exists() and os.path.samefile(self.source_root / item["source"], target)
        except OSError:
            dev_link = False
        if not target.exists():
            raise self.error_type(
                f"custom node ausente; runtime não pode ser instalado [RUNTIME_NODE_MISSING]: {target.name}"
            )
        if not (target / ".edx-component.json").is_file() and not dev_link:
            print(
                f"[EDx] node externo {target.name}: runtime não é instalado pelo Runtime "
                "(execute install_runtime.py)"
            )
            return RuntimeInstallResult(None)
        if self.status(ident, comfy)["ready"]:
            self.emit(progress, "RUNTIME_CACHED", component=ident)
            return RuntimeInstallResult(None)

        module = self.module(ident)
        self.emit(progress, "INSTALLING_COMPONENT", component=item["backend_runtime"].get("id", ident))
        try:
            platform_name = module.normalize_platform()
            hardware = module.detect_hardware(platform_name)
            candidates = module.select_backend_candidates(
                platform_name,
                hardware,
                requested_backend="auto",
                allow_fallback=True,
                build_from_source=False,
            )
            context = module.InstallerContext(
                root=target,
                platform_name=platform_name,
                architecture=hardware.architecture,
                release_tag=module.RELEASE_TAG,
                release_commit=module.RELEASE_COMMIT,
                executable_name="llama-server.exe" if platform_name == "windows" else "llama-server",
            )
            package = module.provision_runtime(
                context,
                hardware,
                candidates,
                cache_dir=context.runtime_root / ".downloads" / module.RELEASE_TAG,
                source_dir=context.runtime_root / ".sources" / f"llama.cpp-{module.RELEASE_TAG}",
                force=True,
                downloader=self.downloader(ident, progress, cancel_event),
                reraise=self.cancelled_errors,
            )
        except self.cancelled_errors:
            raise
        except Exception as exc:
            raise self.error_type(
                f"runtime llama.cpp não pôde ser instalado [RUNTIME_INSTALL_FAILED]: {exc}"
            ) from exc

        status = self.status(ident, comfy)
        if not status["ready"]:
            raise self.error_type(
                "runtime llama.cpp instalado mas inválido "
                f"[RUNTIME_INSTALL_INVALID]: {status.get('reason')}"
            )
        return RuntimeInstallResult({
            "id": item["backend_runtime"].get("id", ident),
            "tag": module.RELEASE_TAG,
            "backend": package.backend,
            "path": str(context.runtime_root),
            "owned": True,
            "installed_at": self.clock(),
        })
