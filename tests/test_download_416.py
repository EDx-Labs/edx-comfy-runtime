"""HTTP 416 num resume: recuperação determinística do .partial (sem rede, respostas simuladas)."""
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

DATA = b"abcdefgh"
SHA = hashlib.sha256(DATA).hexdigest()


class Resp:
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None):
        self.data, self.status, self.headers = data, status, headers or {}
    def read(self, size=-1): data, self.data = self.data, b""; return data
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


def err416(remote: int | None = None) -> urllib.error.HTTPError:
    headers = {"Content-Range": f"bytes */{remote}"} if remote is not None else {}
    return urllib.error.HTTPError("https://example.invalid/m", 416, "Range Not Satisfiable", headers, None)


class Download416Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); base = Path(self.tmp.name)
        self.target = base / "ComfyUI" / "models" / "x" / "model.bin"; self.target.parent.mkdir(parents=True)
        self.partial = self.target.with_suffix(".bin.partial")
        self.model = {"url": "https://example.invalid/m", "filename": "model.bin", "directory": "x", "size_bytes": len(DATA), "sha256": SHA}
        self.requests: list[dict] = []; self.events: list[dict] = []
    def tearDown(self) -> None: self.tmp.cleanup()

    def provisioner(self, handler) -> Provisioner:
        def opener(request, timeout):
            self.requests.append(dict(request.headers)); return handler(len(self.requests), request)
        return Provisioner(opener=opener, sleeper=lambda _: None)
    def ranges(self) -> list[str | None]: return [r.get("Range") for r in self.requests]
    def download(self, p: Provisioner, model=None, **kw) -> bool:
        return p._download("m", self.target, model or self.model, progress=self.events.append, **kw)

    def test_normal_partial_resumes_with_206(self) -> None:
        self.partial.write_bytes(DATA[:3])
        p = self.provisioner(lambda n, r: Resp(DATA[3:], 206, {"Content-Range": "bytes 3-7/8"}))
        self.assertTrue(self.download(p))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.ranges(), ["bytes=3-"])

    def test_server_ignoring_range_answers_200_and_is_not_appended(self) -> None:
        self.partial.write_bytes(DATA[:3])
        p = self.provisioner(lambda n, r: Resp(DATA, 200))
        self.assertTrue(self.download(p))
        self.assertEqual(self.target.read_bytes(), DATA)  # nunca DATA[:3] + DATA
        self.assertEqual(self.ranges(), ["bytes=3-", None])

    def test_remote_object_of_other_size_is_not_concatenated(self) -> None:
        self.partial.write_bytes(DATA[:3])
        p = self.provisioner(lambda n, r: Resp(b"zz", 206, {"Content-Range": "bytes 3-4/99"}) if n == 1 else Resp(DATA))
        self.assertTrue(self.download(p)); self.assertEqual(self.target.read_bytes(), DATA)

    def test_complete_partial_with_correct_sha_is_promoted_without_any_request(self) -> None:
        self.partial.write_bytes(DATA)
        p = self.provisioner(lambda n, r: self.fail("não deve haver requisição"))
        self.assertTrue(self.download(p))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertFalse(self.partial.exists()); self.assertEqual(self.requests, [])

    def test_416_with_complete_partial_and_sha_promotes_without_redownload(self) -> None:
        model = {k: v for k, v in self.model.items() if k != "size_bytes"}  # tamanho desconhecido: o servidor é quem responde 416
        self.partial.write_bytes(DATA)
        def handler(n, r): raise err416(len(DATA))
        self.assertTrue(self.download(self.provisioner(handler), model))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.ranges(), [f"bytes={len(DATA)}-"])

    def test_complete_size_but_wrong_sha_restarts_from_zero(self) -> None:
        self.partial.write_bytes(b"X" * len(DATA))
        p = self.provisioner(lambda n, r: Resp(DATA))
        self.assertTrue(self.download(p))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.ranges(), [None])

    def test_416_with_complete_size_but_wrong_sha_restarts_without_range(self) -> None:
        model = {k: v for k, v in self.model.items() if k != "size_bytes"}
        self.partial.write_bytes(b"X" * len(DATA))
        def handler(n, r):
            if n == 1: raise err416(len(DATA))
            return Resp(DATA)
        self.assertTrue(self.download(self.provisioner(handler), model))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.ranges(), [f"bytes={len(DATA)}-", None])

    def test_partial_larger_than_remote_restarts_from_zero(self) -> None:
        self.partial.write_bytes(DATA + b"EXTRA")  # maior que size_bytes: nem tenta Range
        p = self.provisioner(lambda n, r: Resp(DATA))
        self.assertTrue(self.download(p)); self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.ranges(), [None])

    def test_416_with_partial_larger_than_remote_and_unknown_size_falls_back_without_range(self) -> None:
        model = {k: v for k, v in self.model.items() if k != "size_bytes"}
        self.partial.write_bytes(DATA + b"EXTRA")
        def handler(n, r):
            if n == 1: raise err416(len(DATA))
            return Resp(DATA)
        self.assertTrue(self.download(self.provisioner(handler), model))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.ranges(), [f"bytes={len(DATA) + 5}-", None])

    def test_incompatible_partial_416_without_content_range_falls_back_without_range(self) -> None:
        self.partial.write_bytes(DATA[:5])
        def handler(n, r):
            if n == 1: raise err416()
            return Resp(DATA)
        self.assertTrue(self.download(self.provisioner(handler)))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.ranges(), ["bytes=5-", None])

    def test_fallback_failure_propagates_normally(self) -> None:
        self.partial.write_bytes(DATA[:5])
        def handler(n, r):
            if n == 1: raise err416()
            raise urllib.error.HTTPError("u", 404, "nf", {}, None)
        with self.assertRaisesRegex(ProvisioningError, "sem possibilidade de retry"): self.download(self.provisioner(handler))
        self.assertEqual(len(self.requests), 2); self.assertFalse(self.target.exists())

    def test_416_never_loops(self) -> None:
        self.partial.write_bytes(DATA[:5]); calls = []
        def handler(n, r): calls.append(1); raise err416()
        with self.assertRaises(ProvisioningError): self.download(self.provisioner(handler), retries=5)
        self.assertEqual(len(calls), 2)  # Range -> 416 -> sem Range -> 416 -> erro

    def test_416_without_range_is_still_a_definitive_error(self) -> None:
        def handler(n, r): raise err416()
        with self.assertRaises(ProvisioningError): self.download(self.provisioner(handler))
        self.assertEqual(len(self.requests), 1)

    def test_cancel_before_fallback_request(self) -> None:
        self.partial.write_bytes(DATA[:5]); cancel = threading.Event()
        def handler(n, r): cancel.set(); raise err416()
        with self.assertRaises(ProvisioningCancelled): self.download(self.provisioner(handler), cancel_event=cancel)
        self.assertEqual(len(self.requests), 1)

    def test_cancel_during_fallback_download(self) -> None:
        self.partial.write_bytes(DATA[:5]); cancel = threading.Event()
        def handler(n, r):
            if n == 1: raise err416()
            cancel.set(); return Resp(DATA)
        with self.assertRaises(ProvisioningCancelled): self.download(self.provisioner(handler), cancel_event=cancel)
        self.assertFalse(self.target.exists())

    def test_progress_is_reset_when_the_partial_is_abandoned(self) -> None:
        self.partial.write_bytes(DATA[:6])
        def handler(n, r):
            if n == 1: raise err416()
            return Resp(DATA)
        self.download(self.provisioner(handler))
        restart = next(i for i, e in enumerate(self.events) if "Reiniciando" in str(e.get("message", "")))
        self.assertEqual(self.events[restart]["bytes_downloaded"], 0); self.assertEqual(self.events[restart]["progress"], 0.0)
        after = [e["bytes_downloaded"] for e in self.events[restart:] if e.get("bytes_downloaded") is not None]
        self.assertTrue(all(b <= len(DATA) for b in after)); self.assertEqual(after[0], 0)

    def test_existing_valid_final_file_is_never_touched(self) -> None:
        self.target.write_bytes(DATA); self.partial.write_bytes(b"garbage")
        before = self.target.stat().st_mtime_ns
        p = self.provisioner(lambda n, r: self.fail("não deve baixar"))
        self.assertFalse(self.download(p))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(self.target.stat().st_mtime_ns, before)

    def test_sha_cache_and_ownership_work_after_recovery(self) -> None:
        self.partial.write_bytes(DATA)
        p = self.provisioner(lambda n, r: self.fail("sem rede"))
        hashed = []; real = provisioning.sha256
        with mock.patch.object(provisioning, "sha256", lambda path: hashed.append(path) or real(path)):
            self.download(p); self.assertEqual(len(hashed), 1)  # só o hash do .partial
            self.assertTrue(p._valid_model(self.target, self.model)); self.assertEqual(len(hashed), 1)  # cache: sem nova leitura
        state = provisioning.read_json(self.target.parents[2] / "user" / "default" / "EDx" / "provisioning-state.json")
        self.assertTrue(state["models"]["m"]["owned"])

    def test_krea2_turbo_regression_complete_partial_then_install_continues(self) -> None:
        """Caso real: .partial do krea2-turbo-fp8 completo (ou maior) e o servidor responde 416 ao Range."""
        comfy = Path(self.tmp.name) / "krea"; (comfy / "models").mkdir(parents=True)
        payloads = {}
        p = Provisioner(sleeper=lambda _: None)
        for ident in ("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae"):
            data = f"payload-{ident}".encode(); payloads[ident] = data
            p.models[ident].update({"url": f"https://example.invalid/{ident}", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        turbo = p._model_path(comfy, p.models["krea2-turbo-fp8"]); turbo.parent.mkdir(parents=True)
        turbo.with_suffix(turbo.suffix + ".partial").write_bytes(payloads["krea2-turbo-fp8"])  # completo, nunca promovido
        seen = []
        def opener(request, timeout):
            ident = request.full_url.rsplit("/", 1)[-1]; seen.append((ident, request.headers.get("Range")))
            if ident == "krea2-turbo-fp8": raise err416(len(payloads[ident]))
            return Resp(payloads[ident])
        p.opener = opener
        p.install(p.plan("krea2", comfy), skip_deps=True)
        self.assertEqual(turbo.read_bytes(), payloads["krea2-turbo-fp8"])
        self.assertNotIn("krea2-turbo-fp8", [i for i, _ in seen])  # promovido pelo SHA, sem requisição
        self.assertTrue(p.profile_installation("krea2", comfy)["installed"])


if __name__ == "__main__":
    unittest.main()
