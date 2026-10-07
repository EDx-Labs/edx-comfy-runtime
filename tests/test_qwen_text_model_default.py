"""qwen-text (text-generation) usa o Qwen3-14B Q4_K_M; qwen-h3 (multimodal) permanece com o 27B + mmproj.

Sem rede, sem modelo real: só manifests, plano de instalação em diretório temporário e roteamento do node por AST.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from installer import text_task as client
from installer.provisioning import Provisioner

ROOT = Path(__file__).resolve().parents[1]
NODE_DIR = ROOT / "integrations" / "LLM" / "ComfyUI_Qwen_H3_Prompt"
spec = importlib.util.spec_from_file_location("qwen_model_default_text", NODE_DIR / "text_task.py")
text = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = text
spec.loader.exec_module(text)

H3_MODELS = ["qwen-h3-27b-q4", "qwen-h3-mmproj"]
TEXT_MODEL = "qwen3-14b-q4-k-m"


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Provisioner()

    def test_qwen_text_resolves_to_the_14b_only(self) -> None:
        profile = self.p.profiles["qwen-text"]
        self.assertEqual(profile["models"], [TEXT_MODEL])
        self.assertNotIn("requires", profile, "qwen-text não pode herdar o 27B/mmproj do qwen-h3")
        requirements = self.p.profile_requirements("qwen-text")
        self.assertEqual(requirements["models"], [TEXT_MODEL])
        self.assertEqual(requirements["components"], ["qwen-h3-runtime"])
        self.assertEqual(self.p.resolve_profile("text-task"), "qwen-text")
        self.assertEqual(self.p.resolve_profile("text-generation"), "qwen-text")

    def test_14b_registry_entry_is_the_official_verified_artifact(self) -> None:
        model = self.p.models[TEXT_MODEL]
        self.assertEqual((model["directory"], model["filename"]), ("LLM/Qwen3-14B", "Qwen3-14B-Q4_K_M.gguf"))
        self.assertEqual(model["size_bytes"], 9001752960)
        self.assertEqual(model["sha256"], "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0")
        self.assertRegex(model["url"], r"^https://huggingface\.co/Qwen/Qwen3-14B-GGUF/resolve/[0-9a-f]{40}/Qwen3-14B-Q4_K_M\.gguf$")

    def test_qwen_h3_is_unchanged(self) -> None:
        profile = self.p.profiles["qwen-h3"]
        self.assertEqual(profile["models"], H3_MODELS)
        self.assertEqual(profile["components"], ["qwen-h3-runtime"])
        self.assertEqual(profile["workflow_sets"], ["qwen-h3"])
        self.assertEqual(profile["hardware"], {"recommended_vram": 20})
        self.assertEqual(self.p.resolve_profile("prompt-generation"), "qwen-h3")
        self.assertEqual(self.p.profile_requirements("qwen-h3")["models"], H3_MODELS)
        self.assertEqual((self.p.models["qwen-h3-27b-q4"]["directory"], self.p.models["qwen-h3-27b-q4"]["filename"]), ("LLM/Qwen3.8", "Qwen3.8-27B-Q4_K_M.gguf"))
        self.assertEqual((self.p.models["qwen-h3-mmproj"]["directory"], self.p.models["qwen-h3-mmproj"]["filename"]), ("LLM/Qwen3.8", "mmproj-F16.gguf"))
        self.assertNotIn(TEXT_MODEL, self.p.profile_requirements("qwen-h3")["models"])

    def test_both_profiles_share_one_component_and_the_27b_is_not_removed(self) -> None:
        self.assertEqual(self.p.profiles["qwen-text"]["components"], self.p.profiles["qwen-h3"]["components"])
        for ident in H3_MODELS:
            self.assertIn(ident, self.p.models)

    def test_plans_download_only_their_own_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            self.assertEqual(self.p.plan("qwen-text", comfy).models_to_download, [TEXT_MODEL])
            self.assertEqual(self.p.plan("qwen-h3", comfy).models_to_download, H3_MODELS)

    def test_edx_video_full_still_pulls_qwen_h3_not_the_text_profile(self) -> None:
        self.assertEqual(self.p.profiles["edx-video-full"]["requires"][0], "qwen-h3")
        self.assertNotIn("qwen-text", self.p._resolved_profiles("edx-video-full"))


class NodeAndWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse((NODE_DIR / "node.py").read_text(encoding="utf-8"))
        cls.registry = Provisioner().models[TEXT_MODEL]

    def klass(self, name: str) -> ast.ClassDef:
        return next(n for n in self.tree.body if isinstance(n, ast.ClassDef) and n.name == name)

    def test_text_node_constants_match_the_registry(self) -> None:
        self.assertEqual(text.TEXT_MODEL_FILENAME, self.registry["filename"])
        self.assertEqual("LLM/" + text.TEXT_MODEL_SUBDIR, self.registry["directory"])

    def test_workflow_template_defaults_to_the_14b_without_a_vision_model(self) -> None:
        node = json.loads(client.WORKFLOW_PATH.read_text(encoding="utf-8"))["1"]["inputs"]
        self.assertEqual(node["llm_model"], text.TEXT_MODEL_FILENAME)
        self.assertNotIn("vision_model", node)

    def test_text_node_is_text_only_and_resolves_from_its_own_directory(self) -> None:
        source = ast.unparse(self.klass("QwenTextTask"))
        self.assertNotIn("vision_model", source)
        self.assertNotIn("DEFAULT_MMPROJ", source)
        self.assertIn("_resolve_model(llm_model, TEXT_MODEL_DIR)", source)
        acquire = next(n for n in ast.walk(self.klass("QwenTextTask")) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("acquire"))
        self.assertEqual(ast.unparse(acquire.args[1]), "None", "modelo só de texto: sem mmproj")

    def test_h3_node_keeps_the_27b_and_its_projector(self) -> None:
        source = ast.unparse(self.klass("QwenH3Prompt"))
        for expected in ("vision_model", "_ensure_model_available(llm_model)", "_ensure_model_available(vision_model)", "DEFAULT_MMPROJ"):
            self.assertIn(expected, source)
        self.assertNotIn("TEXT_MODEL", source)
        acquire = next(n for n in ast.walk(self.klass("QwenH3Prompt")) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("acquire"))
        self.assertEqual([ast.unparse(a) for a in acquire.args], ["model", "projector"])
        self.assertEqual(acquire.keywords, [])
        module_source = (NODE_DIR / "node.py").read_text(encoding="utf-8")
        self.assertRegex(module_source, r'MODEL_DIR = Path\(folder_paths\.models_dir\) / "LLM" / "Qwen3\.8"')
        self.assertRegex(module_source, r'DEFAULT_MODEL = "Qwen3\.8-27B-Q4_K_M\.gguf"')

    def test_result_label_comes_from_the_loaded_file_not_a_constant(self) -> None:
        self.assertFalse(hasattr(text, "MODEL_LABEL"))
        self.assertIn("model=model.stem", ast.unparse(self.klass("QwenTextTask")))


if __name__ == "__main__":
    unittest.main()
