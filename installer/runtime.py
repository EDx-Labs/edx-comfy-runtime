"""Managed, manifest-driven installation of the base ComfyUI runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .provisioning import ProvisioningCancelled, ProvisioningError, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
Progress = Callable[[dict[str, Any]], None]


def runtime_python(comfy: Path) -> Path:
    relative = Path(".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    return comfy / relative


def bundled_python() -> Path | None:
    """The standalone interpreter shipped next to the Runtime package (``python-runtime/``), when present.

    The packaged Studio Agent carries it so creating the managed ComfyUI environment never needs a system Python
    (``sys.executable`` of the frozen agent cannot create virtual environments).
    """
    name = "python.exe" if os.name == "nt" else "bin/python3"
    for candidate in (ROOT.parent / "python-runtime" / name, Path(sys.executable).resolve().parent / "python-runtime" / name):
        if candidate.is_file():
            return candidate
    return None


def _default_download(url: str, target: Path, cancel_event=None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "edx-studio-runtime"})
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:  # noqa: S310 - https URL from the trusted manifest
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ProvisioningCancelled("instalação do Runtime cancelada")
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    os.replace(partial, target)


class BaseRuntimeInstaller:
    """Install ComfyUI from the trusted Runtime manifest, never from UI input."""

    def __init__(
        self,
        manifest: Path | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        python_executable: str | Path | None = None,
        downloader: Callable[..., None] | None = None,
    ) -> None:
        self.manifest_path = Path(manifest or ROOT / "installer" / "runtime.json")
        self.manifest = read_json(self.manifest_path).get("comfyui", {})
        self.runner = runner
        self.downloader = downloader or _default_download
        self.python_executable = str(Path(python_executable or bundled_python() or sys.executable).resolve())
        required = {"repository", "revision", "entrypoint", "requirements"}
        if not required.issubset(self.manifest):
            raise ProvisioningError("manifesto do runtime base está incompleto")

    def inspect(self, target: str | Path) -> dict[str, Any]:
        root = Path(target).resolve()
        marker = read_json(root / ".edx-runtime.json")
        valid = (
            (root / self.manifest["entrypoint"]).is_file()
            and (root / "models").is_dir()
            and runtime_python(root).is_file()
            and marker.get("revision") == self.manifest["revision"]
        )
        partial = root.with_name(root.name + ".installing")
        return {
            "installed": valid,
            "path": str(root),
            "revision": marker.get("revision"),
            "backend": marker.get("backend"),
            "partial": partial.exists(),
        }

    @staticmethod
    def _emit(progress: Progress | None, phase: str, **details: Any) -> None:
        if progress:
            progress({"phase": phase, **details})

    def _run(self, command: list[str], *, cwd: Path | None = None, cancel_event=None) -> subprocess.CompletedProcess:
        if cancel_event is not None and cancel_event.is_set():
            raise ProvisioningCancelled("instalação do Runtime cancelada")
        if cancel_event is not None and self.runner is subprocess.run:
            process = subprocess.Popen(
                command, cwd=str(cwd) if cwd else None,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            while process.poll() is None:
                if cancel_event.wait(0.1):
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                    raise ProvisioningCancelled("instalação do Runtime cancelada")
            result = subprocess.CompletedProcess(command, process.returncode, "", "")
        else:
            result = self.runner(
                command, cwd=str(cwd) if cwd else None, check=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        if cancel_event is not None and cancel_event.is_set():
            raise ProvisioningCancelled("instalação do Runtime cancelada")
        if result.returncode:
            raise ProvisioningError("não foi possível preparar o mecanismo local de geração")
        return result

    def _fetch_archive(self, partial: Path, archive: dict[str, Any], *, progress: Progress | None = None, cancel_event=None) -> None:
        """Download the pinned ComfyUI source archive and unpack it into ``partial`` (resumable by commit marker)."""
        commit = str(archive.get("commit") or "")
        url = str(archive.get("url") or "")
        if not commit or not url.startswith("https://"):
            raise ProvisioningError("manifesto do runtime base está incompleto")
        marker = partial / ".edx-source.json"
        if (partial / self.manifest["entrypoint"]).is_file() and read_json(marker).get("commit") == commit:
            return
        if partial.exists():
            shutil.rmtree(partial)
        self._emit(progress, "INSTALLING_COMPONENT", component="comfyui-source")
        bundle = partial.with_name(partial.name + ".zip")
        unpack = partial.with_name(partial.name + ".unpack")
        try:
            self.downloader(url, bundle, cancel_event)
            if unpack.exists():
                shutil.rmtree(unpack)
            unpack.mkdir(parents=True)
            top = self._unpack_archive(bundle, unpack, commit)
            os.replace(unpack / top, partial)
        except zipfile.BadZipFile as error:
            raise ProvisioningError("não foi possível preparar o mecanismo local de geração") from error
        finally:
            shutil.rmtree(unpack, ignore_errors=True)
            bundle.unlink(missing_ok=True)
        write_json(marker, {"commit": commit, "url": url})

    @staticmethod
    def _unpack_archive(bundle: Path, destination: Path, commit: str) -> str:
        with zipfile.ZipFile(bundle) as archive:
            names = [info.filename for info in archive.infolist()]
            tops = {name.split("/", 1)[0] for name in names if name}
            if len(tops) != 1:
                raise ProvisioningError("o pacote do mecanismo local de geração é inválido")
            top = next(iter(tops))
            if not top.endswith(f"-{commit}"):
                raise ProvisioningError("o pacote do mecanismo local não corresponde à revisão esperada")
            root = destination.resolve()
            for info in archive.infolist():
                target = (destination / info.filename).resolve()
                if target != root and root not in target.parents:
                    raise ProvisioningError("o pacote do mecanismo local de geração é inválido")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
        return top

    def _ensure_backend(self, root: Path, backend: str, *, progress: Progress | None = None, cancel_event=None) -> None:
        config = self.manifest.get("torch_backends", {}).get(backend)
        if not config:
            raise ProvisioningError(f"backend PyTorch não declarado: {backend}")
        python = runtime_python(root)
        if not python.is_file():
            raise ProvisioningError("Python do runtime local não está disponível")
        self._emit(progress, "INSTALLING_COMPONENT", component=f"pytorch-{backend}")
        self._run([
            str(python), "-m", "pip", "install", "--upgrade",
            *config["packages"], "--index-url", config["index_url"],
        ], cancel_event=cancel_event)
        if backend == "nvidia":
            self._run([
                str(python), "-c",
                "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)",
            ], cancel_event=cancel_event)

    def ensure(
        self, target: str | Path, *, progress: Progress | None = None,
        backend: str | None = None, cancel_event=None,
    ) -> dict[str, Any]:
        root = Path(target).resolve()
        if cancel_event is not None and cancel_event.is_set():
            raise ProvisioningCancelled("instalação do Runtime cancelada")
        current = self.inspect(root)
        if current["installed"]:
            if backend and current.get("backend") != backend:
                self._ensure_backend(root, backend, progress=progress, cancel_event=cancel_event)
                marker = read_json(root / ".edx-runtime.json")
                marker["backend"] = backend
                marker["backend_updated_at"] = time.time()
                write_json(root / ".edx-runtime.json", marker)
            self._emit(progress, "RUNTIME_CACHED")
            return self.inspect(root)
        self._emit(progress, "PREPARING_RUNTIME")
        root.parent.mkdir(parents=True, exist_ok=True)
        partial = root.with_name(root.name + ".installing")
        if root.exists() and not (root / ".edx-runtime.json").is_file():
            raise ProvisioningError("a pasta do runtime local já existe e não é gerenciada pelo EDx")
        if isinstance(self.manifest.get("archive"), dict):
            # Source archive pinned to a full commit: no Git is needed on the user's PC.
            self._fetch_archive(partial, self.manifest["archive"], progress=progress, cancel_event=cancel_event)
        else:
            if not (partial / ".git").is_dir():
                if partial.exists():
                    shutil.rmtree(partial)
                self._run([
                    "git", "clone", "--filter=blob:none", "--no-checkout",
                    self.manifest["repository"], str(partial),
                ], cancel_event=cancel_event)
            else:
                self._run(["git", "fetch", "origin", "--prune"], cwd=partial, cancel_event=cancel_event)
            # A full reachable-history clone makes the trusted short commit from the
            # manifest resolvable without relying on a host accepting fetch-by-SHA.
            self._run(["git", "checkout", "--force", self.manifest["revision"]], cwd=partial, cancel_event=cancel_event)
        (partial / "models").mkdir(exist_ok=True)
        python = runtime_python(partial)
        if not python.is_file():
            self._run([self.python_executable, "-m", "venv", str(partial / ".venv")], cancel_event=cancel_event)
        if backend:
            self._ensure_backend(partial, backend, progress=progress, cancel_event=cancel_event)
        self._emit(progress, "INSTALLING_COMPONENT", component="comfyui-base")
        self._run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "-r", str(partial / self.manifest["requirements"]),
        ], cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise ProvisioningCancelled("instalação do Runtime cancelada")
        write_json(partial / ".edx-runtime.json", {
            "schema_version": 1,
            "repository": self.manifest["repository"],
            "revision": self.manifest["revision"],
            "backend": backend,
            "installed_at": time.time(),
        })
        if root.exists():
            # Repair never discards valid user data or previously downloaded
            # models.  Move them into the verified replacement atomically when
            # possible; capability provisioning validates each model afterward.
            for preserved in ("models", "user"):
                source = root / preserved
                destination = partial / preserved
                if source.exists():
                    if destination.exists():
                        shutil.rmtree(destination)
                    os.replace(source, destination)
            backup = root.with_name(f"{root.name}.replaced-{uuid4().hex[:8]}")
            os.replace(root, backup)
        os.replace(partial, root)
        self._emit(progress, "RUNTIME_READY")
        return self.inspect(root)

    def repair(
        self, target: str | Path, *, progress: Progress | None = None,
        backend: str | None = None, cancel_event=None,
    ) -> dict[str, Any]:
        """Conservative repair: keep a valid runtime and resumable checkout."""
        return self.ensure(target, progress=progress, backend=backend, cancel_event=cancel_event)
