from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from installer.errors import ProvisioningError
from installer.model_adapter import ModelAdapter
from installer.provisioning import same_physical_tree
from installer.provisioning_state import ProvisioningStateStore, empty_state


class Response:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _size: int = -1) -> bytes:
        payload, self.payload = self.payload, b""
        return payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self) -> None:
        pass


class PlatformPathTests(unittest.TestCase):
    def make_adapter(self, payload: bytes) -> ModelAdapter:
        return ModelAdapter(
            opener=lambda _request, timeout: Response(payload),
            sleeper=lambda _delay: None,
            hash_file=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
            check_cancelled=lambda *_args: None,
            emit=lambda *_args, **_kwargs: None,
            thread_asset_lock=lambda *_args: nullcontext(),
            asset_lock=lambda *_args: nullcontext(),
            mark_owned=lambda *_args: None,
            error_type=ProvisioningError,
            cancelled_type=RuntimeError,
        )

    def test_model_download_supports_spaces_unicode_and_absolute_paths(self) -> None:
        payload = "modelo sintético – São Paulo\n".encode("utf-8")
        model = {
            "url": "https://example.invalid/model",
            "directory": "text encoders – português",
            "filename": "modelo final.bin",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        with tempfile.TemporaryDirectory(prefix="EDx Comfy UI – ") as directory:
            comfy = Path(directory).resolve() / "Comfy UI – Instância 01"
            target = ModelAdapter.model_path(comfy, model)

            self.assertTrue(self.make_adapter(payload).download("unicode", target, model))
            self.assertEqual(target.read_bytes(), payload)
            self.assertTrue(target.is_absolute())

    def test_model_destination_keeps_relative_path_semantics(self) -> None:
        model = {"directory": "models", "filename": "file.bin"}
        comfy = Path("relative Comfy UI")

        self.assertEqual(ModelAdapter.model_path(comfy, model), comfy / "models" / "models" / "file.bin")

    def test_state_store_supports_unicode_and_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="EDx Estado – ") as directory:
            path = Path(directory) / "Meu Comfy UI" / "estado – persistente.json"
            store = ProvisioningStateStore(path)
            state = {**empty_state(), "profiles": {"perfil-são": {"version": "1"}}}

            store.save(state)

            self.assertEqual(store.load(), state)

    def test_initial_model_directory_permission_failure_is_normalized(self) -> None:
        payload = b"model"
        model = {
            "url": "https://example.invalid/model",
            "directory": "models",
            "filename": "file.bin",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ComfyUI" / "models" / "file.bin"
            adapter = self.make_adapter(payload)

            with mock.patch.object(Path, "mkdir", side_effect=PermissionError("synthetic access denied")):
                with self.assertRaises(ProvisioningError):
                    adapter.download("permission", target, model)

            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".bin.partial").exists())

    @unittest.skipUnless(os.name == "nt", "Windows-only symlink behavior")
    def test_windows_symlink_tree_behavior_is_explicitly_platform_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            (source / "file.txt").write_text("x", encoding="utf-8")
            try:
                target.symlink_to(source, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink indisponível no host Windows")
            self.assertTrue(same_physical_tree(source, target))

    @unittest.skipUnless(os.name == "posix", "Linux/POSIX-only symlink behavior")
    def test_posix_symlink_tree_behavior_is_explicitly_platform_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            target = base / "target"
            source.mkdir()
            (source / "file.txt").write_text("x", encoding="utf-8")
            target.symlink_to(source, target_is_directory=True)
            self.assertTrue(same_physical_tree(source, target))


if __name__ == "__main__":
    unittest.main()
