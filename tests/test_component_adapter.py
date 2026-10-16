from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from installer.component_adapter import ComponentAdapter
from installer.provisioning import Provisioner, fingerprint, link_or_copy, same_physical_tree
from installer.errors import ProvisioningError


class ComponentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.source_root = base / "sources"
        self.source_root.mkdir()
        self.source = self.source_root / "node"
        self.source.mkdir()
        (self.source / "pyproject.toml").write_text("[project]\nname='synthetic-node'\n")
        (self.source / "__init__.py").write_text("")
        self.comfy = base / "ComfyUI"
        (self.comfy / "custom_nodes").mkdir(parents=True)
        self.components = {"node": {"name": "SyntheticNode", "source": "node"}}
        self.commands: list[list[str]] = []
        self.adapter = ComponentAdapter(
            self.source_root,
            self.components,
            python_executable="synthetic-python",
            run_command=lambda command, _cancel, _label: self.commands.append(command),
            fingerprint=fingerprint,
            link_or_copy=link_or_copy,
            same_physical_tree=same_physical_tree,
            check_cancelled=Provisioner._check_cancelled,
            emit=Provisioner._emit,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_install_returns_filesystem_result_without_persisting_state(self) -> None:
        result = self.adapter.install("node", self.comfy, install_deps=True)
        target = self.comfy / "custom_nodes" / "SyntheticNode"
        self.assertTrue(result.managed)
        self.assertEqual(result.target, target)
        self.assertTrue((target / ".edx-component.json").is_file())
        self.assertEqual(self.commands[0][-2:], ["-e", str(target)])

    def test_reexecution_is_idempotent_when_previous_fingerprint_matches(self) -> None:
        first = self.adapter.install("node", self.comfy, install_deps=True)
        self.commands.clear()
        second = self.adapter.install(
            "node",
            self.comfy,
            install_deps=True,
            previous_fingerprint=first.fingerprint,
        )
        self.assertEqual(second.fingerprint, first.fingerprint)
        self.assertEqual(self.commands, [])

    def test_external_component_is_preserved_and_not_reported_as_managed(self) -> None:
        target = self.comfy / "custom_nodes" / "SyntheticNode"
        target.mkdir()
        (target / "user_file.py").write_text("external")
        result = self.adapter.install("node", self.comfy, install_deps=True)
        self.assertFalse(result.managed)
        self.assertEqual((target / "user_file.py").read_text(), "external")
        self.assertEqual(self.commands, [])

    def test_dependency_failure_leaves_retryable_filesystem_without_state_contract(self) -> None:
        calls = 0

        def fail_once(command, cancel_event, label):
            nonlocal calls
            calls += 1
            raise ProvisioningError("synthetic dependency failure")

        self.adapter.run_command = fail_once
        with self.assertRaises(ProvisioningError):
            self.adapter.install("node", self.comfy, install_deps=True)
        self.assertTrue((self.comfy / "custom_nodes" / "SyntheticNode" / ".edx-component.json").is_file())
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
