"""MiniMax Music 3: manifesto confere com o upstream e a instalação funciona (payloads pequenos, sem rede)."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from installer.provisioning import Provisioner, ProvisioningError

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "audio" / "minimax-music3" / "text-to-music.json"
# Verificado em huggingface.co/Comfy-Org/MiniMax-Music-3 (tree API + X-Linked-Size/ETag + Content-Range); nunca mudou entre revisões.
UPSTREAM = {
    "minimax-music3-dit-fp16": ("diffusion_models/minimax_music3_dit_fp16.safetensors", 4914197682, "45494a2b6b69af115902ff28eaf54118d19067aa54da01000f3e3efce7ba0e34"),
    "minimax-music3-dit-int8": ("diffusion_models/minimax_music3_dit_int8_convrot.safetensors", 2502161682, "d6b959633e69899f99f3a92d6741c0fe79f26958a30811e50e372ef978b24d5f"),
    "minimax-music3-text-encoder": ("text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors", 9196611886, "010b7416d2336a08c711bc22ee65849c9623069ddb7d89bec011a75699e52014"),
    "minimax-music3-dav": ("vae/minimax_music3_dav.safetensors", 216696128, "2a32155b769be01445fcc2a8663b910fc9e1751e18dc1c3ec528064512d9ef0c"),
}


class Response:
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None): self.data, self.status, self.headers = data, status, headers or {}
    def read(self, size=-1): data, self.data = self.data, b""; return data
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


class Music3ManifestTests(unittest.TestCase):
    def setUp(self) -> None: self.p = Provisioner()

    def test_registry_matches_verified_upstream_artifacts(self) -> None:
        for ident, (path, size, sha) in UPSTREAM.items():
            model = self.p.models[ident]
            self.assertEqual(f"{model['directory']}/{model['filename']}", path, ident)
            self.assertEqual(urlparse(model["url"]).path, f"/Comfy-Org/MiniMax-Music-3/resolve/main/{path}", ident)
            self.assertEqual(model["size_bytes"], size, ident)  # antes divergia do arquivo real -> "download inválido"
            self.assertEqual(model["sha256"], sha, ident)

    def test_loader_names_and_folders_in_the_workflow_match_the_registry(self) -> None:
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8")); found: dict[str, str] = {}
        def walk(node):
            if isinstance(node, dict):
                for model in (node.get("properties") or {}).get("models", []) if isinstance(node.get("properties"), dict) else []:
                    found[model["name"]] = model["directory"]
                for value in node.values(): walk(value)
            elif isinstance(node, list):
                for value in node: walk(value)
        walk(workflow)
        for ident in ("minimax-music3-dit-fp16", "minimax-music3-text-encoder", "minimax-music3-dav"):
            model = self.p.models[ident]; self.assertEqual(found.get(model["filename"]), model["directory"], ident)
        self.assertIn(self.p.models["minimax-music3-dit-int8"]["filename"], WORKFLOW.read_text(encoding="utf-8"))  # INT8 é opção do loader

    def test_profiles_resolve_and_hardware_is_not_a_blocker(self) -> None:
        low = self.p.profiles["minimax-music3-low-vram"]
        self.assertEqual(low["models"], ["minimax-music3-dit-int8", "minimax-music3-text-encoder", "minimax-music3-dav"])
        self.assertEqual(low["workflow_sets"], ["minimax-music3"])
        for profile in ("minimax-music3", "minimax-music3-low-vram"):
            self.assertEqual(self.p.profile_provisionability(profile), {"installable": True, "reason_codes": []})
        total = sum(self.p.models[m]["size_bytes"] for m in low["models"])
        self.assertEqual(self.p.profile_requirements("minimax-music3-low-vram")["download_bytes"], total)

    def test_every_registry_url_ends_with_its_filename_and_has_integrity_for_music3(self) -> None:
        for ident in UPSTREAM:
            model = self.p.models[ident]
            self.assertEqual(urlparse(model["url"]).path.rsplit("/", 1)[-1], model["filename"])
            self.assertRegex(model["sha256"], r"^[0-9a-f]{64}$")


class Music3InstallTests(unittest.TestCase):
    """Mesma forma do manifesto real, com payloads pequenos."""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); self.comfy = Path(self.tmp.name) / "ComfyUI"; (self.comfy / "models").mkdir(parents=True)
        self.p = Provisioner(sleeper=lambda _: None); self.payloads: dict[str, bytes] = {}; self.requests: list[tuple[str, str | None]] = []
        for ident in UPSTREAM:
            data = (ident + "-bytes").encode() * 20; self.payloads[ident] = data
            self.p.models[ident].update({"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        def opener(request, timeout):
            ident = request.full_url.rsplit("/", 1)[-1]
            key = next(k for k, m in self.p.models.items() if m["filename"] == ident)
            self.requests.append((key, request.headers.get("Range")))
            return Response(self.payloads[key])
        self.p.opener = opener
    def tearDown(self) -> None: self.tmp.cleanup()
    def path(self, ident: str) -> Path: return self.p._model_path(self.comfy, self.p.models[ident])

    def test_install_validates_and_places_models_where_the_workflow_looks(self) -> None:
        self.p.install(self.p.plan("minimax-music3-low-vram", self.comfy), skip_deps=True)
        self.assertTrue(self.p.profile_installation("minimax-music3-low-vram", self.comfy)["installed"])
        self.assertTrue((self.comfy / "models" / "diffusion_models" / "minimax_music3_dit_int8_convrot.safetensors").is_file())
        self.assertTrue((self.comfy / "models" / "text_encoders" / "minimax_music3_text_encoder_pruned_int8_convrot.safetensors").is_file())
        self.assertTrue((self.comfy / "models" / "vae" / "minimax_music3_dav.safetensors").is_file())
        self.assertFalse(list((self.comfy / "models").rglob("*.partial")))

    def test_second_install_and_repair_are_idempotent_and_keep_ownership(self) -> None:
        plan = lambda: self.p.plan("minimax-music3-low-vram", self.comfy)
        self.p.install(plan(), skip_deps=True); first = len(self.requests)
        self.p.install(plan(), skip_deps=True); self.p.install(plan(), skip_deps=True, force_verify=True)
        self.assertEqual(len(self.requests), first)  # nada rebaixado
        state = json.loads(self.p._state_path(self.comfy).read_text(encoding="utf-8"))
        self.assertTrue(all(state["models"][m]["owned"] for m in ("minimax-music3-dit-int8", "minimax-music3-text-encoder", "minimax-music3-dav")))

    def test_resume_of_a_partial_completes_and_validates(self) -> None:
        data = self.payloads["minimax-music3-dit-int8"]; target = self.path("minimax-music3-dit-int8"); target.parent.mkdir(parents=True)
        target.with_suffix(target.suffix + ".partial").write_bytes(data[:50])
        self.p.opener = lambda request, timeout: (self.requests.append(("r", request.headers.get("Range"))), Response(data[50:], 206, {"Content-Range": f"bytes 50-{len(data) - 1}/{len(data)}"}))[1]
        self.assertTrue(self.p._download("minimax-music3-dit-int8", target, self.p.models["minimax-music3-dit-int8"]))
        self.assertEqual(target.read_bytes(), data); self.assertEqual(self.requests, [("r", "bytes=50-")])

    def test_size_mismatch_is_reported_with_the_real_cause(self) -> None:
        """Reproduz a falha real: o servidor entrega o arquivo inteiro, mas o registry esperava outro tamanho."""
        model = self.p.models["minimax-music3-dit-int8"]; real = self.payloads["minimax-music3-dit-int8"]
        model["size_bytes"] = len(real) - 7  # registry desatualizado
        target = self.path("minimax-music3-dit-int8")
        with self.assertRaises(ProvisioningError) as caught: self.p._download("minimax-music3-dit-int8", target, model)
        message = str(caught.exception)
        self.assertIn("download inválido para minimax-music3-dit-int8", message); self.assertIn("MODEL_DOWNLOAD_SIZE_MISMATCH", message)
        self.assertIn(str(len(real)), message); self.assertIn(str(len(real) - 7), message)
        self.assertFalse(target.exists())  # nunca promovido

    def test_sha_mismatch_is_reported_and_not_promoted(self) -> None:
        model = self.p.models["minimax-music3-dav"]; model["sha256"] = "0" * 64
        target = self.path("minimax-music3-dav")
        with self.assertRaisesRegex(ProvisioningError, "MODEL_DOWNLOAD_SHA256_MISMATCH"): self.p._download("minimax-music3-dav", target, model)
        self.assertFalse(target.exists()); self.assertFalse(target.with_suffix(target.suffix + ".partial").exists())

    def test_html_error_page_with_status_200_is_rejected_without_creating_a_partial(self) -> None:
        model = self.p.models["minimax-music3-dav"]
        self.p.opener = lambda request, timeout: Response(b"<html>Access denied</html>", 200, {"Content-Type": "text/html; charset=utf-8"})
        target = self.path("minimax-music3-dav")
        with self.assertRaisesRegex(ProvisioningError, "MODEL_DOWNLOAD_INVALID_RESPONSE"): self.p._download("minimax-music3-dav", target, model)
        self.assertFalse(target.exists()); self.assertFalse(target.with_suffix(target.suffix + ".partial").exists())

    def test_short_body_without_content_type_is_a_truncation_not_a_valid_file(self) -> None:
        model = self.p.models["minimax-music3-dav"]
        self.p.opener = lambda request, timeout: Response(b"short")
        target = self.path("minimax-music3-dav")
        with self.assertRaisesRegex(ProvisioningError, "download interrompido"): self.p._download("minimax-music3-dav", target, model)
        self.assertFalse(target.exists())  # nunca promovido


if __name__ == "__main__":
    unittest.main()
