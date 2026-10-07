"""Integridade dos pesos FL2VA/REF2VA do MiniMax H3 (Kijai/MiniMax-H3-experimental @ d8023be0). Sem rede."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from installer.provisioning import Provisioner

FL2VA = ("minimax-fl2va-w4a8", "minimax_h3_fl2va_pruned_w4a8_mixed.safetensors", 12540858008,
         "01aa7b92c007c599890461c325f9b7e3c96fb06c36f242f95b62f7f20e538dec")
REF2VA = ("minimax-ref2va-w4a8", "minimax_h3_ref2va_pruned_w4a8_mixed.safetensors", 11770657048,
          "de2c6c29c4ee702b45e48e40daae3834aeee58ab681c732d9152589a87c89910")


class MiniMaxModelIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Provisioner()

    def test_fl2va_has_expected_size_and_hash(self) -> None:
        ident, filename, size, sha = FL2VA
        m = self.p.models[ident]
        self.assertEqual((m["filename"], m["directory"], m["size_bytes"], m["sha256"]), (filename, "diffusion_models", size, sha))

    def test_ref2va_has_expected_size_and_hash(self) -> None:
        ident, filename, size, sha = REF2VA
        m = self.p.models[ident]
        self.assertEqual((m["filename"], m["directory"], m["size_bytes"], m["sha256"]), (filename, "diffusion_models", size, sha))

    def _model_with(self, data: bytes, **override):
        base = dict(self.p.models[FL2VA[0]])
        base.update(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        base.update(override)
        return base

    def _check(self, written: bytes, model_kwargs: dict, expected: bool) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ComfyUI" / "models" / "diffusion_models" / FL2VA[1]
            path.parent.mkdir(parents=True)
            path.write_bytes(written)
            model = self._model_with(b"good-data", **model_kwargs)
            self.assertIs(self.p._valid_model(path, model, force=True, record=False), expected)

    def test_truncated_non_empty_file_is_invalid(self) -> None:
        self._check(b"good", {}, False)

    def test_right_size_wrong_hash_is_invalid(self) -> None:
        self._check(b"evil-data", {}, False)

    def test_correct_file_is_valid(self) -> None:
        self._check(b"good-data", {}, True)

    def test_real_manifest_rejects_truncated_70mb_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / FL2VA[1]
            path.write_bytes(b"x" * 1024)
            self.assertFalse(self.p._valid_model(path, self.p.models[FL2VA[0]], force=True, record=False))


if __name__ == "__main__":
    unittest.main()
