"""SeedVR2: o perfil instala exatamente os modelos que o workflow de upscale do Studio carrega (nós nativos do ComfyUI)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

from installer.provisioning import Provisioner

ROOT = Path(__file__).resolve().parents[1]
# O workflow do Studio mora no repositório do backend; só é conferido quando o checkout irmão existe.
STUDIO_WORKFLOW = ROOT.parent / "comfyui-generate-video" / "workflows" / "Seedvr2_upscale_API.json"
# Verificado na tree API de huggingface.co/Comfy-Org/SeedVR2 (tamanho e oid LFS), sem baixar os pesos.
UPSTREAM = {
    "seedvr2-3b-int8-convrot": ("diffusion_models/seedvr2_3b_int8_convrot.safetensors", 3458259704,
                                "c3dec8bcc5916843a8a858572970597462e1f2dc598d6dfd818f6cd40f53a157"),
    "seedvr2-native-vae": ("vae/seedvr2_ema_vae_fp16.safetensors", 501324814,
                           "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1"),
}


class SeedVR2ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Provisioner()

    def test_registry_matches_verified_upstream_artifacts(self) -> None:
        for ident, (path, size, sha) in UPSTREAM.items():
            model = self.p.models[ident]
            self.assertEqual(f"{model['directory']}/{model['filename']}", path, ident)
            self.assertEqual(urlparse(model["url"]).path, f"/Comfy-Org/SeedVR2/resolve/main/{path}", ident)
            self.assertEqual(model["size_bytes"], size, ident)
            self.assertEqual(model["sha256"], sha, ident)

    def test_the_native_vae_is_the_same_file_as_the_vae_already_shipped_for_the_custom_node(self) -> None:
        legacy, native = self.p.models["seedvr2-vae"], self.p.models["seedvr2-native-vae"]
        self.assertEqual(legacy["sha256"], native["sha256"])  # mesmos bytes, só outro nome e pasta

    def test_the_profile_installs_the_native_models_and_keeps_the_node_and_examples(self) -> None:
        profile = self.p.profiles["seedvr2"]
        self.assertEqual(profile["models"], ["seedvr2-3b-int8-convrot", "seedvr2-native-vae"])
        self.assertEqual(profile["components"], ["seedvr2-runtime"])
        self.assertEqual(profile["workflow_sets"], ["seedvr2"])
        resolved = self.p.catalog.resolve_installation("seedvr2")
        self.assertEqual(list(resolved.models), ["seedvr2-3b-int8-convrot", "seedvr2-native-vae"])

    def test_the_old_custom_node_models_stay_in_the_registry_but_not_in_the_profile(self) -> None:
        self.assertIn("seedvr2-3b-fp8", self.p.models)
        self.assertIn("seedvr2-vae", self.p.models)
        self.assertNotIn("seedvr2-3b-fp8", self.p.profiles["seedvr2"]["models"])

    @unittest.skipUnless(STUDIO_WORKFLOW.is_file(), "workflow do Studio (repositório backend irmão) ausente")
    def test_the_studio_upscale_workflow_loads_exactly_what_the_profile_installs(self) -> None:
        graph = json.loads(STUDIO_WORKFLOW.read_text(encoding="utf-8"))
        wanted = {(node["class_type"], key): value for node in graph.values() for key, value in node.get("inputs", {}).items()
                  if node["class_type"] in {"UNETLoader", "VAELoader"} and key in {"unet_name", "vae_name"}}
        folders = {"unet_name": "diffusion_models", "vae_name": "vae"}
        for (_, key), filename in wanted.items():
            matches = [m for m in map(self.p.models.get, self.p.profiles["seedvr2"]["models"])
                       if m["filename"] == filename and m["directory"] == folders[key]]
            self.assertTrue(matches, f"{filename} ({folders[key]}) não é provisionado pelo perfil seedvr2")
        self.assertEqual(len(wanted), 2)


if __name__ == "__main__":
    unittest.main()
