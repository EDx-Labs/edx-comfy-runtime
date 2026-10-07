"""Manifest do Krea2 básico: tamanhos/SHA reais (o size_bytes do turbo já foi declarado errado: 14.066.017.894).

Valores verificados contra a API do Hugging Face (Comfy-Org/Krea-2) e contra o arquivo baixado. Sem rede.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from installer.provisioning import Provisioner

TURBO = ("krea2-turbo-fp8", "diffusion_models", "krea2_turbo_fp8_scaled.safetensors", 13141730784,
         "eb4dd8c612cfd10f64f25b057e6e6bbcb5737c94a7372177e456dbf7579502f1")
ENCODER = ("qwen3vl-4b-fp8", "text_encoders", "qwen3vl_4b_fp8_scaled.safetensors", 5242467968,
           "54bd5144df0bbc25dd6ccadfcb826b521445a1b06ae5a42570bdd2974ca87094")
VAE = ("qwen-image-vae", "vae", "qwen_image_vae.safetensors", 253806246,
       "a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f")


class Krea2ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Provisioner()

    def test_turbo_size_is_the_real_remote_size(self) -> None:
        ident, directory, filename, size, sha = TURBO
        model = self.p.models[ident]
        self.assertEqual(model["size_bytes"], size, "o tamanho remoto real é 13.141.730.784 (não 14.066.017.894)")
        self.assertEqual((model["directory"], model["filename"], model["sha256"]), (directory, filename, sha))
        self.assertRegex(model["url"], r"^https://huggingface\.co/Comfy-Org/Krea-2/resolve/main/diffusion_models/krea2_turbo_fp8_scaled\.safetensors$")

    def test_krea2_assets_match_verified_sizes_and_hashes(self) -> None:
        for ident, directory, filename, size, sha in (TURBO, ENCODER, VAE):
            model = self.p.models[ident]
            self.assertEqual((model["directory"], model["filename"], model["size_bytes"], model["sha256"]), (directory, filename, size, sha), ident)
            self.assertRegex(model["sha256"], r"^[0-9a-f]{64}$")

    def test_basic_profile_needs_exactly_the_three_models_and_no_reference_assets(self) -> None:
        profile = self.p.profiles["krea2"]
        self.assertEqual(profile["models"], ["krea2-turbo-fp8", "qwen3vl-4b-fp8", "qwen-image-vae"])
        self.assertNotIn("qwen3vl-4b-bf16", self.p.profile_requirements("krea2")["models"])
        self.assertNotIn("krea2-reference", self.p._resolved_profiles("krea2"))

    def test_download_budget_is_the_sum_of_real_sizes(self) -> None:
        requirements = self.p.profile_requirements("krea2")
        self.assertEqual(requirements["download_bytes"], TURBO[3] + ENCODER[3] + VAE[3])
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            plan = self.p.plan("krea2", comfy)
            self.assertEqual(plan.estimated_bytes, TURBO[3] + ENCODER[3] + VAE[3])
            self.assertTrue(plan.download_size_complete)

    def test_a_complete_partial_counts_as_nothing_left_to_download(self) -> None:
        """Com o tamanho certo, um .partial completo zera o orçamento do turbo (antes sobravam ~924 MB fantasmas)."""
        ident, directory, filename, size, _sha = TURBO
        with tempfile.TemporaryDirectory() as tmp:
            comfy = Path(tmp) / "ComfyUI"
            target = comfy / "models" / directory
            target.mkdir(parents=True)
            partial = target / (filename + ".partial")
            with partial.open("wb") as handle:
                handle.truncate(0)
            real_stat = Path.stat

            def fake_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if Path(path) == partial:
                    class Proxy:
                        def __getattr__(self, name): return getattr(result, name)
                        st_size = size
                    return Proxy()
                return result

            Path.stat = fake_stat
            try:
                plan = self.p.plan("krea2", comfy)
            finally:
                Path.stat = real_stat
            self.assertEqual(plan.estimated_bytes, ENCODER[3] + VAE[3])
            self.assertNotEqual(plan.estimated_bytes, 14066017894 - size + ENCODER[3] + VAE[3])


if __name__ == "__main__":
    unittest.main()


class Krea2VramMetadataTests(unittest.TestCase):
    """Metadata medida (RTX 3050 6 GB: krea2 ~5,1 GB; reference 5,3 GB @768 e 5,2 GB @1024, sem OOM)."""

    def setUp(self) -> None:
        self.p = Provisioner()

    def test_krea2_declares_measured_6_8(self) -> None:
        hardware = self.p.profiles["krea2"]["hardware"]
        self.assertEqual((hardware["min_vram"], hardware["recommended_vram"]), (6, 8))
        req = self.p.profile_requirements("krea2")
        self.assertEqual((req["min_vram_mb"], req["recommended_vram_mb"]), (6 * 1024, 8 * 1024))

    def test_reference_inherits_6_8_without_its_own_requirement(self) -> None:
        self.assertNotIn("hardware", self.p.profiles["krea2-reference"])
        self.assertNotIn("hardware", self.p.components["krea2-reference-runtime"])
        req = self.p.profile_requirements("krea2-reference")
        self.assertEqual((req["min_vram_mb"], req["recommended_vram_mb"]), (6 * 1024, 8 * 1024))
        self.assertEqual(self.p.profile_requirements("krea2-full")["min_vram_mb"], 6 * 1024)

    def test_6gb_gpu_meets_the_minimum_but_not_the_recommended(self) -> None:
        req = self.p.profile_requirements("krea2-reference")
        rtx_3050 = 6 * 1024
        self.assertGreaterEqual(rtx_3050, req["min_vram_mb"])   # sem INSUFFICIENT_VRAM
        self.assertLess(rtx_3050, req["recommended_vram_mb"])   # abaixo do recomendado
        self.assertLess(4 * 1024, req["min_vram_mb"])           # GPU menor continua abaixo do mínimo
