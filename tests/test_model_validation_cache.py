from __future__ import annotations
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner

DATA = b"abcdef"


class ModelValidationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.comfy = Path(self.tmp.name) / "ComfyUI"
        self.path = self.comfy / "models" / "x" / "model.bin"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(DATA)
        self.model = {"directory": "x", "filename": "model.bin", "size_bytes": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest(), "url": "https://example.invalid/m"}
        self.p = Provisioner()
        self.calls = 0
        real = provisioning.sha256
        def counting(path):
            self.calls += 1; return real(path)
        patcher = mock.patch.object(provisioning, "sha256", counting); patcher.start(); self.addCleanup(patcher.stop)
    def tearDown(self) -> None: self.tmp.cleanup()

    def test_new_file_hashed_then_cached(self) -> None:
        self.assertTrue(self.p._valid_model(self.path, self.model)); self.assertEqual(self.calls, 1)
        self.assertTrue(self.p._valid_model(self.path, self.model)); self.assertEqual(self.calls, 1)

    def test_cache_survives_new_instance(self) -> None:
        self.p._valid_model(self.path, self.model)
        self.assertTrue(Provisioner()._valid_model(self.path, self.model)); self.assertEqual(self.calls, 1)

    def test_size_change_invalid(self) -> None:
        self.p._valid_model(self.path, self.model)
        self.path.write_bytes(DATA + b"x")
        self.assertFalse(self.p._valid_model(self.path, self.model)); self.assertEqual(self.calls, 1)

    def test_mtime_change_rehashes(self) -> None:
        self.p._valid_model(self.path, self.model)
        stat = self.path.stat(); import os; os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        self.assertTrue(self.p._valid_model(self.path, self.model)); self.assertEqual(self.calls, 2)

    def test_expected_hash_change_rehashes_and_wrong_hash_invalid(self) -> None:
        self.p._valid_model(self.path, self.model)
        wrong = {**self.model, "sha256": hashlib.sha256(b"zzzzzz").hexdigest()}
        self.assertFalse(self.p._valid_model(self.path, wrong)); self.assertEqual(self.calls, 2)

    def test_same_size_corruption_detected_with_new_mtime_and_on_force(self) -> None:
        self.p._valid_model(self.path, self.model)
        self.assertTrue(self.p._valid_model(self.path, self.model, force=True)); self.assertEqual(self.calls, 2)
        self.path.write_bytes(b"abcdeX")
        self.assertFalse(self.p._valid_model(self.path, self.model))
        self.assertFalse(self.p._valid_model(self.path, self.model))  # entrada descartada/ inválida

    def test_missing_file_invalid(self) -> None:
        self.path.unlink(); self.assertFalse(self.p._valid_model(self.path, self.model)); self.assertEqual(self.calls, 0)

    def test_install_download_records_validation_without_second_read(self) -> None:
        self.path.unlink()
        class Resp:
            status = 200
            def __init__(self): self.data = DATA
            def read(self, n):
                d, self.data = self.data, b""; return d
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self): pass
        p = Provisioner(opener=lambda *a, **k: Resp())
        self.assertTrue(p._download("m", self.path, self.model))
        self.assertEqual(self.calls, 1)  # só o hash do .partial
        self.assertTrue(p._valid_model(self.path, self.model)); self.assertEqual(self.calls, 1)

    def test_concurrent_queries_single_hash(self) -> None:
        real = provisioning.sha256
        def slow(path):
            time.sleep(0.2); return real(path)
        with mock.patch.object(provisioning, "sha256", slow):
            results = []
            threads = [threading.Thread(target=lambda: results.append(self.p._valid_model(self.path, self.model))) for _ in range(5)]
            [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(results, [True] * 5); self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
