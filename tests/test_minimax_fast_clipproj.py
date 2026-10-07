"""minimax-h3-fast inclui o ClipProj (Qwen3-VL-4B = 2560 dims, DiT = 5120) sem virar o FULL. Sem rede."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_uninstall import KEEP_SHARED, REMOVE, UninstallBase  # noqa: E402

FULL_ONLY_SETS = {"minimax-h3-extras", "clipproj"}
CLIP = "minimax-clipproj-4b"


class MiniMaxFastClipProjTests(UninstallBase):
    def requirements(self, profile: str) -> dict:
        return self.p.profile_requirements(profile)

    def test_capability_still_resolves_to_fast(self) -> None:
        self.assertEqual(self.p.resolve_profile("minimax"), "minimax-h3-fast")
        self.assertEqual(self.p.resolve_profile("video-generation"), "minimax-h3-fast")

    def test_fast_resolves_base_and_clipproj(self) -> None:
        self.assertEqual(set(self.p._resolved_profiles("minimax-h3-fast")), {"minimax-h3-base", "minimax-h3-fast"})
        req = self.requirements("minimax-h3-fast")
        self.assertIn(CLIP, req["models"])
        self.assertIn("clipproj", req["components"])
        for ident in ("minimax-fl2va-w4a8", "minimax-ref2va-w4a8", "qwen3vl-4b-fp8", "minimax-runtime"):
            self.assertIn(ident, req["models"] + req["components"])

    def test_clipproj_model_has_integrity_data(self) -> None:
        m = self.p.models[CLIP]
        self.assertEqual((m["size_bytes"], m["sha256"]), (304213176, "275b389991276532d969dbb32f91ce67e170549873e61819cfec52a770660699"))

    def test_fast_does_not_pull_full_only_extras(self) -> None:
        req = self.requirements("minimax-h3-fast")
        self.assertFalse(FULL_ONLY_SETS & set(req["workflow_sets"]))
        self.assertNotIn("minimax-h3-continuation", req["components"])
        self.assertNotIn("minimax-h3-full", self.p._resolved_profiles("minimax-h3-fast"))
        fast, full = set(req["models"]), set(self.requirements("minimax-h3-full")["models"])
        self.assertEqual(fast, full)  # o FULL não tem pesos extras; os extras são workflows (pass2/SelfLift/TRT)
        self.assertEqual(self.p.profiles["minimax-h3-fast"]["workflow_sets"], [])

    def test_other_profiles_keep_their_definitions(self) -> None:
        self.assertEqual(self.p.profiles["minimax-h3-high"]["components"], [])
        self.assertEqual(self.p.profiles["minimax-h3-high"]["models"], [])
        self.assertIn(CLIP, self.p.profiles["minimax-h3-full"]["models"])
        self.assertIn(CLIP, self.p.profiles["minimax-h3-easy-continuation"]["models"])
        for profile in ("minimax-h3-high", "minimax-h3-full", "minimax-h3-easy-continuation"):
            self.p.plan(profile, self.comfy)

    def test_old_fast_install_without_clipproj_is_pending_then_ready(self) -> None:
        self.install("minimax-h3-fast")
        self.assertTrue(self.installed("minimax-h3-fast"))
        # simula instalação anterior ao ClipProj: remove modelo e componente
        self.model_file(CLIP).unlink()
        import shutil
        shutil.rmtree(self.comfy / "custom_nodes" / self.p.components["clipproj"]["name"])
        self.assertFalse(self.installed("minimax-h3-fast"))
        plan = self.p.plan("minimax-h3-fast", self.comfy)
        self.assertIn("model:" + CLIP, plan.pending_assets)
        self.assertIn("component:clipproj", plan.pending_assets)
        self.assertEqual(plan.models_to_download, [CLIP])
        self.assertIn("model:minimax-fl2va-w4a8", plan.reused_assets)
        self.p.install(plan, skip_deps=True)
        self.assertTrue(self.installed("minimax-h3-fast"))
        self.assertTrue(self.model_file(CLIP).is_file())

    def test_repeated_install_is_idempotent(self) -> None:
        self.install("minimax-h3-fast")
        again = self.p.plan("minimax-h3-fast", self.comfy)
        self.assertEqual(again.pending_assets, [])
        self.assertEqual(again.models_to_download, [])
        self.p.install(again, skip_deps=True)
        self.assertEqual(self.p.plan("minimax-h3-fast", self.comfy).pending_assets, [])

    def test_clipproj_shared_with_continuation_survives_fast_uninstall(self) -> None:
        self.install("minimax-h3-fast"); self.install("minimax-h3-easy-continuation")
        plan = self.p.plan_uninstall("minimax-h3-fast", self.comfy)
        self.assertEqual(self.actions(plan)[CLIP], KEEP_SHARED)
        self.assertEqual(self.actions(plan, "component")["clipproj"], KEEP_SHARED)
        self.p.uninstall("minimax-h3-fast", self.comfy, plan)
        self.assertTrue(self.model_file(CLIP).is_file())
        self.assertTrue(self.installed("minimax-h3-easy-continuation"))

    def test_clipproj_shared_with_fast_survives_continuation_uninstall(self) -> None:
        self.install("minimax-h3-fast"); self.install("minimax-h3-easy-continuation")
        plan = self.p.plan_uninstall("minimax-h3-easy-continuation", self.comfy)
        self.assertEqual(self.actions(plan)[CLIP], KEEP_SHARED)
        self.assertEqual(self.actions(plan, "component")["clipproj"], KEEP_SHARED)
        self.assertEqual(self.actions(plan, "component")["minimax-h3-continuation"], REMOVE)
        self.p.uninstall("minimax-h3-easy-continuation", self.comfy, plan)
        self.assertTrue(self.installed("minimax-h3-fast"))


if __name__ == "__main__":
    unittest.main()
