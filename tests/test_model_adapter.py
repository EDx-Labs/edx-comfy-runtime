from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest

from installer.errors import ProvisioningError
from installer.model_adapter import ModelAdapter


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


class ModelAdapterTests(unittest.TestCase):
    def make_adapter(self, opener, owned: list[tuple[str, Path]]) -> ModelAdapter:
        return ModelAdapter(
            opener=opener,
            sleeper=lambda _delay: None,
            hash_file=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
            check_cancelled=lambda *_args: None,
            emit=lambda *_args, **_kwargs: None,
            thread_asset_lock=lambda *_args: nullcontext(),
            asset_lock=lambda *_args: nullcontext(),
            mark_owned=lambda ident, path: owned.append((ident, path)),
            error_type=ProvisioningError,
            cancelled_type=RuntimeError,
        )

    def test_verified_download_is_promoted_and_ownership_is_injected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ComfyUI" / "models" / "x" / "model.bin"
            payload = b"synthetic-model"
            model = {
                "url": "https://example.invalid/model",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            owned: list[tuple[str, Path]] = []
            adapter = self.make_adapter(lambda _request, timeout: Response(payload), owned)
            self.assertTrue(adapter.download("synthetic", target, model))
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(owned, [("synthetic", target)])
            self.assertFalse(target.with_suffix(".bin.partial").exists())

    def test_invalid_sha_is_rejected_without_claiming_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ComfyUI" / "models" / "x" / "model.bin"
            owned: list[tuple[str, Path]] = []
            adapter = self.make_adapter(
                lambda _request, timeout: Response(b"tampered"),
                owned,
            )
            model = {
                "url": "https://example.invalid/model",
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"expected").hexdigest(),
            }
            with self.assertRaisesRegex(ProvisioningError, "SHA256_MISMATCH"):
                adapter.download("synthetic", target, model)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".bin.partial").exists())
            self.assertEqual(owned, [])


if __name__ == "__main__":
    unittest.main()
