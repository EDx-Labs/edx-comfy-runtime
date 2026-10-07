"""Testes de desinstalação. TODOS usam diretórios temporários; nunca a instalação real."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner, ProvisioningError, write_json
from installer.uninstall import (
    KEEP_REQUIRED, KEEP_SHARED, KEEP_UNMANAGED, MARKER, REMOVE, Uninstaller, UninstallItem, UninstallPlan, _inside,
)


class _Response:
    status = 200
    def __init__(self, data: bytes): self.data = data
    def read(self, size=-1): data, self.data = self.data, b""; return data
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


def snapshot(root: Path) -> list[tuple[str, int, int]]:
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.exists() or p.is_symlink())


class UninstallBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); self.comfy = Path(self.tmp.name) / "ComfyUI"
        (self.comfy / "models").mkdir(parents=True)
        self.p = Provisioner()
        self.payloads: dict[str, bytes] = {}
        self.p.opener = lambda request, timeout: _Response(self.payloads[request.full_url.rsplit("/", 1)[-1]])
    def tearDown(self) -> None: self.tmp.cleanup()

    def fake_models(self, *idents: str) -> None:
        for ident in idents:
            data = f"payload-{ident}".encode()
            self.payloads[ident] = data
            self.p.models[ident].update({"url": f"https://example.invalid/{ident}", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            self.p.models[ident].pop("install_mode", None)

    def models_of(self, *profiles: str) -> list[str]:
        return self.p._unique([m for prof in self.p._resolved_profiles(profiles[0]) for m in self.p.profiles[prof].get("models", [])])

    def install(self, profile: str) -> None:
        self.fake_models(*{m for prof in self.p._resolved_profiles(profile) for m in self.p.profiles[prof].get("models", [])})
        self.p.install(self.p.plan(profile, self.comfy), skip_deps=True)

    def model_file(self, ident: str) -> Path: return self.p._model_path(self.comfy, self.p.models[ident])
    def actions(self, plan: UninstallPlan, kind: str = "model") -> dict[str, str]:
        return {item.ident: item.action for item in plan.items if item.kind == kind}
    def installed(self, profile: str) -> bool: return self.p.profile_installation(profile, self.comfy)["installed"]


class KreaUninstallTests(UninstallBase):
    def test_plan_is_read_only(self) -> None:
        self.install("krea2")
        before = snapshot(self.comfy)
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertTrue(plan.by_action(REMOVE))
        self.assertEqual(snapshot(self.comfy), before)

    def test_exclusive_capability_is_removed_and_bytes_are_computed(self) -> None:
        self.install("krea2")
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual(self.actions(plan), {"krea2-turbo-fp8": REMOVE, "qwen3vl-4b-fp8": REMOVE, "qwen-image-vae": REMOVE})
        expected = sum(len(f"payload-{i}") for i in ("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae"))
        self.assertEqual(plan.freed_bytes, expected)
        result = self.p.uninstall("krea2", self.comfy, plan)
        self.assertTrue(result.ok); self.assertEqual(result.freed_bytes, expected)
        self.assertFalse(any(self.model_file(i).exists() for i in self.actions(plan)))
        self.assertFalse(self.installed("krea2"))  # filesystem é a fonte da verdade

    def test_repeated_uninstall_is_idempotent_and_removes_nothing_else(self) -> None:
        self.install("krea2")
        self.p.uninstall("krea2", self.comfy)
        keep = self.comfy / "models" / "diffusion_models" / "user-own-model.safetensors"; keep.write_bytes(b"mine")
        again = self.p.uninstall("krea2", self.comfy)
        self.assertTrue(again.ok); self.assertEqual(again.removed, []); self.assertEqual(again.freed_bytes, 0)
        self.assertEqual(keep.read_bytes(), b"mine")

    def test_krea2_is_required_by_installed_reference(self) -> None:
        self.install("krea2"); self.install("krea2-reference")
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {KEEP_REQUIRED})
        self.assertEqual(self.p.uninstall("krea2", self.comfy, plan).removed, [])
        self.assertTrue(self.installed("krea2") and self.installed("krea2-reference"))

    def test_reference_removal_keeps_shared_krea2_assets(self) -> None:
        self.install("krea2"); self.install("krea2-reference")
        plan = self.p.plan_uninstall("krea2-reference", self.comfy)
        self.assertEqual(self.actions(plan)["qwen3vl-4b-bf16"], REMOVE)
        for shared in ("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae"): self.assertEqual(self.actions(plan)[shared], KEEP_SHARED)
        self.assertEqual(self.actions(plan, "workflow_set"), {"krea2-reference": REMOVE})
        self.assertTrue(self.p.uninstall("krea2-reference", self.comfy, plan).ok)
        self.assertTrue(self.installed("krea2"))  # krea2 continua íntegro
        self.assertFalse(self.model_file("qwen3vl-4b-bf16").exists())

    def test_dependency_only_krea2_goes_away_with_reference(self) -> None:
        self.install("krea2-reference")  # krea2 entra só como dependência (explicit=False)
        plan = self.p.plan_uninstall("krea2-reference", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {REMOVE})
        self.assertEqual(len(self.actions(plan)), 4)

    def test_install_group_removes_all_members(self) -> None:
        self.install("krea2-full")
        plan = self.p.plan_uninstall("krea2-full", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {REMOVE}); self.assertEqual(len(self.actions(plan)), 4)
        self.assertTrue(self.p.uninstall("krea2-full", self.comfy, plan).ok)
        self.assertFalse(self.installed("krea2-full")); self.assertFalse(self.installed("krea2-reference"))

    def test_krea_capabilities_stay_distinct(self) -> None:
        self.assertNotEqual(self.p._resolved_profiles("krea2"), self.p._resolved_profiles("krea2-reference"))


class OwnershipTests(UninstallBase):
    def test_manual_model_without_provisioning_record_is_kept(self) -> None:
        self.install("krea2"); state = provisioning.read_json(self.p._state_path(self.comfy))
        state["models"].pop("krea2-turbo-fp8"); write_json(self.p._state_path(self.comfy), state)
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual(self.actions(plan)["krea2-turbo-fp8"], KEEP_UNMANAGED)
        self.p.uninstall("krea2", self.comfy, plan); self.assertTrue(self.model_file("krea2-turbo-fp8").exists())

    def test_model_with_different_content_is_kept(self) -> None:
        self.install("krea2"); self.model_file("qwen-image-vae").write_bytes(b"x" * len(self.payloads["qwen-image-vae"]))
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual(self.actions(plan)["qwen-image-vae"], KEEP_UNMANAGED)
        self.p.uninstall("krea2", self.comfy, plan); self.assertTrue(self.model_file("qwen-image-vae").exists())

    def test_unknown_files_in_models_are_never_touched(self) -> None:
        self.install("krea2"); stray = self.model_file("krea2-turbo-fp8").parent / "stray.bin"; stray.write_bytes(b"s")
        self.p.uninstall("krea2", self.comfy); self.assertTrue(stray.exists())

    def test_partial_download_removed_only_without_lock(self) -> None:
        self.fake_models("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae")
        target = self.model_file("krea2-turbo-fp8"); target.parent.mkdir(parents=True)
        partial = target.with_suffix(target.suffix + ".partial"); partial.write_bytes(b"abc")
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual([i.action for i in plan.items if i.kind == "partial"], [REMOVE])
        lock = target.with_suffix(target.suffix + ".lock"); lock.write_text("{}")
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual([i.action for i in plan.items if i.kind == "partial"], [KEEP_UNMANAGED])
        self.p.uninstall("krea2", self.comfy, plan); self.assertTrue(partial.exists())


class MinimaxUninstallTests(UninstallBase):
    def test_shared_base_survives_while_another_installed_capability_needs_it(self) -> None:
        self.install("minimax-h3-fast"); self.install("minimax-h3-easy-continuation")
        plan = self.p.plan_uninstall("minimax-h3-easy-continuation", self.comfy)
        self.assertEqual(self.actions(plan)["minimax-fl2va-w4a8"], KEEP_SHARED)
        self.assertEqual(self.actions(plan)["minimax-clipproj-4b"], KEEP_SHARED)
        self.assertEqual(self.actions(plan, "component")["minimax-runtime"], KEEP_SHARED)
        self.assertEqual(self.actions(plan, "component")["minimax-h3-continuation"], REMOVE)
        self.assertEqual(self.actions(plan, "workflow_set")["minimax-h3-core"], KEEP_SHARED)
        self.assertTrue(self.p.uninstall("minimax-h3-easy-continuation", self.comfy, plan).ok)
        self.assertTrue(self.installed("minimax-h3-fast"))

    def test_fast_is_blocked_by_installed_continuation_and_then_freed(self) -> None:
        self.install("minimax-h3-fast"); self.install("minimax-h3-easy-continuation")
        plan = self.p.plan_uninstall("minimax-h3-fast", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {KEEP_SHARED})  # a base é necessária pela continuação
        self.p.uninstall("minimax-h3-easy-continuation", self.comfy)
        plan = self.p.plan_uninstall("minimax-h3-fast", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {REMOVE})
        self.assertTrue(self.p.uninstall("minimax-h3-fast", self.comfy, plan).ok)
        self.assertFalse(self.installed("minimax-h3-fast"))

    def test_hardlinked_components_leave_the_source_intact_and_free_zero_bytes(self) -> None:
        self.install("minimax-h3-fast")
        source = provisioning.ROOT / self.p.components["minimax-runtime"]["source"]
        before = {p.relative_to(source).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in provisioning._local_files(source)}
        target = self.comfy / "custom_nodes" / self.p.components["minimax-runtime"]["name"]
        linked = any(os.path.samefile(next(provisioning._local_files(source)), target / next(provisioning._local_files(source)).relative_to(source)) for _ in [0])
        plan = self.p.plan_uninstall("minimax-h3-fast", self.comfy)
        component = next(i for i in plan.items if i.kind == "component" and i.ident == "minimax-runtime")
        if linked: self.assertEqual(component.size_bytes, 0)
        self.assertTrue(self.p.uninstall("minimax-h3-fast", self.comfy, plan).ok)
        self.assertFalse(target.exists())
        after = {p.relative_to(source).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in provisioning._local_files(source)}
        self.assertEqual(before, after)


class LocalComponentTests(unittest.TestCase):
    """Component local sintético: copia, extras, links e falhas sem tocar nos manifests reais."""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); base = Path(self.tmp.name)
        self.comfy = base / "ComfyUI"; (self.comfy / "custom_nodes").mkdir(parents=True); (self.comfy / "models").mkdir()
        self.src = base / "src"; (self.src / "sub").mkdir(parents=True)
        (self.src / "__init__.py").write_text("a"); (self.src / "sub" / "x.py").write_text("x"); (self.src / "sub" / "y.txt").write_text("y")
        self.p = Provisioner(); self.p.components = {"c": {"name": "Node", "source": "src", "version": "1"}}
        self.p.profiles = {"p": {"components": ["c"]}, "other": {"components": ["c"]}}
        self.p.models = {}; self.p.workflow_sets = {}
        patcher = mock.patch.object(provisioning, "ROOT", base); patcher.start(); self.addCleanup(patcher.stop)
        self.target = self.comfy / "custom_nodes" / "Node"
    def tearDown(self) -> None: self.tmp.cleanup()
    def install(self, profile="p") -> None: self.p.install(self.p.plan(profile, self.comfy), skip_deps=True)
    def source_snapshot(self) -> dict[str, bytes]: return {str(p.relative_to(self.src)): p.read_bytes() for p in self.src.rglob("*") if p.is_file()}

    def test_extra_user_files_survive_and_install_can_be_redone(self) -> None:
        self.install(); extra = self.target / "sub" / "mine.txt"; extra.write_text("user"); top = self.target / "notes.md"; top.write_text("n")
        before = self.source_snapshot()
        plan = self.p.plan_uninstall("p", self.comfy)
        self.assertEqual({Path(i.path).name for i in plan.items if i.action == KEEP_UNMANAGED}, {"mine.txt", "notes.md"})
        self.assertTrue(self.p.uninstall("p", self.comfy, plan).ok)
        self.assertEqual(extra.read_text(), "user"); self.assertEqual(top.read_text(), "n")
        self.assertFalse((self.target / "__init__.py").exists())
        self.assertEqual(self.source_snapshot(), before)  # a fonte nunca é tocada (hardlinks)
        self.assertEqual(self.p.plan("p", self.comfy).to_install, ["component:c"])  # marcador-resíduo não afirma "instalado"
        self.assertEqual([i for i in self.p.plan_uninstall("p", self.comfy).items if i.action == REMOVE], [])  # idempotente
        self.install(); self.assertEqual(self.p.plan("p", self.comfy).to_install, []); self.assertEqual(extra.read_text(), "user")

    def test_edited_copy_is_kept(self) -> None:
        self.install(); mod = self.target / "sub" / "y.txt"; mod.unlink(); mod.write_text("edited")
        plan = self.p.plan_uninstall("p", self.comfy)
        self.assertIn(KEEP_UNMANAGED, [i.action for i in plan.items if Path(i.path).name == "y.txt"])
        self.p.uninstall("p", self.comfy, plan); self.assertEqual(mod.read_text(), "edited")

    def test_real_copy_is_removed_and_counts_bytes(self) -> None:
        self.install()
        for f in list(provisioning._local_files(self.target)):  # força cópias independentes (sem hardlink)
            data = f.read_bytes(); f.unlink(); f.write_bytes(data)
        plan = self.p.plan_uninstall("p", self.comfy)
        component = next(i for i in plan.items if i.kind == "component")
        self.assertEqual(component.size_bytes, 3)  # a + x + y, um byte cada
        self.assertTrue(self.p.uninstall("p", self.comfy, plan).ok)
        self.assertFalse(self.target.exists())

    def test_generated_pycache_of_owned_module_is_removed(self) -> None:
        self.install(); pyc = self.target / "__pycache__" / "__init__.cpython-311.pyc"; pyc.parent.mkdir(); pyc.write_bytes(b"x")
        self.p.uninstall("p", self.comfy); self.assertFalse(self.target.exists())

    def test_component_without_marker_is_external_and_preserved(self) -> None:
        self.target.mkdir(); (self.target / "__init__.py").write_text("a")
        plan = self.p.plan_uninstall("p", self.comfy)
        self.assertEqual([i.action for i in plan.items], [KEEP_UNMANAGED])
        self.p.uninstall("p", self.comfy, plan); self.assertTrue((self.target / "__init__.py").exists())

    def test_shared_component_between_two_installed_profiles_is_kept(self) -> None:
        self.install("p"); self.install("other")
        plan = self.p.plan_uninstall("p", self.comfy)
        self.assertEqual([i.action for i in plan.items if i.kind == "component"], [KEEP_SHARED])

    def test_directory_link_is_unlinked_without_touching_the_source(self) -> None:
        if os.name == "nt":
            done = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.target), str(self.src)], capture_output=True)
            if done.returncode != 0: self.skipTest("junction indisponível")
        else:
            try: os.symlink(self.src, self.target, target_is_directory=True)
            except OSError: self.skipTest("symlink indisponível")
        before = self.source_snapshot()
        plan = self.p.plan_uninstall("p", self.comfy)
        self.assertEqual([i.kind for i in plan.items], ["component_link"])
        self.assertTrue(self.p.uninstall("p", self.comfy, plan).ok)
        self.assertFalse(os.path.lexists(self.target)); self.assertEqual(self.source_snapshot(), before)

    def test_link_to_unknown_destination_is_kept(self) -> None:
        elsewhere = Path(self.tmp.name) / "elsewhere"; elsewhere.mkdir(); (elsewhere / "data.txt").write_text("d")
        if os.name == "nt":
            done = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.target), str(elsewhere)], capture_output=True)
            if done.returncode != 0: self.skipTest("junction indisponível")
        else: os.symlink(elsewhere, self.target, target_is_directory=True)
        plan = self.p.plan_uninstall("p", self.comfy)
        self.assertEqual([i.action for i in plan.items], [KEEP_UNMANAGED])
        self.p.uninstall("p", self.comfy, plan); self.assertTrue(os.path.lexists(self.target)); self.assertTrue((elsewhere / "data.txt").exists())

    def test_partial_failure_preserves_the_rest_and_next_run_reconciles(self) -> None:
        self.install(); calls = []; real_unlink = os.unlink
        def flaky(path, *a, **k):
            calls.append(path)
            if len(calls) == 2: raise PermissionError("locked")
            return real_unlink(path, *a, **k)
        for f in list(provisioning._local_files(self.target)): data = f.read_bytes(); f.unlink(); f.write_bytes(data)
        calls.clear()
        with mock.patch("os.unlink", flaky):
            result = self.p.uninstall("p", self.comfy)
        self.assertFalse(result.ok); self.assertEqual(result.removed, [])
        self.assertTrue(self.target.exists()); self.assertEqual(len(list(provisioning._local_files(self.target))), 3)  # 1 removido; 2 fontes + marcador preservados
        self.assertTrue(self.p.uninstall("p", self.comfy).ok); self.assertFalse(self.target.exists())

    def test_changed_between_plan_and_execute_is_preserved(self) -> None:
        self.install(); plan = self.p.plan_uninstall("p", self.comfy)
        marker = self.target / MARKER; marker.write_text(json.dumps({"component": "someone-else"}))  # deixou de ser nosso
        result = self.p.uninstall("p", self.comfy, plan)
        self.assertEqual(result.removed, []); self.assertTrue(result.skipped); self.assertTrue((self.target / "__init__.py").exists())

    def test_forged_plan_outside_roots_is_not_executed(self) -> None:
        outside = Path(self.tmp.name) / "outside.txt"; outside.write_text("keep")
        forged = UninstallPlan("p", str(self.comfy), ["p"], [UninstallItem("model", "x", str(outside), REMOVE, "forjado", 4)])
        result = self.p.uninstall("p", self.comfy, forged)
        self.assertTrue(outside.exists()); self.assertEqual(result.removed, [])

    def test_path_containment_and_traversal_are_rejected(self) -> None:
        root = self.comfy / "models"
        self.assertTrue(_inside(root, root / "a" / "b.bin"))
        self.assertFalse(_inside(root, root / ".." / "x.bin")); self.assertFalse(_inside(root, Path(self.tmp.name) / "x.bin"))
        self.p.components["c"]["name"] = "..\\..\\escape" if os.name == "nt" else "../../escape"
        with self.assertRaises(ProvisioningError): self.p.plan_uninstall("p", self.comfy)

    def test_unknown_target_is_an_error_not_a_noop_delete(self) -> None:
        with self.assertRaises(ProvisioningError): self.p.plan_uninstall("nope", self.comfy)


class WorkflowUninstallTests(UninstallBase):
    def test_edited_workflow_is_kept_and_extra_file_survives(self) -> None:
        self.install("krea2-reference")
        folder = self.comfy / "user" / "default" / "workflows" / "EDx" / "krea2-reference"
        workflow = next(folder.glob("*.json")); workflow.write_text("{\"edited\": true}")
        extra = folder / "my-own.json"; extra.write_text("{}")
        plan = self.p.plan_uninstall("krea2-reference", self.comfy)
        self.assertTrue(self.p.uninstall("krea2-reference", self.comfy, plan).ok)
        self.assertTrue(workflow.exists() and extra.exists())

    def test_workflow_edited_after_plan_is_preserved(self) -> None:
        self.install("krea2-reference")
        plan = self.p.plan_uninstall("krea2-reference", self.comfy)
        folder = self.comfy / "user" / "default" / "workflows" / "EDx" / "krea2-reference"
        workflow = next(folder.glob("*.json")); workflow.write_text("{\"late\": 1}")
        result = self.p.uninstall("krea2-reference", self.comfy, plan)
        self.assertTrue(workflow.exists()); self.assertTrue(any("workflows" in s["path"] for s in result.skipped) or result.ok)


class OwnershipProofTests(UninstallBase):
    """Reutilizado != criado pelo EDx: só o que o Runtime materializou é removível."""
    KREA = ("krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae")

    def state(self) -> dict:
        return provisioning.read_json(self.p._state_path(self.comfy))
    def preexisting(self, *idents: str) -> None:
        self.fake_models(*idents)
        for ident in idents:
            path = self.model_file(ident); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(self.payloads[ident])
    def reason(self, plan: UninstallPlan, ident: str) -> str:
        return next(i.reason for i in plan.items if i.kind == "model" and i.ident == ident)

    def test_downloaded_by_runtime_is_owned_and_removable(self) -> None:
        self.install("krea2")
        self.assertTrue(all(self.state()["models"][i]["owned"] is True for i in self.KREA))
        self.assertEqual(set(self.actions(self.p.plan_uninstall("krea2", self.comfy)).values()), {REMOVE})

    def test_preexisting_valid_model_is_reused_not_owned_and_preserved(self) -> None:
        self.preexisting(*self.KREA)
        self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True)
        self.assertTrue(self.installed("krea2"))
        self.assertTrue(all(self.state()["models"][i]["owned"] is False for i in self.KREA))
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {KEEP_UNMANAGED})
        self.assertIn("propriedade não comprovada", self.reason(plan, "krea2-turbo-fp8"))
        self.assertEqual(self.p.uninstall("krea2", self.comfy, plan).removed, [])
        self.assertTrue(all(self.model_file(i).exists() for i in self.KREA))

    def test_mixed_install_only_owns_what_it_created(self) -> None:
        self.preexisting("qwen3vl-4b-fp8")
        self.install("krea2")
        actions = self.actions(self.p.plan_uninstall("krea2", self.comfy))
        self.assertEqual(actions, {"krea2-turbo-fp8": REMOVE, "qwen-image-vae": REMOVE, "qwen3vl-4b-fp8": KEEP_UNMANAGED})
        self.p.uninstall("krea2", self.comfy)
        self.assertTrue(self.model_file("qwen3vl-4b-fp8").exists()); self.assertFalse(self.model_file("krea2-turbo-fp8").exists())

    def test_manual_model_with_correct_hash_and_no_state_is_kept(self) -> None:
        self.preexisting(*self.KREA)  # nunca instalado: não há estado algum
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {KEEP_UNMANAGED})

    def test_legacy_state_without_ownership_is_preserved(self) -> None:
        self.install("krea2"); state = self.state()
        for record in state["models"].values(): record.pop("owned", None); record.pop("owned_stamp", None)
        write_json(self.p._state_path(self.comfy), state)
        plan = self.p.plan_uninstall("krea2", self.comfy)
        self.assertEqual(set(self.actions(plan).values()), {KEEP_UNMANAGED})
        self.assertIn("legado", self.reason(plan, "qwen-image-vae"))

    def test_repair_never_promotes_a_reused_asset_to_owned(self) -> None:
        self.preexisting(*self.KREA)
        self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True)
        self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True, force_verify=True)  # repair
        self.assertTrue(all(self.state()["models"][i]["owned"] is False for i in self.KREA))
        self.assertEqual(set(self.actions(self.p.plan_uninstall("krea2", self.comfy)).values()), {KEEP_UNMANAGED})

    def test_repair_keeps_ownership_of_a_runtime_created_asset(self) -> None:
        self.install("krea2")
        self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True, force_verify=True)
        self.assertEqual(set(self.actions(self.p.plan_uninstall("krea2", self.comfy)).values()), {REMOVE})

    def test_download_finished_but_install_crashed_before_state_write_is_still_owned_on_retry(self) -> None:
        self.fake_models(*self.KREA)
        self.p._download("krea2-turbo-fp8", self.model_file("krea2-turbo-fp8"), self.p.models["krea2-turbo-fp8"])  # concluiu; install morreu aqui
        self.assertFalse(self.p._state_path(self.comfy).is_file() and "krea2" in self.state().get("profiles", {}))
        self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True)  # retry: o arquivo já é válido, mas foi o EDx que o criou
        self.assertEqual(self.actions(self.p.plan_uninstall("krea2", self.comfy))["krea2-turbo-fp8"], REMOVE)

    def test_resumed_partial_started_by_runtime_is_owned_when_completed(self) -> None:
        self.fake_models(*self.KREA); data = self.payloads["krea2-turbo-fp8"]; target = self.model_file("krea2-turbo-fp8")
        target.parent.mkdir(parents=True); target.with_suffix(target.suffix + ".partial").write_bytes(data[:5])
        class Resume(_Response): status = 206
        served = dict(self.payloads)
        def opener(request, timeout):
            ident = request.full_url.rsplit("/", 1)[-1]
            return Resume(served[ident][5:]) if ident == "krea2-turbo-fp8" else _Response(served[ident])
        self.p.opener = opener
        self.p.install(self.p.plan("krea2", self.comfy), skip_deps=True)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(self.actions(self.p.plan_uninstall("krea2", self.comfy))["krea2-turbo-fp8"], REMOVE)

    def test_file_replaced_after_ownership_is_no_longer_ours(self) -> None:
        self.install("krea2"); path = self.model_file("krea2-turbo-fp8"); data = path.read_bytes()
        path.unlink(); path.write_bytes(data)  # mesmo conteúdo, outro arquivo (novo mtime): prova de criação perdida
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
        self.assertEqual(self.actions(self.p.plan_uninstall("krea2", self.comfy))["krea2-turbo-fp8"], KEEP_UNMANAGED)

    def test_shared_owned_model_stays_shared_and_repeated_uninstall_is_safe(self) -> None:
        self.install("krea2"); self.install("krea2-reference")
        plan = self.p.plan_uninstall("krea2-reference", self.comfy)
        self.assertEqual(self.actions(plan)["qwen3vl-4b-fp8"], KEEP_SHARED)  # owned, mas outra capability instalada usa
        self.assertEqual(self.actions(plan)["qwen3vl-4b-bf16"], REMOVE)
        self.assertTrue(self.p.uninstall("krea2-reference", self.comfy, plan).ok)
        again = self.p.uninstall("krea2-reference", self.comfy)
        self.assertTrue(again.ok and again.removed == [] and again.freed_bytes == 0)
        self.assertTrue(self.installed("krea2"))


if __name__ == "__main__":
    unittest.main()
