"""krea2-reference READY == workflow + modelos + custom node executáveis (ComfyUI-Krea2-Reference @ bae20b3e). Sem rede."""
from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_uninstall import KEEP_SHARED, REMOVE, UninstallBase  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations" / "image" / "krea2"
WORKFLOW = ROOT / "workflows" / "image" / "krea2" / "character-reference.json"
PINNED = "bae20b3e0316e5b13fea94aa8a502c33fd2063ca"
CORE_NODES = {"UNETLoader", "CLIPLoader", "VAELoader", "LoadImage", "CLIPTextEncode", "EmptySD3LatentImage", "KSampler", "VAEDecode", "SaveImage"}


def custom_node_classes() -> set[str]:
    tree = ast.parse((SOURCE / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "NODE_CLASS_MAPPINGS" for t in node.targets):
            return {key.value for key in node.value.keys}
    return set()


class Krea2ReferenceContractTests(UninstallBase):
    def test_profile_resolves_the_runtime_component(self) -> None:
        self.assertEqual(self.p.profiles["krea2-reference"]["components"], ["krea2-reference-runtime"])
        self.assertIn("krea2-reference-runtime", self.p.profile_requirements("krea2-reference")["components"])
        self.assertEqual(self.p.components["krea2-reference-runtime"]["name"], "ComfyUI-Krea2-Reference")
        self.assertEqual(self.p.profiles["krea2"]["components"], [])

    def test_component_source_is_pinned_real_and_not_empty(self) -> None:
        self.assertTrue((SOURCE / "__init__.py").is_file(), "submódulo não inicializado: git submodule update --init")
        self.assertTrue(any(SOURCE.iterdir()))
        gitmodules = (ROOT / ".gitmodules").read_text(encoding="utf-8")
        self.assertIn("path = integrations/image/krea2", gitmodules)
        self.assertIn("url = https://github.com/KonokoAz/ComfyUI-Krea2-Reference.git", gitmodules)
        head = (SOURCE / ".git").exists() and __import__("subprocess").run(
            ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, PINNED)
        self.assertIn("MIT License", (SOURCE / "LICENSE").read_text(encoding="utf-8"))

    def test_every_workflow_class_type_is_core_or_provided_by_the_component(self) -> None:
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        used = {node["type"] for node in workflow["nodes"]}
        provided = custom_node_classes()
        self.assertTrue({"TextEncodeKrea2Reference", "Krea2ReferenceLoraLoader"} <= provided)
        self.assertEqual(used - CORE_NODES - provided, set())
        self.assertIn("TextEncodeKrea2Reference", used)

    def test_workflow_matches_the_upstream_node_contract(self) -> None:
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        node = next(n for n in workflow["nodes"] if n["type"] == "TextEncodeKrea2Reference")
        source = (SOURCE / "__init__.py").read_text(encoding="utf-8")
        widgets = re.findall(r'"(image[1-4]_detail|layout_split)":', source)
        self.assertEqual(widgets, ["image1_detail", "image2_detail", "image3_detail", "image4_detail", "layout_split"])
        values = node["widgets_values"]
        self.assertEqual(len(values), 1 + len(widgets))  # prompt + widgets (sem os antigos imageN_role)
        self.assertEqual(values[1:5], ["high", "low", "high", "high"])
        self.assertNotIn("content", values)

    def test_models_stay_shared_with_krea2(self) -> None:
        reference = self.p.profile_requirements("krea2-reference")["models"]
        base = self.p.profile_requirements("krea2")["models"]
        self.assertEqual([m for m in reference if m not in base], ["qwen3vl-4b-bf16"])
        self.assertTrue(set(base) <= set(reference))

    def test_ready_only_with_the_component_installed_and_repair_is_idempotent(self) -> None:
        self.install("krea2"); self.install("krea2-reference")
        self.assertTrue(self.installed("krea2-reference"))
        target = self.comfy / "custom_nodes" / "ComfyUI-Krea2-Reference"
        self.assertTrue((target / "__init__.py").is_file())
        import shutil
        shutil.rmtree(target)
        self.assertFalse(self.installed("krea2-reference"))
        self.assertIn("component:krea2-reference-runtime", self.p.plan("krea2-reference", self.comfy).pending_assets)
        self.p.install(self.p.plan("krea2-reference", self.comfy), skip_deps=True)
        self.assertTrue(self.installed("krea2-reference"))
        again = self.p.plan("krea2-reference", self.comfy)
        self.assertEqual((again.pending_assets, again.models_to_download), ([], []))

    def test_uninstall_reference_keeps_what_krea2_still_uses(self) -> None:
        self.install("krea2"); self.install("krea2-reference")
        plan = self.p.plan_uninstall("krea2-reference", self.comfy)
        actions = self.actions(plan)
        for shared in ("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae"):
            self.assertEqual(actions[shared], KEEP_SHARED)
        self.assertEqual(actions["qwen3vl-4b-bf16"], REMOVE)
        self.assertEqual(self.actions(plan, "component")["krea2-reference-runtime"], REMOVE)
        self.p.uninstall("krea2-reference", self.comfy, plan)
        self.assertTrue(self.installed("krea2"))


if __name__ == "__main__":
    unittest.main()
