from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner, ProvisioningCancelled, ProvisioningError

class FakeResponse:
    def __init__(self, data: bytes, status=200):
        self.data = data; self.status = status; self.offset = 0
    def read(self, size=-1):
        end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
        value = self.data[self.offset:end]; self.offset = end; return value
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


DATA = b"abcdef"
MODEL = {"url": "https://example.invalid/m", "filename": "model.bin", "directory": "x", "size_bytes": 6, "sha256": hashlib.sha256(DATA).hexdigest()}


class _Short(FakeResponse):
    """Entrega só parte do corpo e encerra a conexão."""


class DownloadRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); self.target = Path(self.tmp.name) / "model.bin"
        self.partial = self.target.with_suffix(".bin.partial"); self.sleeps: list[float] = []
    def tearDown(self) -> None: self.tmp.cleanup()
    def provisioner(self, opener) -> Provisioner: return Provisioner(opener=opener, sleeper=self.sleeps.append)

    def test_transient_failure_retries_with_backoff_and_reports_attempt(self) -> None:
        calls = []; events = []
        def opener(request, timeout):
            calls.append(1)
            if len(calls) < 3: raise urllib.error.HTTPError("u", 503, "busy", {}, None)
            return FakeResponse(DATA)
        self.provisioner(opener)._download("m", self.target, MODEL, progress=events.append)
        self.assertEqual(len(calls), 3); self.assertEqual(self.sleeps, [0.5, 1.0])
        self.assertEqual(self.target.read_bytes(), DATA)
        self.assertEqual(max(e.get("attempt", 0) for e in events), 3)

    def test_retries_are_finite(self) -> None:
        calls = []
        def opener(request, timeout): calls.append(1); raise TimeoutError()
        with self.assertRaises(ProvisioningError): self.provisioner(opener)._download("m", self.target, MODEL, retries=3)
        self.assertEqual(len(calls), 3)

    def test_permanent_http_error_is_not_retried(self) -> None:
        calls = []
        def opener(request, timeout): calls.append(1); raise urllib.error.HTTPError("u", 404, "nf", {}, None)
        with self.assertRaisesRegex(ProvisioningError, "sem possibilidade de retry"): self.provisioner(opener)._download("m", self.target, MODEL)
        self.assertEqual(len(calls), 1); self.assertEqual(self.sleeps, [])

    def test_cancel_during_backoff_stops_and_keeps_partial(self) -> None:
        cancel = threading.Event(); self.partial.write_bytes(b"abc")
        def opener(request, timeout): cancel.set(); raise ConnectionError("drop")
        with self.assertRaises(ProvisioningCancelled): Provisioner(opener=opener)._download("m", self.target, MODEL, cancel_event=cancel)
        self.assertEqual(self.partial.read_bytes(), b"abc"); self.assertFalse(self.target.exists())

    def test_truncated_response_keeps_partial_and_resumes_with_range(self) -> None:
        calls = []
        def opener(request, timeout):
            calls.append(dict(request.headers))
            return _Short(DATA[:3]) if len(calls) == 1 else FakeResponse(DATA[3:], 206)
        self.provisioner(opener)._download("m", self.target, MODEL)
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(calls[1].get("Range"), "bytes=3-")

    def test_content_range_mismatch_restarts_instead_of_corrupting(self) -> None:
        self.partial.write_bytes(b"abc"); calls = []
        class Wrong(FakeResponse):
            headers = {"Content-Range": "bytes 0-5/6"}
        def opener(request, timeout):
            calls.append(dict(request.headers))
            return Wrong(DATA, 206) if len(calls) == 1 else FakeResponse(DATA)
        self.provisioner(opener)._download("m", self.target, MODEL)
        self.assertEqual(self.target.read_bytes(), DATA); self.assertNotIn("Range", calls[1])

    def test_corrupt_complete_download_is_deleted_not_retried(self) -> None:
        calls = []
        def opener(request, timeout): calls.append(1); return FakeResponse(b"zzzzzz")
        with self.assertRaisesRegex(ProvisioningError, "inválido"): self.provisioner(opener)._download("m", self.target, MODEL)
        self.assertEqual(len(calls), 1); self.assertFalse(self.partial.exists())


class RetryVsRepairTests(unittest.TestCase):
    """Retry reaproveita o que é válido; repair (force_verify) revalida de verdade."""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); self.comfy = Path(self.tmp.name) / "ComfyUI"; (self.comfy / "models").mkdir(parents=True)
        self.p = Provisioner(opener=lambda r, timeout: FakeResponse(b"x" * 8), sleeper=lambda _: None)
        for ident in ("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae"):
            self.p.models[ident] = {**self.p.models[ident], "size_bytes": 8, "sha256": hashlib.sha256(b"x" * 8).hexdigest()}
    def tearDown(self) -> None: self.tmp.cleanup()

    def test_retry_after_failure_downloads_only_missing_asset_and_repair_rehashes(self) -> None:
        plan = self.p.plan("krea2", self.comfy)
        order = []; failed = []
        real = Provisioner._download
        def flaky(self_, ident, *a, **k):
            order.append(ident)
            if ident == plan.models[-1] and not failed:
                failed.append(1); raise ProvisioningError("boom")
            return real(self_, ident, *a, **k)
        with mock.patch.object(Provisioner, "_download", flaky):
            with self.assertRaises(ProvisioningError): self.p.install(plan, skip_deps=True)
            first = list(order); order.clear()
            self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True)  # retry
        self.assertEqual(first[-1], plan.models[-1]); self.assertEqual(order, [plan.models[-1]])
        self.assertTrue(self.p.profile_installation("krea2", self.comfy)["installed"])
        hashed = []; real_sha = provisioning.sha256
        with mock.patch.object(provisioning, "sha256", lambda path: hashed.append(path) or real_sha(path)):
            self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True)
            self.assertEqual(hashed, [])  # retry/status: cache
            self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True, force_verify=True)
            self.assertEqual(len(hashed), len(plan.models))  # repair: relê tudo


class KreaTests(unittest.TestCase):
    def test_distinct_capabilities_share_one_install_group(self) -> None:
        p = Provisioner()
        self.assertNotEqual(p.profiles_for_capability("krea2"), p.profiles_for_capability("krea2-reference"))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "models").mkdir()
            full = p.plan("krea2-full", Path(directory))
        self.assertEqual(len(full.models), len(set(full.models)))


if __name__ == "__main__":
    unittest.main()
