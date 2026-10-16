from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from installer.model_adapter import ModelAdapter
from installer.provisioning import Provisioner


PAYLOAD = b"abcdef"
MODEL = {
    "url": "https://example.invalid/model",
    "filename": "model.bin",
    "directory": "synthetic",
    "size_bytes": len(PAYLOAD),
    "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
}


class Response:
    status = 200

    def __init__(self, payload: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    def read(self, _size: int = -1) -> bytes:
        payload, self.payload = self.payload, b""
        return payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self) -> None:
        pass


class TimeoutAfterPrefix(Response):
    def __init__(self, prefix: bytes) -> None:
        super().__init__(prefix)
        self.reads = 0

    def read(self, _size: int = -1) -> bytes:
        self.reads += 1
        if self.reads == 1:
            return super().read(_size)
        raise TimeoutError("synthetic stream timeout")


class ModelResilienceTests(unittest.TestCase):
    def test_stream_timeout_preserves_partial_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ComfyUI" / "models" / "synthetic" / "model.bin"
            calls: list[dict[str, str]] = []

            def opener(request, timeout):
                calls.append(dict(request.headers))
                if len(calls) == 1:
                    return TimeoutAfterPrefix(PAYLOAD[:3])
                return Response(PAYLOAD[3:], status=206, headers={"Content-Range": "bytes 3-5/6"})

            adapter = ModelAdapter(
                opener=opener,
                sleeper=lambda _delay: None,
                hash_file=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
                check_cancelled=lambda *_args: None,
                emit=lambda *_args, **_kwargs: None,
                thread_asset_lock=lambda *_args: nullcontext(),
                asset_lock=lambda *_args: nullcontext(),
                mark_owned=lambda *_args: None,
                error_type=RuntimeError,
                cancelled_type=RuntimeError,
            )

            self.assertTrue(adapter.download("synthetic", target, MODEL))
            self.assertEqual(target.read_bytes(), PAYLOAD)
            self.assertEqual(calls[1].get("Range"), "bytes=3-")
            self.assertFalse(target.with_suffix(".bin.partial").exists())

    def test_inconsistent_content_length_cannot_bypass_body_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"
            owned: list[tuple[str, Path]] = []
            adapter = ModelAdapter(
                opener=lambda _request, timeout: Response(
                    PAYLOAD,
                    headers={"Content-Length": "999"},
                ),
                sleeper=lambda _delay: None,
                hash_file=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
                check_cancelled=lambda *_args: None,
                emit=lambda *_args, **_kwargs: None,
                thread_asset_lock=lambda *_args: nullcontext(),
                asset_lock=lambda *_args: nullcontext(),
                mark_owned=lambda ident, path: owned.append((ident, path)),
                error_type=RuntimeError,
                cancelled_type=RuntimeError,
            )

            self.assertTrue(adapter.download("synthetic", target, MODEL))
            self.assertEqual(target.read_bytes(), PAYLOAD)
            self.assertEqual(owned, [("synthetic", target)])

    def test_promotion_failure_is_retried_without_second_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"
            calls = 0

            def opener(_request, timeout):
                nonlocal calls
                calls += 1
                return Response(PAYLOAD)

            provisioner = Provisioner(opener=opener, sleeper=lambda _delay: None)
            real_replace = Path.replace
            replace_calls = 0

            def flaky_replace(path: Path, destination: Path):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    raise OSError("synthetic antivirus lock")
                return real_replace(path, destination)

            with mock.patch.object(Path, "replace", new=flaky_replace):
                self.assertTrue(provisioner._download("synthetic", target, MODEL))

            self.assertEqual(calls, 1)
            self.assertGreaterEqual(replace_calls, 2)
            self.assertEqual(target.read_bytes(), PAYLOAD)
            self.assertFalse(target.with_suffix(".bin.partial").exists())

    def test_concurrent_requests_share_one_materialized_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            target = comfy / "models" / MODEL["directory"] / MODEL["filename"]
            first_opened = threading.Event()
            release_first = threading.Event()
            calls = 0
            calls_guard = threading.Lock()

            class BlockingResponse(Response):
                def read(self, size: int = -1) -> bytes:
                    first_opened.set()
                    self_release = release_first.wait(5)
                    if not self_release:
                        raise TimeoutError("synthetic test did not release response")
                    return super().read(size)

            def opener(_request, timeout):
                nonlocal calls
                with calls_guard:
                    calls += 1
                    number = calls
                return BlockingResponse(PAYLOAD) if number == 1 else Response(PAYLOAD)

            provisioner = Provisioner(opener=opener, sleeper=lambda _delay: None)
            adapter = provisioner._model_adapter()
            results: list[bool] = []
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    results.append(adapter.download("synthetic", target, MODEL))
                except BaseException as exc:  # surfaced by the assertion below
                    errors.append(exc)

            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            self.assertTrue(first_opened.wait(5))
            second.start()
            release_first.set()
            first.join(5)
            second.join(5)

            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(calls, 1)
            self.assertEqual(target.read_bytes(), PAYLOAD)


if __name__ == "__main__":
    unittest.main()
