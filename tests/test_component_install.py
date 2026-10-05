from __future__ import annotations
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner, ProvisioningError

REAL_ROOT = Path(__file__).resolve().parents[1]


class ComponentInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.comfy = base / "ComfyUI"; (self.comfy / "custom_nodes").mkdir(parents=True)
        self.src = base / "src"; self.src.mkdir()
        (self.src / "pyproject.toml").write_text("[project]\nname='x'\n")
        (self.src / "__init__.py").write_text("")
        self.p = Provisioner()
        self.p.components = {"c": {"name": "Node", "source": "src"}}
        self.patch = mock.patch.object(provisioning, "ROOT", base); self.patch.start()
    def tearDown(self) -> None:
        self.patch.stop(); self.tmp.cleanup()

    def run_component(self, state=None):
        with mock.patch.object(Provisioner, "_run_cancellable") as run:
            self.p._component("c", self.comfy, True, {} if state is None else state)
        return [c.args[0] for c in run.call_args_list]

    def test_requirements_method_never_runs_editable(self) -> None:
        self.p.components["c"]["install"] = "requirements"
        self.assertEqual(self.run_component(), [])
        shutil.rmtree(self.comfy / "custom_nodes" / "Node")  # hardlinked copy; start from a clean target
        (self.src / "requirements.txt").write_text("six\n")
        cmds = self.run_component()
        self.assertEqual(len(cmds), 1); self.assertIn("-r", cmds[0]); self.assertNotIn("-e", cmds[0])

    def test_minimax_runtime_never_installs_editable(self) -> None:
        real = json.loads((REAL_ROOT / "installer" / "components" / "local.json").read_text(encoding="utf-8"))["components"]["minimax-runtime"]
        self.assertEqual(real["name"], "comfyui-minimaxh3-easy-optimized")
        self.assertEqual(real["install"], "requirements")
        self.p.components["c"] = {"name": "Node", "source": "src", "install": real["install"]}
        # upstream has pyproject.toml but no requirements.txt -> no pip command at all
        self.assertEqual(self.run_component(), [])
        shutil.rmtree(self.comfy / "custom_nodes" / "Node")
        (self.src / "requirements.txt").write_text("requests\n")
        cmds = self.run_component()
        self.assertEqual(len(cmds), 1); self.assertIn("-r", cmds[0]); self.assertNotIn("-e", cmds[0])

    def test_legacy_default_and_explicit_editable_still_editable(self) -> None:
        for method in ("auto", "editable"):
            self.p.components["c"]["install"] = method
            cmds = self.run_component()
            self.assertIn("-e", cmds[0])

    def test_retry_after_failed_deps_reuses_copy_and_reruns_install(self) -> None:
        self.p.components["c"]["install"] = "requirements"
        (self.src / "requirements.txt").write_text("six\n")
        state: dict = {}
        with mock.patch.object(Provisioner, "_run_cancellable", side_effect=ProvisioningError("boom")):
            with self.assertRaises(ProvisioningError): self.p._component("c", self.comfy, True, state)
        self.assertNotIn("c", state.get("components", {}))
        with mock.patch.object(provisioning.shutil, "copytree") as copy:
            cmds = self.run_component(state)
        copy.assert_not_called(); self.assertEqual(len(cmds), 1)
        self.assertIn("c", state["components"])

    def test_subprocess_failure_preserves_stderr(self) -> None:
        code = "import sys; print('out-line'); sys.stderr.write('Multiple top-level packages discovered'); sys.exit(1)"
        with self.assertRaises(ProvisioningError) as ctx:
            Provisioner._run_cancellable([sys.executable, "-c", code, "https://user:pw@host/x"], label="ComfyUI_Qwen_H3_Prompt")
        msg = str(ctx.exception)
        self.assertIn("ComfyUI_Qwen_H3_Prompt setup failed (exit code 1)", msg)
        self.assertIn("Multiple top-level packages discovered", msg)
        self.assertNotIn("pw@", msg)

if __name__ == "__main__": unittest.main()
