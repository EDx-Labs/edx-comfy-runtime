from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path

from installer.provisioning import Provisioner, ProvisioningError, fingerprint, write_json


class ProvisionerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.comfy = Path(self.tmp.name) / "ComfyUI"
        (self.comfy / "models").mkdir(parents=True)
        self.provisioner = Provisioner()

    def tearDown(self) -> None: self.tmp.cleanup()

    def test_simple_resolution_and_deduplication(self) -> None:
        plan = self.provisioner.plan("minimax-h3-fast", self.comfy)
        self.assertEqual(plan.profiles, ["minimax-h3-fast", "minimax-h3-base"])
        self.assertEqual(plan.components.count("minimax-runtime"), 1)
        self.assertEqual(plan.workflow_sets, ["minimax-h3-core"])

    def test_recursive_meta_profile_reuses_shared_resources(self) -> None:
        plan = self.provisioner.plan("edx-video-full", self.comfy)
        self.assertIn("qwen-h3", plan.profiles)
        self.assertIn("minimax-h3-base", plan.profiles)
        self.assertEqual(len(plan.models), len(set(plan.models)))
        self.assertEqual(plan.components.count("minimax-runtime"), 1)

    def test_fast_then_high_has_same_base_resources(self) -> None:
        fast = self.provisioner.plan("minimax-h3-fast", self.comfy)
        high = self.provisioner.plan("minimax-h3-high", self.comfy)
        self.assertEqual(fast.components, high.components)
        self.assertEqual(fast.models, high.models)
        self.assertEqual(fast.workflow_sets, high.workflow_sets)

    def test_high_reuses_fast_filesystem_resources(self) -> None:
        fast = self.provisioner.plan("minimax-h3-fast", self.comfy)
        for component_id in fast.components:
            component = self.provisioner.components[component_id]
            marker = self.comfy / "custom_nodes" / component["name"] / ".edx-component.json"
            write_json(marker, {"fingerprint": fingerprint(Path.cwd() / component["source"])})
        for model_id in fast.models:
            model = self.provisioner.models[model_id]
            target = self.comfy / "models" / model["directory"] / model["filename"]
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b"present")
        for set_id in fast.workflow_sets:
            for rel in self.provisioner.workflow_sets[set_id]["workflows"]:
                target = self.comfy / "user" / "default" / "workflows" / "EDx" / set_id / Path(rel).name
                target.parent.mkdir(parents=True, exist_ok=True); target.write_text("{}", encoding="utf-8")
        high = self.provisioner.plan("minimax-h3-high", self.comfy)
        self.assertFalse(high.models_to_download)
        self.assertFalse(high.to_install)

    def test_missing_profile_and_cycle_are_reported(self) -> None:
        with self.assertRaisesRegex(ProvisioningError, "inexistente"):
            self.provisioner.plan("not-real", self.comfy)
        self.provisioner.profiles["cycle-a"] = {"requires": ["cycle-b"]}
        self.provisioner.profiles["cycle-b"] = {"requires": ["cycle-a"]}
        with self.assertRaisesRegex(ProvisioningError, "ciclo"):
            self.provisioner.plan("cycle-a", self.comfy)

    def test_existing_model_is_detected(self) -> None:
        model = self.provisioner.models["qwen-h3-mmproj"]
        path = self.comfy / "models" / model["directory"] / model["filename"]
        path.parent.mkdir(parents=True); path.write_bytes(b"model")
        plan = self.provisioner.plan("qwen-h3", self.comfy)
        self.assertIn("model:qwen-h3-mmproj", plan.already_installed)

    def test_dry_run_does_not_create_state(self) -> None:
        plan = self.provisioner.plan("qwen-asr", self.comfy)
        output = plan.format()
        self.assertIn("MODELS TO DOWNLOAD", output)
        self.assertFalse((self.comfy / "user").exists())

    def test_install_is_idempotent(self) -> None:
        plan = self.provisioner.plan("qwen-asr", self.comfy)
        self.provisioner.install(plan, skip_models=True, skip_deps=True)
        state = self.comfy / "user" / "default" / "EDx" / "provisioning-state.json"
        first = state.read_text(encoding="utf-8")
        self.provisioner.install(self.provisioner.plan("qwen-asr", self.comfy), skip_models=True, skip_deps=True)
        payload = json.loads(state.read_text(encoding="utf-8"))
        self.assertIn("qwen-asr", payload["profiles"])
        self.assertTrue(first)

    def test_music3_variants_share_encoder_vae_and_workflow(self) -> None:
        standard = self.provisioner.plan("minimax-music3", self.comfy)
        low_vram = self.provisioner.plan("minimax-music3-low-vram", self.comfy)
        self.assertEqual(standard.workflow_sets, ["minimax-music3"])
        self.assertEqual(low_vram.workflow_sets, ["minimax-music3"])
        self.assertEqual(set(standard.models) & set(low_vram.models), {"minimax-music3-text-encoder", "minimax-music3-dav"})
        self.assertNotIn("minimax-music3-dit-fp16", low_vram.models)

    def test_krea_reference_inherits_base_without_duplicate_models(self) -> None:
        plan = self.provisioner.plan("krea2-reference", self.comfy)
        self.assertEqual(plan.profiles, ["krea2-reference", "krea2"])
        self.assertEqual(len(plan.models), len(set(plan.models)))
        self.assertIn("krea2-reference-runtime", plan.components)
        self.assertIn("krea2-reference", plan.workflow_sets)

    def test_chatterbox_clone_reuses_tts_and_stays_independent_of_qwen(self) -> None:
        clone = self.provisioner.plan("chatterbox-voice-clone", self.comfy)
        qwen = self.provisioner.plan("qwen3-tts", self.comfy)
        self.assertEqual(clone.components, ["chatterbox-runtime"])
        self.assertEqual(clone.models, ["chatterbox-default-pack"])
        self.assertNotIn("qwen3-tts-runtime", clone.components)
        self.assertNotIn("chatterbox-runtime", qwen.components)

    def test_capability_discovery_exposes_alternative_voice_providers(self) -> None:
        capabilities = self.provisioner.list_capabilities()
        self.assertEqual(capabilities["text-to-speech"]["providers"], ["qwen3-tts", "chatterbox-tts"])
        self.assertIn("chatterbox-voice-clone", capabilities["voice-cloning"]["providers"])

    def test_existing_profiles_and_h3_manifest_are_still_resolvable(self) -> None:
        self.assertIn("minimax-h3-fast", self.provisioner.list_profiles())
        self.assertEqual(self.provisioner.plan("minimax-h3-fast", self.comfy).workflow_sets, ["minimax-h3-core"])


if __name__ == "__main__": unittest.main()
