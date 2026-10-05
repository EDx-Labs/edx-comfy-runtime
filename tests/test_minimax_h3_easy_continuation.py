from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer.provisioning import Provisioner


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "video" / "minimax-h3-easy-continuation-api.json"
VISUAL_WORKFLOW = ROOT / "workflows" / "video" / "minimax-h3-easy-continuation.json"
API_SHA256 = "f0fe2c225a4e271faf6146d73b7b5d7e68bf191a0bacdb3b50f2b47d3ae69c82"
BASELINE_WORKFLOW = (
    ROOT
    / "integrations"
    / "video"
    / "minimax"
    / "comfyui-minimaxh3-easy"
    / "workflow"
    / "1.MiniMax_H3_Easy.json"
)
BASELINE_SHA256 = "0099a3c1387958192b3e5e2da57d3c11e4447d5cbc62e909ad9833ad8cbb2223"
CONTINUATION_SOURCE = (
    ROOT
    / "integrations"
    / "video"
    / "minimax"
    / "ComfyUI-Minimax-H3-Continuation"
    / "continuation_nodes.py"
)


class MiniMaxH3EasyContinuationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        self.visual_workflow = json.loads(VISUAL_WORKFLOW.read_text(encoding="utf-8"))
        self.provisioner = Provisioner()

    def test_production_easy_workflow_is_byte_identical(self) -> None:
        digest = hashlib.sha256(BASELINE_WORKFLOW.read_bytes()).hexdigest()
        self.assertEqual(digest, BASELINE_SHA256)
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            baseline = self.provisioner.plan("minimax-h3-base", comfy)
        self.assertNotIn("minimax-h3-continuation", baseline.components)
        self.assertNotIn("minimax-h3-easy-continuation", baseline.workflow_sets)

    def test_api_workflow_is_byte_identical(self) -> None:
        self.assertEqual(hashlib.sha256(WORKFLOW.read_bytes()).hexdigest(), API_SHA256)

    def test_visual_workflow_links_and_slot_types_are_valid(self) -> None:
        nodes = {node["id"]: node for node in self.visual_workflow["nodes"]}
        self.assertEqual(len(nodes), len(self.visual_workflow["nodes"]))
        links = {link[0]: link for link in self.visual_workflow["links"]}
        self.assertEqual(len(links), len(self.visual_workflow["links"]))

        for link_id, origin_id, origin_slot, target_id, target_slot, value_type in links.values():
            self.assertIn(origin_id, nodes)
            self.assertIn(target_id, nodes)
            origin = nodes[origin_id]["outputs"][origin_slot]
            target = nodes[target_id]["inputs"][target_slot]
            if value_type != "*":
                self.assertEqual(origin["type"], value_type, link_id)
            self.assertIn(target["type"], {value_type, "*"}, link_id)
            self.assertIn(link_id, origin["links"])
            self.assertEqual(target["link"], link_id)

    def test_visual_workflow_matches_api_pipeline_and_models(self) -> None:
        visual = {str(node["id"]): node for node in self.visual_workflow["nodes"]}
        links = {link[0]: link for link in self.visual_workflow["links"]}
        self.assertEqual({key: node["class_type"] for key, node in self.workflow.items()},
                         {key: node["type"] for key, node in visual.items()})
        for node_id, api_node in self.workflow.items():
            visual_inputs = {item["name"]: item for item in visual[node_id]["inputs"]}
            for name, value in api_node.get("inputs", {}).items():
                if not (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)):
                    continue
                visual_input = visual_inputs[name]
                link = links[visual_input["link"]]
                self.assertEqual((str(link[1]), link[2]), (value[0], value[1]), (node_id, name))
        self.assertEqual(sum(node["type"] == "UNETLoader" for node in visual.values()), 1)
        self.assertEqual(visual["1"]["widgets_values"][0], self.workflow["1"]["inputs"]["unet_name"])
        self.assertEqual(visual["2"]["widgets_values"][0], self.workflow["2"]["inputs"]["clip_name"])
        self.assertEqual(visual["3"]["widgets_values"][0], self.workflow["3"]["inputs"]["projection"])
        self.assertEqual(visual["16"]["widgets_values"][:2], [22, 119])
        for node_id in ("7", "14"):
            values = visual[node_id]["widgets_values"]
            api = self.workflow[node_id]["inputs"]
            self.assertEqual(values[0], api["mode"])
            self.assertEqual(values[1], api["prompt"])
            self.assertEqual(values[3:15], [
                api["resolution"], api["aspect_ratio"], api["width"], api["height"],
                api["seconds"], api["advanced"], api["fps"], api["keyframe_role"],
                api["ref_image_size"], api["reference_mention_mode"],
                api["prompt_optimizer_settings"], api["prompt_optimizer_scene_guide"],
            ])
        self.assertEqual(visual["24"]["inputs"][0]["link"], 44)
        serialized = json.dumps(self.visual_workflow)
        self.assertNotIn("minimax_h3_fl2va_bf16", serialized)
        self.assertNotIn("qwen3vl_32b", serialized)

    def test_profile_reuses_registered_easy_models_without_heavy_h3_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            base = self.provisioner.plan("minimax-h3-base", comfy)
            continuation = self.provisioner.plan("minimax-h3-easy-continuation", comfy)

        self.assertEqual(
            set(continuation.models),
            set(base.models) | {"minimax-clipproj-4b"},
        )
        self.assertEqual(len(continuation.models), len(set(continuation.models)))
        self.assertIn("minimax-runtime", continuation.components)
        self.assertIn("minimax-h3-continuation", continuation.components)
        self.assertIn("clipproj", continuation.components)
        self.assertNotIn("minimax_h3_fl2va_bf16.safetensors", str(continuation.models))
        self.assertNotIn("qwen3vl_32b_minimax_h3_bf16.safetensors", str(continuation.models))

    def test_clean_install_provisions_shared_runtime_continuation_and_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            plan = self.provisioner.plan("minimax-h3-easy-continuation", comfy)
            self.provisioner.install(plan, skip_models=True, skip_deps=True)

            self.assertTrue(
                (comfy / "custom_nodes" / "comfyui-minimaxh3-easy-optimized" / ".edx-component.json").is_file()
            )
            self.assertTrue(
                (comfy / "custom_nodes" / "ComfyUI-Minimax-H3-Continuation" / ".edx-component.json").is_file()
            )
            self.assertTrue(
                (comfy / "custom_nodes" / "ComfyUI-ClipProj" / ".edx-component.json").is_file()
            )
            self.assertTrue(
                (
                    comfy
                    / "user"
                    / "default"
                    / "workflows"
                    / "EDx"
                    / "minimax-h3-easy-continuation"
                    / WORKFLOW.name
                ).is_file()
            )
            self.assertTrue(
                (
                    comfy
                    / "user"
                    / "default"
                    / "workflows"
                    / "EDx"
                    / "minimax-h3-easy-continuation"
                    / VISUAL_WORKFLOW.name
                ).is_file()
            )

    def test_official_graph_uses_registry_paths_and_one_shared_model(self) -> None:
        registry = self.provisioner.models
        self.assertEqual(
            self.workflow["1"]["inputs"]["unet_name"],
            registry["minimax-fl2va-w4a8"]["filename"],
        )
        self.assertEqual(
            self.workflow["2"]["inputs"]["clip_name"],
            registry["qwen3vl-4b-fp8"]["filename"],
        )
        self.assertEqual(
            self.workflow["3"]["inputs"]["projection"],
            registry["minimax-clipproj-4b"]["filename"],
        )
        self.assertEqual(
            self.workflow["4"]["inputs"]["vae_name"],
            registry["minimax-video-vae"]["filename"],
        )
        self.assertEqual(
            self.workflow["5"]["inputs"]["vae_name"],
            registry["minimax-audio-vae"]["filename"],
        )
        self.assertEqual(self.workflow["12"]["inputs"]["model"], ["7", 0])
        self.assertEqual(self.workflow["18"]["inputs"]["model"], ["7", 0])
        self.assertEqual(
            sum(node["class_type"] == "UNETLoader" for node in self.workflow.values()),
            1,
        )

    def test_graph_preserves_native_av_latent_tail_continuation(self) -> None:
        classes = {node["class_type"] for node in self.workflow.values()}
        self.assertTrue(
            {
                "MiniMaxH3EasyModelAdapter",
                "MiniMaxH3Easy",
                "MiniMaxH3EasyOutput",
                "MiniMaxH3GuidedContinuationWindow",
                "MiniMaxH3LatentTailGuide",
                "MiniMaxH3AppendContinuation",
            }.issubset(classes)
        )
        window = self.workflow["16"]["inputs"]
        self.assertEqual(window["previous_av_latent"], ["13", 0])
        self.assertEqual(window["overlap_frames"], 22)
        self.assertEqual(window["extension_frames"], 119)
        self.assertEqual(self.workflow["17"]["inputs"]["target_av_latent"], ["16", 0])
        self.assertEqual(self.workflow["19"]["inputs"]["latent_image"], ["16", 0])
        self.assertEqual(self.workflow["20"]["inputs"]["sampled_window"], ["19", 0])
        self.assertNotIn("LoadImage", classes)

    def test_runtime_parameters_are_explicit_and_append_is_cumulative(self) -> None:
        initial = self.workflow["7"]["inputs"]
        extension = self.workflow["14"]["inputs"]
        scheduler = self.workflow["11"]["inputs"]
        self.assertEqual((initial["width"], initial["height"]), (768, 448))
        self.assertEqual((extension["width"], extension["height"]), (768, 448))
        self.assertEqual(initial["fps"], 24)
        self.assertEqual(extension["fps"], 24)
        self.assertEqual(self.workflow["9"]["inputs"]["noise_seed"], 2345235234)
        self.assertEqual((scheduler["scheduler"], scheduler["steps"], scheduler["denoise"]), ("beta", 20, 1))
        self.assertEqual(self.workflow["20"]["inputs"]["previous_av_latent"], ["13", 0])
        self.assertEqual(self.workflow["21"]["inputs"]["samples"], ["20", 0])
        self.assertEqual(self.workflow["22"]["inputs"]["samples"], ["20", 0])

    def test_all_api_links_resolve_and_output_is_saved(self) -> None:
        for node in self.workflow.values():
            for value in node.get("inputs", {}).values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    self.assertIn(value[0], self.workflow)
                    self.assertIsInstance(value[1], int)
        self.assertEqual(self.workflow["24"]["class_type"], "SaveVideo")
        self.assertEqual(self.workflow["24"]["inputs"]["video"], ["23", 0])
        self.assertEqual(self.workflow["25"]["class_type"], "RAMCleanup")
        self.assertEqual(self.workflow["25"]["inputs"]["anything"], ["24", 0])

    def test_continuation_nodes_are_loader_agnostic_but_enforce_h3_layout(self) -> None:
        source = CONTINUATION_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("UNETLoader", source)
        self.assertNotIn("CLIPLoader", source)
        self.assertNotIn("VAELoader", source)
        self.assertIn("VIDEO_LATENT_CHANNELS = 24", source)
        self.assertIn("AUDIO_LATENT_CHANNELS = 32", source)
        self.assertIn("isinstance(samples, NestedTensor)", source)
        self.assertIn('"minimax_keyframes": keyframes', source)
        self.assertIn("requires ComfyUI commit e01fb4c or newer", source)


if __name__ == "__main__":
    unittest.main()


class MinimaxRuntimePipCommandTests(unittest.TestCase):
    """Efetivo: provisiona o profile real e inspeciona os comandos pip construídos."""

    def test_provisioning_never_runs_editable_pip_for_minimax_runtime(self) -> None:
        provisioner = Provisioner()
        self.assertEqual(provisioner.components["minimax-runtime"]["install"], "requirements")
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            plan = provisioner.plan("minimax-h3-easy-continuation", comfy)
            with mock.patch.object(Provisioner, "_run_cancellable") as run:
                provisioner.install(plan, skip_models=True, skip_deps=False)
            commands = [list(call.args[0]) for call in run.call_args_list]
            target = comfy / "custom_nodes" / "comfyui-minimaxh3-easy-optimized"
            self.assertTrue(target.is_dir())
            self.assertFalse((target / "requirements.txt").exists())
            for command in commands:
                self.assertNotIn(str(target), command, command)
                self.assertFalse("-e" in command and "comfyui-minimaxh3-easy-optimized" in " ".join(command), command)
