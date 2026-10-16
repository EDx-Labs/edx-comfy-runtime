from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner


class ComponentResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.comfy = base / "ComfyUI"
        (self.comfy / "custom_nodes").mkdir(parents=True)
        self.source = base / "source"
        self.source.mkdir()
        (self.source / "__init__.py").write_text("ready\n", encoding="utf-8")
        (self.source / "data.txt").write_text("payload\n", encoding="utf-8")
        self.provisioner = Provisioner()
        self.provisioner.components = {"synthetic": {"name": "SyntheticNode", "source": "source"}}
        self.root_patch = mock.patch.object(provisioning, "ROOT", base)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_copy_failure_leaves_retryable_partial_install(self) -> None:
        target = self.comfy / "custom_nodes" / "SyntheticNode"
        state: dict = {}

        def interrupted_copy(source, destination, **kwargs):
            Path(destination).mkdir(parents=True, exist_ok=True)
            (Path(destination) / "__init__.py").write_text("partial\n", encoding="utf-8")
            raise OSError("synthetic disk-full interruption")

        with mock.patch.object(provisioning.shutil, "copytree", side_effect=interrupted_copy):
            with self.assertRaises(OSError):
                self.provisioner._component("synthetic", self.comfy, False, state)

        self.assertNotIn("synthetic", state.get("components", {}))
        self.assertFalse(target.exists())
        self.assertTrue(target.with_name(target.name + ".edx-tmp").is_dir())

        # A retry must treat this directory as an interrupted managed copy,
        # not as an external node that can never be repaired. The stale
        # staging directory is discarded before the retry.
        self.provisioner._component("synthetic", self.comfy, False, state)

        self.assertEqual((target / "__init__.py").read_text(encoding="utf-8"), "ready\n")
        self.assertEqual((target / "data.txt").read_text(encoding="utf-8"), "payload\n")
        self.assertIn("synthetic", state["components"])
        self.assertFalse(target.with_name(target.name + ".edx-tmp").exists())

    def test_hardlink_failure_falls_back_to_copy_in_isolated_target(self) -> None:
        source = self.source / "file.txt"
        source.write_text("content\n", encoding="utf-8")
        target = Path(self.tmp.name) / "target.txt"

        with mock.patch.object(provisioning.os, "link", side_effect=OSError("synthetic unsupported link")):
            provisioning.link_or_copy(str(source), str(target))

        self.assertEqual(target.read_text(encoding="utf-8"), "content\n")


if __name__ == "__main__":
    unittest.main()
