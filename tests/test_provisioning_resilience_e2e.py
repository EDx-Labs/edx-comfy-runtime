from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner, ProvisioningError


PAYLOAD = b"synthetic-model-payload"


class Response:
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


class InterruptedResponse(Response):
    def __init__(self, prefix: bytes) -> None:
        super().__init__(prefix)
        self.reads = 0

    def read(self, _size: int = -1) -> bytes:
        self.reads += 1
        if self.reads == 1:
            return super().read(_size)
        raise ProvisioningError("synthetic connection interruption")


class ProvisioningResilienceE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.comfy = self.base / "Comfy UI – E2E"
        (self.comfy / "models").mkdir(parents=True)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "__init__.py").write_text("synthetic\n", encoding="utf-8")
        self.workflow = self.base / "workflows" / "synthetic.json"
        self.workflow.parent.mkdir()
        self.workflow.write_text('{"synthetic": true}\n', encoding="utf-8")
        self.model = {
            "url": "https://example.invalid/synthetic-model",
            "directory": "checkpoints",
            "filename": "synthetic.bin",
            "size_bytes": len(PAYLOAD),
            "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        }
        self.provisioner = Provisioner(opener=lambda _request, timeout: Response(PAYLOAD), sleeper=lambda _delay: None)
        self.provisioner.components = {
            "synthetic-node": {"name": "SyntheticNode", "source": "source"},
        }
        self.provisioner.models = {"synthetic-model": self.model}
        self.provisioner.workflow_sets = {
            "synthetic-workflow": {"workflows": ["workflows/synthetic.json"]},
        }
        self.provisioner.profiles = {
            "synthetic-profile": {
                "components": ["synthetic-node"],
                "models": ["synthetic-model"],
                "workflow_sets": ["synthetic-workflow"],
            },
            "shared-profile": {
                "components": ["synthetic-node"],
                "models": ["synthetic-model"],
                "workflow_sets": [],
            },
        }
        self.root_patch = mock.patch.object(provisioning, "ROOT", self.base)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_successful_install_and_reexecution_are_idempotent(self) -> None:
        calls: list[str] = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return Response(PAYLOAD)

        self.provisioner.opener = opener
        first = self.provisioner.plan("synthetic-profile", self.comfy)
        self.provisioner.install(first, skip_deps=True, skip_runtime=True)
        second = self.provisioner.plan("synthetic-profile", self.comfy)
        self.provisioner.install(second, skip_deps=True, skip_runtime=True)

        state_path = self.comfy / "user" / "default" / "EDx" / "provisioning-state.json"
        self.assertEqual(calls, [self.model["url"]])
        self.assertEqual(second.to_install, [])
        self.assertEqual(second.models_to_download, [])
        self.assertIn("synthetic-profile", state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            (self.comfy / "custom_nodes" / "SyntheticNode" / "__init__.py").read_text(encoding="utf-8"),
            "synthetic\n",
        )
        self.assertEqual(
            (self.comfy / "user" / "default" / "workflows" / "EDx" / "synthetic-workflow" / "synthetic.json").read_text(encoding="utf-8"),
            '{"synthetic": true}\n',
        )

    def test_interrupted_model_download_resumes_on_next_install(self) -> None:
        calls: list[dict[str, str]] = []

        def opener(request, timeout):
            calls.append(dict(request.headers))
            if len(calls) == 1:
                return InterruptedResponse(PAYLOAD[:7])
            return Response(PAYLOAD[7:], status=206, headers={"Content-Range": f"bytes 7-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"})

        self.provisioner.opener = opener
        plan = self.provisioner.plan("synthetic-profile", self.comfy)
        with self.assertRaises(ProvisioningError):
            self.provisioner.install(plan, skip_deps=True, skip_runtime=True)

        target = self.comfy / "models" / "checkpoints" / "synthetic.bin"
        self.assertEqual(target.with_suffix(".bin.partial").read_bytes(), PAYLOAD[:7])
        self.assertFalse(target.exists())
        self.assertNotIn("synthetic-profile", self.provisioner._state_store(self.comfy).load().get("profiles", {}))

        self.provisioner.install(self.provisioner.plan("synthetic-profile", self.comfy), skip_deps=True, skip_runtime=True)

        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertEqual(calls[1].get("Range"), "bytes=7-")
        self.assertEqual(len(calls), 2)

    def test_component_failure_does_not_declare_model_or_profile(self) -> None:
        requirements = self.source / "requirements.txt"
        requirements.write_text("synthetic-package\n", encoding="utf-8")
        self.provisioner.components["synthetic-node"]["install"] = "requirements"
        calls: list[str] = []
        self.provisioner.opener = lambda request, timeout: calls.append(request.full_url) or Response(PAYLOAD)
        plan = self.provisioner.plan("synthetic-profile", self.comfy)

        with mock.patch.object(Provisioner, "_run_cancellable", side_effect=ProvisioningError("synthetic component failure")):
            with self.assertRaisesRegex(ProvisioningError, "synthetic component failure"):
                self.provisioner.install(plan, skip_runtime=True)

        state = self.provisioner._state_store(self.comfy).load()
        target = self.comfy / "models" / "checkpoints" / "synthetic.bin"
        self.assertEqual(calls, [])
        self.assertFalse(target.exists())
        self.assertNotIn("synthetic-profile", state.get("profiles", {}))
        self.assertNotIn("synthetic-model", state.get("models", {}))

        with mock.patch.object(Provisioner, "_run_cancellable"):
            self.provisioner.install(self.provisioner.plan("synthetic-profile", self.comfy), skip_runtime=True)
        self.assertTrue(target.is_file())
        self.assertIn("synthetic-profile", self.provisioner._state_store(self.comfy).load()["profiles"])

    def test_shared_resources_are_reused_by_second_profile(self) -> None:
        calls: list[str] = []
        self.provisioner.opener = lambda request, timeout: calls.append(request.full_url) or Response(PAYLOAD)

        self.provisioner.install(self.provisioner.plan("synthetic-profile", self.comfy), skip_deps=True, skip_runtime=True)
        second = self.provisioner.plan("shared-profile", self.comfy)
        self.provisioner.install(second, skip_deps=True, skip_runtime=True)

        self.assertEqual(calls, [self.model["url"]])
        self.assertEqual(second.to_install, [])
        self.assertEqual(second.models_to_download, [])
        state = self.provisioner._state_store(self.comfy).load()
        self.assertEqual(set(state["profiles"]), {"synthetic-profile", "shared-profile"})


if __name__ == "__main__":
    unittest.main()
