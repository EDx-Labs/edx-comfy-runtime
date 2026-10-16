from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from installer.errors import ProvisioningError
from installer.provisioning import Provisioner
from installer.runtime_adapter import BackendRuntimeAdapter


class RuntimeAdapterTests(unittest.TestCase):
    def test_install_uses_injected_downloader_and_returns_record_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "src"
            source.mkdir()
            comfy = base / "ComfyUI"
            target = comfy / "custom_nodes" / "Node"
            target.mkdir(parents=True)
            (target / ".edx-component.json").write_text("{}")
            calls: list[str] = []

            class FakeModule:
                RELEASE_TAG = "fake-tag"
                RELEASE_COMMIT = "fake-commit"

                @staticmethod
                def runtime_status(root):
                    return {"ready": (Path(root) / "runtime" / "ready.txt").is_file()}

                @staticmethod
                def normalize_platform():
                    return "windows"

                @staticmethod
                def detect_hardware(_platform):
                    return SimpleNamespace(architecture="x64")

                @staticmethod
                def select_backend_candidates(*_args, **_kwargs):
                    return ["fake"]

                class InstallerContext:
                    def __init__(self, root, **_kwargs):
                        self.root = Path(root)
                        self.runtime_root = self.root / "runtime"

                @staticmethod
                def provision_runtime(context, *_args, downloader, **_kwargs):
                    asset = SimpleNamespace(name="fake.zip", url="https://invalid/fake", sha256="hash")
                    downloader(asset, context.runtime_root / ".downloads", False)
                    context.runtime_root.mkdir(parents=True, exist_ok=True)
                    (context.runtime_root / "ready.txt").write_text("ready")
                    return SimpleNamespace(backend="fake")

            def download(ident, target, _model, **_kwargs):
                calls.append(ident)
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_bytes(b"synthetic")

            adapter = BackendRuntimeAdapter(
                source,
                {"component": {"name": "Node", "source": "src", "backend_runtime": {"id": "fake", "installer": "install_runtime.py"}}},
                load_installer=lambda _path: FakeModule,
                download_asset=download,
                emit=Provisioner._emit,
                check_cancelled=Provisioner._check_cancelled,
                cancelled_errors=(),
                error_type=ProvisioningError,
            )

            result = adapter.install("component", comfy)
            self.assertEqual(calls, ["runtime:component:fake.zip"])
            self.assertEqual(result.backend_record["backend"], "fake")
            self.assertTrue((target / "runtime" / "ready.txt").is_file())


if __name__ == "__main__":
    unittest.main()
