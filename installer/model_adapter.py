"""Validação e materialização segura de modelos declarados."""

from __future__ import annotations

import http.client
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Mapping

from .storage import read_json, write_json


_VALIDATION_WRITE_LOCK = threading.Lock()
_HASH_LOCKS: dict[str, threading.Lock] = {}
_HASH_LOCKS_GUARD = threading.Lock()
CHUNK = 8 * 1024 * 1024


class _TruncatedDownload(OSError):
    """Resposta terminou antes do tamanho declarado; o partial é retomável."""


HashFile = Callable[[Path], str]
CancellationCheck = Callable[..., None]
ProgressEmitter = Callable[..., None]
LockFactory = Callable[[Path, Any], AbstractContextManager[None]]


class ModelAdapter:
    """Executa operações de modelo sem conhecer o orquestrador ou seu plano."""

    def __init__(
        self,
        *,
        opener,
        sleeper: Callable[[float], None],
        hash_file: HashFile,
        check_cancelled: CancellationCheck,
        emit: ProgressEmitter,
        thread_asset_lock: LockFactory,
        asset_lock: LockFactory,
        mark_owned: Callable[[str, Path], None],
        error_type: type[BaseException],
        cancelled_type: type[BaseException],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.opener = opener
        self.sleeper = sleeper
        self.hash_file = hash_file
        self.check_cancelled = check_cancelled
        self.emit = emit
        self.thread_asset_lock = thread_asset_lock
        self.asset_lock = asset_lock
        self.mark_owned = mark_owned
        self.error_type = error_type
        self.cancelled_type = cancelled_type
        self.clock = clock

    @staticmethod
    def model_path(comfy: Path, model: Mapping[str, Any]) -> Path:
        return Path(comfy) / "models" / model["directory"] / model["filename"]

    @staticmethod
    def validation_cache_path(path: Path) -> Path | None:
        # <comfy>/models/... -> <comfy>/user/default/EDx/model-validation.json
        for parent in path.parents:
            if parent.name == "models":
                return parent.parent / "user" / "default" / "EDx" / "model-validation.json"
        return None

    @staticmethod
    def file_stamp(path: Path) -> dict[str, int]:
        info = path.stat()
        return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}

    @staticmethod
    def _hash_lock(path: Path) -> threading.Lock:
        with _HASH_LOCKS_GUARD:
            return _HASH_LOCKS.setdefault(str(path), threading.Lock())

    def cached_valid(self, path: Path, model: Mapping[str, Any]) -> bool:
        cache = self.validation_cache_path(path)
        if cache is None:
            return False
        entry = read_json(cache, {}).get("models", {}).get(str(path))
        return bool(entry) and entry.get("sha256") == model["sha256"] and {
            key: entry.get(key) for key in ("size", "mtime_ns")
        } == self.file_stamp(path)

    def remember_model(self, path: Path, model: Mapping[str, Any]) -> None:
        """Persiste uma validação SHA-256 keyed por caminho, tamanho e mtime."""
        cache = self.validation_cache_path(path)
        if cache is None or not model.get("sha256") or not path.is_file():
            return
        with _VALIDATION_WRITE_LOCK:
            data = read_json(cache, {"schema_version": 1, "models": {}})
            data.setdefault("models", {})
            data["models"][str(path)] = {
                "sha256": model["sha256"],
                "size_bytes": model.get("size_bytes"),
                **self.file_stamp(path),
                "validated_at": self.clock(),
            }
            write_json(cache, data)

    def forget_model(self, path: Path) -> None:
        cache = self.validation_cache_path(path)
        if cache is None:
            return
        with _VALIDATION_WRITE_LOCK:
            data = read_json(cache, {})
            if data.get("models", {}).pop(str(path), None) is not None:
                write_json(cache, data)

    def valid_model(
        self,
        path: Path,
        model: Mapping[str, Any],
        *,
        force: bool = False,
        record: bool = True,
    ) -> bool:
        """Valida modelo por tipo, tamanho e SHA-256, usando cache single-flight."""
        if model.get("install_mode") == "node-managed":
            return path.is_dir() and any(path.iterdir())
        if not path.is_file() or path.stat().st_size == 0:
            return False
        if model.get("size_bytes") and path.stat().st_size != int(model["size_bytes"]):
            return False
        if not model.get("sha256"):
            return True
        if not force and self.cached_valid(path, model):
            return True
        with self._hash_lock(path):
            if not force and self.cached_valid(path, model):
                return True
            if self.hash_file(path) != model["sha256"]:
                self.forget_model(path)
                return False
            if record:
                self.remember_model(path, model)
            return True

    @staticmethod
    def remote_size_from_416(exc: urllib.error.HTTPError) -> int | None:
        match = re.match(
            r"bytes \*/(\d+)",
            str((getattr(exc, "headers", None) or {}).get("Content-Range") or ""),
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def resumes_at(response: Any, offset: int, expected: int | None = None) -> bool:
        if getattr(response, "status", None) != 206:
            return False
        content_range = getattr(response, "headers", None) and response.headers.get("Content-Range")
        if not content_range:
            return True
        match = re.match(r"bytes (\d+)-\d*/(\d+|\*)", content_range)
        if not match or int(match.group(1)) != offset:
            return False
        return not (expected and match.group(2) != "*" and int(match.group(2)) != expected)

    @staticmethod
    def is_transient(exc: BaseException) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in frozenset({408, 425, 429, 500, 502, 503, 504})
        return isinstance(
            exc,
            (urllib.error.URLError, OSError, TimeoutError, ConnectionError, http.client.HTTPException, _TruncatedDownload),
        )

    def backoff(self, delay: float, cancel_event=None) -> None:
        if cancel_event is None:
            self.sleeper(delay)
        elif cancel_event.wait(delay):
            raise self.cancelled_type("download cancelado durante a espera de retry; partial preservado")

    def finalize_partial(
        self,
        ident: str,
        partial: Path,
        target: Path,
        model: Mapping[str, Any],
        current: int,
        expected: int | None,
        progress,
        *,
        strict: bool,
    ) -> bool:
        self.emit(
            progress,
            "VERIFYING_ASSET",
            asset=ident,
            bytes_downloaded=current,
            bytes_total=expected,
            progress=(min(1.0, current / expected) if expected else None),
        )
        received = partial.stat().st_size if partial.is_file() else 0
        if not self.valid_model(partial, model, force=True, record=False):
            partial.unlink(missing_ok=True)
            if strict:
                size = int(model.get("size_bytes", 0) or 0)
                if size and received != size:
                    raise self.error_type(
                        f"download inválido para {ident} [MODEL_DOWNLOAD_SIZE_MISMATCH]: "
                        f"recebidos {received} bytes, o registry espera {size}"
                    )
                if model.get("sha256"):
                    raise self.error_type(
                        f"download inválido para {ident} [MODEL_DOWNLOAD_SHA256_MISMATCH]: "
                        f"{received} bytes recebidos, SHA-256 diferente do esperado ({model['sha256'][:12]}…)"
                    )
                raise self.error_type(
                    f"download inválido para {ident} [MODEL_DOWNLOAD_INVALID]: arquivo vazio ou ilegível ({received} bytes)"
                )
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.replace(target)
        self.remember_model(target, model)
        self.mark_owned(ident, target)
        self.emit(
            progress,
            "ASSET_READY",
            asset=ident,
            bytes_downloaded=current,
            bytes_total=expected,
            progress=(1.0 if expected else None),
        )
        return True

    def download(
        self,
        ident: str,
        target: Path,
        model: Mapping[str, Any],
        *,
        progress=None,
        cancel_event=None,
        retries: int = 3,
    ) -> bool:
        attempts = max(1, retries)
        plain = {"User-Agent": "EDx-Provisioner/2.0"}
        with self.thread_asset_lock(target, cancel_event), self.asset_lock(target, cancel_event):
            self.check_cancelled(cancel_event, f"download cancelado antes de {ident}")
            if self.valid_model(target, model):
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".partial")
            expected = int(model.get("size_bytes", 0)) or None
            reset_used = False
            restart_message = "Reiniciando download após detectar arquivo parcial incompatível"
            for attempt in range(attempts):
                self.check_cancelled(cancel_event, f"download cancelado antes de {ident}")
                current = partial.stat().st_size if partial.exists() else 0
                info = {"attempt": attempt + 1, "max_attempts": attempts}
                if expected and current > expected:
                    partial.unlink(missing_ok=True)
                    current = 0
                    reset_used = True
                    self.emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=0, bytes_total=expected, progress=0.0, message=restart_message, **info)
                elif expected and current == expected:
                    if model.get("sha256") and self.finalize_partial(ident, partial, target, model, current, expected, progress, strict=False):
                        return True
                    partial.unlink(missing_ok=True)
                    current = 0
                    reset_used = True
                    self.emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=0, bytes_total=expected, progress=0.0, message=restart_message, **info)
                headers = dict(plain)
                if current:
                    headers["Range"] = f"bytes={current}-"
                self.emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=current, bytes_total=expected, progress=(current / expected if expected else None), **info)
                try:
                    try:
                        response = self.opener(urllib.request.Request(model["url"], headers=headers), timeout=120)
                    except urllib.error.HTTPError as exc:
                        if exc.code != 416 or not current or reset_used:
                            raise
                        reset_used = True
                        remote = self.remote_size_from_416(exc)
                        exc.close()
                        if remote is not None and remote == current and model.get("sha256") and self.finalize_partial(ident, partial, target, model, current, expected, progress, strict=False):
                            return True
                        partial.unlink(missing_ok=True)
                        current = 0
                        self.check_cancelled(cancel_event, f"download cancelado antes de {ident}")
                        self.emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=0, bytes_total=expected, progress=0.0, message=restart_message, **info)
                        response = self.opener(urllib.request.Request(model["url"], headers=plain), timeout=120)
                    if current and not self.resumes_at(response, current, expected):
                        partial.unlink(missing_ok=True)
                        current = 0
                        reset_used = True
                        response.close()
                        self.emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=0, bytes_total=expected, progress=0.0, message=restart_message, **info)
                        response = self.opener(urllib.request.Request(model["url"], headers=plain), timeout=120)
                    content_type = str((getattr(response, "headers", None) or {}).get("Content-Type") or "").split(";")[0].strip().lower()
                    if content_type in ("text/html", "application/json", "application/xhtml+xml"):
                        response.close()
                        raise self.error_type(
                            f"download inválido para {ident} [MODEL_DOWNLOAD_INVALID_RESPONSE]: "
                            f"o servidor devolveu {content_type} em vez do arquivo do modelo"
                        )
                    with response, partial.open("ab" if current else "wb") as stream:
                        while block := response.read(CHUNK):
                            if cancel_event is not None and cancel_event.is_set():
                                raise self.cancelled_type(f"download cancelado em {ident}; partial preservado")
                            stream.write(block)
                            current += len(block)
                            self.emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=current, bytes_total=expected, progress=(min(1.0, current / expected) if expected else None), **info)
                    if expected and current < expected:
                        raise _TruncatedDownload(f"conexão encerrada em {current}/{expected} bytes")
                    self.finalize_partial(ident, partial, target, model, current, expected, progress, strict=True)
                    return True
                except (self.error_type, self.cancelled_type):
                    raise
                except Exception as exc:
                    if not self.is_transient(exc):
                        raise self.error_type(f"download de {ident} falhou sem possibilidade de retry: {exc}") from exc
                    if attempt + 1 >= attempts:
                        raise self.error_type(f"download interrompido em {ident}; partial preservado") from exc
                    delay = min(8.0, 0.5 * (2 ** attempt))
                    self.emit(progress, "DOWNLOADING_MODEL", asset=ident, bytes_downloaded=current, bytes_total=expected, progress=(current / expected if expected else None), retry_in=delay, message=f"Falha transitória; nova tentativa em {delay:g}s", **info)
                    self.backoff(delay, cancel_event)
