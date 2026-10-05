from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess

from installer.provisioning import Provisioner, ProvisioningCancelled, ProvisioningError
from installer.runtime import BaseRuntimeInstaller, runtime_python


class FakeResponse:
    def __init__(self, data: bytes, status=200):
        self.data = data; self.status = status; self.offset = 0
    def read(self, size=-1):
        if self.offset >= len(self.data): return b""
        end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
        value = self.data[self.offset:end]; self.offset = end; return value
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class RuntimeManifestTests(unittest.TestCase):
    def test_base_runtime_subprocess_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "runtime.json"
            manifest.write_text(json.dumps({"comfyui": {
                "repository": "https://example.invalid/ComfyUI.git",
                "revision": "fixed-revision", "entrypoint": "main.py",
                "requirements": "requirements.txt",
            }}), encoding="utf-8")
            installer = BaseRuntimeInstaller(manifest)
            cancel = threading.Event()
            timer = threading.Timer(0.1, cancel.set)
            started = time.monotonic(); timer.start()
            try:
                with self.assertRaises(ProvisioningCancelled):
                    installer._run(
                        [sys.executable, "-c", "import time; time.sleep(10)"],
                        cancel_event=cancel,
                    )
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 3)

    def test_studio_aliases_resolve_only_to_declared_profiles(self):
        provisioner = Provisioner()
        self.assertEqual(provisioner.resolve_profile("minimax"), "minimax-h3-fast")
        self.assertEqual(provisioner.resolve_profile("krea2"), "krea2")
        self.assertEqual(provisioner.resolve_profile("seedvr2"), "seedvr2")
        with self.assertRaisesRegex(ProvisioningError, "inexistente"):
            provisioner.resolve_profile("arbitrary-shell-command")

    def test_base_runtime_is_idempotent_and_uses_pinned_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "ComfyUI"
            manifest = root / "runtime.json"
            manifest.write_text(json.dumps({"comfyui": {
                "repository": "https://example.invalid/ComfyUI.git", "revision": "fixed-revision",
                "entrypoint": "main.py", "requirements": "requirements.txt",
                "torch_backends": {"nvidia": {
                    "packages": ["torch", "torchvision", "torchaudio"],
                    "index_url": "https://download.example/cu130",
                }},
            }}), encoding="utf-8")
            commands = []
            def runner(command, **kwargs):
                commands.append(command)
                if command[:2] == ["git", "clone"]:
                    partial = Path(command[-1]); (partial / ".git").mkdir(parents=True)
                    (partial / "main.py").write_text("", encoding="utf-8")
                    (partial / "requirements.txt").write_text("", encoding="utf-8")
                elif "venv" in command:
                    python = runtime_python(Path(command[-1]).parent)
                    python.parent.mkdir(parents=True, exist_ok=True); python.write_bytes(b"python")
                return CompletedProcess(command, 0, "", "")
            installer = BaseRuntimeInstaller(manifest, runner=runner, python_executable=root / "studio-python")
            self.assertFalse(installer.inspect(target)["installed"])
            installed = installer.ensure(target)
            self.assertTrue(installed["installed"])
            count = len(commands)
            self.assertTrue(installer.ensure(target)["installed"])
            self.assertEqual(len(commands), count)
            self.assertIn("fixed-revision", [item for command in commands for item in command])
            upgraded = installer.ensure(target, backend="nvidia")
            self.assertEqual(upgraded["backend"], "nvidia")
            self.assertTrue(any("https://download.example/cu130" in command for command in commands))
            backend_count = len(commands)
            self.assertEqual(installer.ensure(target, backend="nvidia")["backend"], "nvidia")
            self.assertEqual(len(commands), backend_count)

    def test_repair_preserves_models_and_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "ComfyUI"
            (target / "models" / "diffusion_models").mkdir(parents=True)
            (target / "models" / "diffusion_models" / "cached.bin").write_bytes(b"cached")
            (target / "user").mkdir()
            (target / "user" / "settings.json").write_text("{}", encoding="utf-8")
            (target / ".edx-runtime.json").write_text(json.dumps({"revision": "old"}), encoding="utf-8")
            manifest = root / "runtime.json"
            manifest.write_text(json.dumps({"comfyui": {
                "repository": "https://example.invalid/ComfyUI.git", "revision": "fixed-revision",
                "entrypoint": "main.py", "requirements": "requirements.txt",
            }}), encoding="utf-8")

            def runner(command, **kwargs):
                if command[:2] == ["git", "clone"]:
                    partial = Path(command[-1]); (partial / ".git").mkdir(parents=True)
                    (partial / "main.py").write_text("", encoding="utf-8")
                    (partial / "requirements.txt").write_text("", encoding="utf-8")
                elif "venv" in command:
                    python = runtime_python(Path(command[-1]).parent)
                    python.parent.mkdir(parents=True, exist_ok=True); python.write_bytes(b"python")
                return CompletedProcess(command, 0, "", "")

            installer = BaseRuntimeInstaller(manifest, runner=runner, python_executable=root / "python")
            self.assertTrue(installer.repair(target)["installed"])
            self.assertEqual((target / "models" / "diffusion_models" / "cached.bin").read_bytes(), b"cached")
            self.assertEqual((target / "user" / "settings.json").read_text(encoding="utf-8"), "{}")


