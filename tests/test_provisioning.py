from __future__ import annotations
import json
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from installer.provisioning import (
    InstallPlan, Provisioner, ProvisioningCancelled, ProvisioningError,
    fingerprint, write_json,
)


class ProvisionerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.comfy = Path(self.tmp.name) / "ComfyUI"
        (self.comfy / "models").mkdir(parents=True)
        self.provisioner = Provisioner()

    def tearDown(self) -> None: self.tmp.cleanup()

    def fake_krea_downloads(self):
        payloads = {
            "krea2-turbo-fp8": b"diffusion",
            "qwen3vl-4b-fp8": b"fp8-encoder",
            "qwen-image-vae": b"vae",
            "qwen3vl-4b-bf16": b"bf16-encoder",
        }
        calls = []

        class Response:
            status = 200
            def __init__(self, data): self.data = data; self.offset = 0
            def read(self, size=-1):
                if self.offset >= len(self.data): return b""
                end = len(self.data) if size < 0 else min(len(self.data), self.offset + size)
                value = self.data[self.offset:end]; self.offset = end; return value
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()

        for ident, data in payloads.items():
            self.provisioner.models[ident].update({
                "url": f"https://example.invalid/{ident}",
                "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            })

        def opener(request, timeout):
            ident = request.full_url.rsplit("/", 1)[-1]
            calls.append(ident)
            return Response(payloads[ident])

        self.provisioner.opener = opener
        return payloads, calls

    def write_model(self, ident, data):
        model = self.provisioner.models[ident]
        target = self.comfy / "models" / model["directory"] / model["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

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
        self.provisioner.models["qwen3vl-4b-fp8"].pop("size_bytes", None)
        self.provisioner.models["qwen3vl-4b-fp8"].pop("sha256", None)
        for stub in ("size_bytes", "sha256"): self.provisioner.models["minimax-video-vae"].pop(stub, None)  # arquivos de teste são placeholders
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

    def test_cancel_at_commit_boundary_never_marks_profile_installed(self) -> None:
        cancel = threading.Event()
        plan = self.provisioner.plan("qwen-asr", self.comfy)

        def cancel_before_commit():
            cancel.set()
            raise ProvisioningCancelled("cancelled at commit boundary")

        with self.assertRaises(ProvisioningCancelled):
            self.provisioner.install(
                plan, skip_models=True, skip_deps=True,
                cancel_event=cancel, before_commit=cancel_before_commit,
            )

        state = self.comfy / "user" / "default" / "EDx" / "provisioning-state.json"
        self.assertFalse(state.exists())

    def test_install_progress_identifies_model_position_with_real_download_bytes(self) -> None:
        self.provisioner.models["fixture-model"] = {
            "url": "https://example.invalid/fixture",
            "filename": "fixture.bin", "directory": "checkpoints", "size_bytes": 4,
        }
        self.provisioner.profiles["fixture-profile"] = {"version": "1"}
        plan = InstallPlan(
            "fixture-profile", ["fixture-profile"], [], ["fixture-model"], [], self.comfy,
        )
        events = []

        def download(ident, target, model, *, progress=None, cancel_event=None):
            progress({
                "phase": "DOWNLOADING_MODEL", "asset": ident,
                "bytes_downloaded": 2, "bytes_total": 4, "progress": 0.5,
            })
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"data")

        with mock.patch.object(self.provisioner, "_download", side_effect=download):
            self.provisioner.install(plan, progress=events.append)

        downloading = next(item for item in events if item["phase"] == "DOWNLOADING_MODEL")
        self.assertEqual(downloading["bytes_downloaded"], 2)
        self.assertEqual(downloading["bytes_total"], 4)
        self.assertEqual(downloading["model_index"], 1)
        self.assertEqual(downloading["model_total"], 1)

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
        self.assertEqual(plan.components, [])
        self.assertEqual(
            set(plan.models),
            {"krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae", "qwen3vl-4b-bf16"},
        )
        self.assertIn("krea2-reference", plan.workflow_sets)

    def test_clean_krea_install_group_downloads_each_physical_asset_once(self) -> None:
        payloads, calls = self.fake_krea_downloads()
        plan = self.provisioner.plan("krea2-full", self.comfy)

        self.provisioner.install(plan, skip_deps=True)

        self.assertEqual(set(calls), set(plan.models))
        self.assertTrue(all(calls.count(ident) == 1 for ident in plan.models))
        self.assertTrue(self.provisioner.profile_installation("krea2", self.comfy)["installed"])
        self.assertTrue(self.provisioner.profile_installation("krea2-reference", self.comfy)["installed"])
        self.assertTrue(self.provisioner.profile_installation("krea2-full", self.comfy)["installed"])

    def test_krea_base_then_group_downloads_only_reference_delta(self) -> None:
        payloads, calls = self.fake_krea_downloads()
        self.provisioner.install(self.provisioner.plan("krea2", self.comfy), skip_deps=True)
        calls.clear()

        plan = self.provisioner.plan("krea2-full", self.comfy)
        self.assertEqual(plan.models_to_download, ["qwen3vl-4b-bf16"])
        self.assertIn("model:krea2-turbo-fp8", plan.reused_assets)
        installation = self.provisioner.profile_installation("krea2-full", self.comfy)
        self.assertEqual(installation["total_bytes"], len(payloads["qwen3vl-4b-bf16"]))
        self.assertEqual(installation["pending_assets"], [
            "model:qwen3vl-4b-bf16", "workflow_set:krea2-reference",
        ])
        self.provisioner.install(plan, skip_deps=True)

        self.assertEqual(calls, ["qwen3vl-4b-bf16"])

    def test_reference_assets_make_base_install_fully_incremental(self) -> None:
        payloads, calls = self.fake_krea_downloads()
        for ident, data in payloads.items():
            self.write_model(ident, data)

        plan = self.provisioner.plan("krea2", self.comfy)
        self.assertEqual(plan.models_to_download, [])
        self.provisioner.install(plan, skip_deps=True)

        self.assertEqual(calls, [])

    def test_krea_group_remaining_size_and_progress_exclude_reused_assets(self) -> None:
        payloads, _calls = self.fake_krea_downloads()
        for ident in ("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae"):
            self.write_model(ident, payloads[ident])
        reference = self.provisioner.models["qwen3vl-4b-bf16"]
        partial = self.comfy / "models" / reference["directory"] / (reference["filename"] + ".partial")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(payloads["qwen3vl-4b-bf16"][:4])
        events = []

        plan = self.provisioner.plan("krea2-full", self.comfy)
        self.assertEqual(plan.estimated_bytes, len(payloads["qwen3vl-4b-bf16"]) - 4)

        class RangeResponse:
            status = 206
            def __init__(self, data): self.data = data; self.done = False
            def read(self, _size=-1):
                if self.done: return b""
                self.done = True; return self.data
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *_args): self.close()

        self.provisioner.opener = lambda request, timeout: RangeResponse(
            payloads["qwen3vl-4b-bf16"][4:],
        )
        self.provisioner.install(plan, skip_deps=True, progress=events.append)

        planning = next(item for item in events if item["phase"] == "PLANNING_ASSETS")
        self.assertEqual(planning["bytes_total"], len(payloads["qwen3vl-4b-bf16"]) - 4)
        self.assertIn("model:krea2-turbo-fp8", planning["reused_assets"])
        downloading = [item for item in events if item["phase"] == "DOWNLOADING_MODEL"][-1]
        self.assertEqual(downloading["bytes_downloaded"], downloading["bytes_total"])

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

    def test_normalized_catalog_keeps_runtime_as_requirements_authority(self) -> None:
        catalog = {item["id"]: item for item in self.provisioner.capability_catalog(self.comfy)}
        minimax = catalog["minimax"]
        provider = minimax["providers"][0]
        self.assertEqual(provider["id"], "minimax-h3-fast")
        self.assertEqual(provider["requirements"]["min_vram_mb"], 12 * 1024)
        self.assertIn("cuda", provider["requirements"]["supported_backends"])
        self.assertFalse(provider["installation"]["installed"])
        self.assertFalse(minimax["operations"]["remove"])
        self.assertEqual(catalog["krea2"]["install_group"], "krea2-full")
        self.assertEqual(catalog["krea2-reference"]["install_group"], "krea2-full")
        groups = {item["id"]: item for item in self.provisioner.install_group_catalog(self.comfy)}
        self.assertEqual(groups["krea2-full"]["members"], ["krea2", "krea2-reference"])
        self.assertEqual(groups["krea2-full"]["profile"], "krea2-full")

    def test_profile_installation_is_distinct_from_manifest_availability(self) -> None:
        status = self.provisioner.profile_installation("seedvr2", self.comfy)
        self.assertFalse(status["installed"])
        self.assertIn("profile_state", status["missing"])

    def test_profile_provisionability_is_independent_of_execution_hardware(self) -> None:
        value = self.provisioner.profile_provisionability("minimax-h3-fast")
        self.assertTrue(value["installable"])
        self.assertEqual(value["reason_codes"], [])

        model = self.provisioner.models["qwen-h3-mmproj"]
        with mock.patch.dict(model, {"url": None}):
            unavailable = self.provisioner.profile_provisionability("qwen-h3")
        self.assertFalse(unavailable["installable"])
        self.assertIn("MODEL_SOURCE_UNAVAILABLE:qwen-h3-mmproj", unavailable["reason_codes"])

    def test_existing_profiles_and_h3_manifest_are_still_resolvable(self) -> None:
        self.assertIn("minimax-h3-fast", self.provisioner.list_profiles())
        self.assertEqual(self.provisioner.plan("minimax-h3-fast", self.comfy).workflow_sets, ["minimax-h3-core"])


if __name__ == "__main__": unittest.main()


class MinimaxVideoVaeDefinitionTests(unittest.TestCase):
    """Regressão do HTTP 404: filename/URL devem apontar para o arquivo que existe no repositório upstream."""

    def setUp(self) -> None:
        self.provisioner = Provisioner()
        self.model = self.provisioner.models["minimax-video-vae"]

    def test_url_is_well_formed_and_matches_filename(self) -> None:
        from urllib.parse import urlparse
        url = urlparse(self.model["url"])
        self.assertEqual((url.scheme, url.netloc), ("https", "huggingface.co"))
        self.assertEqual(url.path, "/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors")
        self.assertEqual(url.path.rsplit("/", 1)[-1], self.model["filename"])
        self.assertEqual(self.model["directory"], "vae")

    def test_integrity_metadata_is_present(self) -> None:
        self.assertEqual(self.model["size_bytes"], 5207808496)
        self.assertRegex(self.model["sha256"], r"^[0-9a-f]{64}$")

    def test_minimax_profile_still_resolves_the_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "models").mkdir()
            plan = self.provisioner.plan("minimax-h3-fast", Path(directory))
        self.assertIn("minimax-video-vae", plan.models)
        self.assertIn("minimax-video-vae", plan.models_to_download)

    def test_every_registry_url_ends_with_its_filename(self) -> None:
        for ident, item in self.provisioner.models.items():
            if item.get("url"): self.assertEqual(item["url"].rsplit("/", 1)[-1], item["filename"], ident)
