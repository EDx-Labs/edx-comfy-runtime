"""The helper pack that lets Motion Context pass the H3 latent between ComfyUI jobs is part of the continuation install."""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

from installer.provisioning import Provisioner

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "integrations" / "video" / "minimax" / "edx-h3-latent-io" / "__init__.py"


class LatentIoComponentTests(unittest.TestCase):
    def test_the_continuation_capability_installs_the_helper_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            plan = Provisioner().plan("minimax-h3-easy-continuation", comfy)
        self.assertIn("minimax-h3-latent-io", plan.components)
        self.assertIn("minimax-h3-continuation", plan.components)

    def test_the_baseline_h3_profile_does_not_get_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            plan = Provisioner().plan("minimax-h3-base", comfy)
        self.assertNotIn("minimax-h3-latent-io", plan.components)

    def test_the_pack_registers_both_nodes(self) -> None:
        tree = ast.parse(PACK.read_text(encoding="utf-8"))
        mapping = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(getattr(target, "id", "") == "NODE_CLASS_MAPPINGS" for target in node.targets))
        self.assertEqual({key.value for key in mapping.keys}, {"EDxH3SaveAVLatent", "EDxH3LoadAVLatent"})

    def test_names_cannot_leave_the_latent_folder(self) -> None:
        source = PACK.read_text(encoding="utf-8")
        pattern = re.search(r'_NAME = re\.compile\(r"([^"]+)"\)', source).group(1)
        for good in ("op-0123456789abcdef0123-w0", "h3chain083644-w3", "a.b_c-1"):
            self.assertRegex(good, pattern)
        for bad in ("../x", "a/b", "a\b", ".hidden", "", "a" * 130, "x y"):
            self.assertNotRegex(bad, pattern)


if __name__ == "__main__":
    unittest.main()