class ResilientAssetTests(unittest.TestCase):
    def test_cancelled_download_keeps_partial_and_retry_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"
            cancel = threading.Event()
            first_chunk_written = threading.Event()

            class BlockingResponse(FakeResponse):
                def read(self, size=-1):
                    if self.offset == 0:
                        value = super().read(3)
                        first_chunk_written.set()
                        return value
                    cancel.wait(timeout=2)
                    return super().read(size)

            model = {
                "url": "https://example.invalid/model", "filename": "model.bin",
                "directory": "x", "size_bytes": 6,
                "sha256": hashlib.sha256(b"abcdef").hexdigest(),
            }
            provisioner = Provisioner(
                opener=lambda request, timeout: BlockingResponse(b"abcdef", 200),
                sleeper=lambda _delay: None,
            )
            errors = []
            thread = threading.Thread(
                target=lambda: self._capture_error(
                    errors, provisioner._download, "fixture", target, model,
                    cancel_event=cancel,
                ),
            )
            thread.start()
            self.assertTrue(first_chunk_written.wait(timeout=2))
            cancel.set(); thread.join(timeout=2)

            self.assertIsInstance(errors[0], ProvisioningCancelled)
            self.assertEqual(target.with_suffix(".bin.partial").read_bytes(), b"abc")
            self.assertFalse(target.exists())

            ranges = []
            provisioner.opener = lambda request, timeout: (
                ranges.append(request.headers.get("Range")) or FakeResponse(b"def", 206)
            )
            provisioner._download("fixture", target, model)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertEqual(ranges, ["bytes=3-"])

    @staticmethod
    def _capture_error(errors, function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except Exception as error:
            errors.append(error)

    def test_cancellation_never_removes_an_existing_valid_shared_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "shared.bin"
            target.write_bytes(b"shared")
            model = {
                "url": "https://example.invalid/shared", "filename": "shared.bin",
                "directory": "x", "size_bytes": 6,
                "sha256": hashlib.sha256(b"shared").hexdigest(),
            }
            cancel = threading.Event(); cancel.set()

            with self.assertRaises(ProvisioningCancelled):
                Provisioner()._download("shared", target, model, cancel_event=cancel)

            self.assertEqual(target.read_bytes(), b"shared")

    def test_partial_resumes_with_range_and_atomic_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"
            partial = target.with_suffix(".bin.partial"); partial.write_bytes(b"abc")
            requests = []
            def opener(request, timeout):
                requests.append(dict(request.headers))
                return FakeResponse(b"def", 206)
            provisioner = Provisioner(opener=opener, sleeper=lambda _delay: None)
            model = {"url": "https://example.invalid/model", "filename": "model.bin", "directory": "x", "size_bytes": 6, "sha256": hashlib.sha256(b"abcdef").hexdigest()}
            provisioner._download("fixture", target, model)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertFalse(partial.exists())
            self.assertEqual(requests[0].get("Range"), "bytes=3-")

    def test_server_without_range_restarts_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"; target.with_suffix(".bin.partial").write_bytes(b"old")
            calls = []
            def opener(request, timeout):
                calls.append(dict(request.headers))
                return FakeResponse(b"fresh", 200)
            model = {"url": "https://example.invalid/model", "filename": "model.bin", "directory": "x", "size_bytes": 5}
            Provisioner(opener=opener, sleeper=lambda _: None)._download("fixture", target, model)
            self.assertEqual(target.read_bytes(), b"fresh")
            self.assertEqual(len(calls), 2)
            self.assertIn("Range", calls[0]); self.assertNotIn("Range", calls[1])

    def test_retry_hash_and_size_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"; attempts = {"count": 0}
            def opener(request, timeout):
                attempts["count"] += 1
                if attempts["count"] == 1: raise OSError("temporary")
                return FakeResponse(b"wrong", 200)
            model = {"url": "https://example.invalid/model", "filename": "model.bin", "directory": "x", "size_bytes": 5, "sha256": hashlib.sha256(b"right").hexdigest()}
            with self.assertRaisesRegex(ProvisioningError, "inválido"):
                Provisioner(opener=opener, sleeper=lambda _: None)._download("fixture", target, model)
            self.assertEqual(attempts["count"], 2)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".bin.partial").exists())

    def test_concurrent_download_uses_one_valid_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"; calls = {"count": 0}
            lock = threading.Lock()
            def opener(request, timeout):
                with lock: calls["count"] += 1
                return FakeResponse(b"model", 200)
            model = {"url": "https://example.invalid/model", "filename": "model.bin", "directory": "x", "size_bytes": 5}
            provisioner = Provisioner(opener=opener, sleeper=lambda _: None)
            threads = [threading.Thread(target=provisioner._download, args=("fixture", target, model)) for _ in range(2)]
            [thread.start() for thread in threads]; [thread.join() for thread in threads]
            self.assertEqual(target.read_bytes(), b"model")
            self.assertEqual(calls["count"], 1)
            self.assertFalse(target.with_suffix(".bin.lock").exists())

    def test_dead_process_lock_is_recovered_without_losing_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"
            target.with_suffix(".bin.partial").write_bytes(b"abc")
            target.with_suffix(".bin.lock").write_text(
                json.dumps({"pid": 99999999, "created_at": time.time()}), encoding="utf-8",
            )
            model = {
                "url": "https://example.invalid/model", "filename": "model.bin",
                "directory": "x", "size_bytes": 6,
            }
            Provisioner(
                opener=lambda request, timeout: FakeResponse(b"def", 206),
                sleeper=lambda _delay: None,
            )._download("fixture", target, model)
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertFalse(target.with_suffix(".bin.lock").exists())


if __name__ == "__main__": unittest.main()
