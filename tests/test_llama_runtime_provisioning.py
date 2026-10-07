"""llama.cpp como dependência executável gerenciada (qwen-h3 / qwen-text).

Nenhum teste baixa o llama.cpp real nem executa llama-server: downloads são fakes e
a validação por processo (--version/--list-devices) é substituída.
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner, ProvisioningCancelled, ProvisioningError, load_runtime_installer
from installer.text_task import describe_availability
from installer.uninstall import KEEP_SHARED, KEEP_UNMANAGED, REMOVE

COMPONENT = "qwen-h3-runtime"
NODE = "ComfyUI_Qwen_H3_Prompt"
MODELS = ("qwen-h3-27b-q4", "qwen-h3-mmproj")  # qwen-h3 (multimodal 27B)
TEXT_MODELS = ("qwen3-14b-q4-k-m",)  # qwen-text (text-only 14B)


class Response:
    status = 200

    def __init__(self, data: bytes): self.data = data; self.headers = {}
    def read(self, size=-1):
        data, self.data = self.data, b""
        return data
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items(): archive.writestr(name, data)
    return buffer.getvalue()


class LlamaRuntimeBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.comfy = Path(self.tmp.name) / "Meu Comfy UI" / "ComfyUI"  # espaços no caminho (Windows)
        (self.comfy / "models").mkdir(parents=True)
        self.p = Provisioner(sleeper=lambda _s: None)
        self.module = load_runtime_installer(provisioning.ROOT / self.p.components[COMPONENT]["source"] / "install_runtime.py")
        self.requests: list[str] = []
        self.payloads: dict[str, bytes] = {}
        self.fail_urls: dict[str, BaseException] = {}

        # modelos pequenos e locais (nada de 27 GB)
        for ident in (*MODELS, *TEXT_MODELS):
            data = f"payload-{ident}".encode()
            self.payloads[ident] = data
            self.p.models[ident].update({"url": f"https://example.invalid/{ident}", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

        # pacote Windows fake (cuda13) com exe + DLLs
        archive = make_zip({"llama-server.exe": b"MZ-fake-server", "ggml.dll": b"dll", "llama.dll": b"dll"})
        self.archive = archive
        asset = self.module.Asset("llama-fake-win-cuda13.zip", hashlib.sha256(archive).hexdigest(), "https://example.invalid/llama")
        self.payloads[asset.name] = archive
        self.package = self.module.RuntimePackage("cuda13", "fake CUDA 13", (asset,), ("CUDA",), (13, 4))
        self.hardware = self.module.HardwareInfo("x64", ("NVIDIA GeForce RTX 3050 6GB Laptop GPU",), ("nvidia",), (13, 4), None)
        patches = [
            mock.patch.object(self.module, "normalize_platform", return_value="windows"),
            mock.patch.object(self.module, "detect_hardware", return_value=self.hardware),
            mock.patch.object(self.module, "select_backend_candidates", return_value=["cuda13"]),
            mock.patch.object(self.module, "PLATFORM_PACKAGES", {"windows": {"x64": {"cuda13": self.package}}}),
            mock.patch.object(self.module, "validate_runtime", return_value=("version fake", "CUDA0 fake")),
        ]
        for patch in patches: patch.start(); self.addCleanup(patch.stop)

        def opener(request, timeout):
            name = request.full_url.rsplit("/", 1)[-1]
            self.requests.append(name)
            if name in self.fail_urls: raise self.fail_urls[name]
            return Response(self.payloads[name])
        self.p.opener = opener

    def tearDown(self) -> None: self.tmp.cleanup()

    @property
    def node(self) -> Path: return self.comfy / "custom_nodes" / NODE
    def zip_requests(self) -> list[str]: return [r for r in self.requests if r.endswith(".zip")]
    def model_requests(self) -> list[str]: return [r for r in self.requests if r in MODELS]
    def text_model_requests(self) -> list[str]: return [r for r in self.requests if r in TEXT_MODELS]
    def install(self, profile: str, **kwargs) -> None: self.p.install(self.p.plan(profile, self.comfy), skip_deps=True, **kwargs)
    def status(self, profile: str) -> dict: return self.p.profile_installation(profile, self.comfy)
    def runtime_ready(self) -> bool: return self.p.backend_runtime_status(COMPONENT, self.comfy)["ready"]


class ReadinessTests(LlamaRuntimeBase):
    def test_node_models_workflow_without_llama_are_not_installed_for_both_profiles(self) -> None:
        self.install("qwen-h3", skip_runtime=True)
        self.install("qwen-text", skip_runtime=True)
        for profile in ("qwen-h3", "qwen-text"):
            info = self.status(profile)
            self.assertFalse(info["installed"], profile)
            self.assertIn(f"runtime:{COMPONENT}", info["missing"], profile)
            self.assertEqual(info["runtime_dependencies"][0]["reason"], "RUNTIME_CONFIG_MISSING")
            self.assertNotIn(f"component:{COMPONENT}", info["missing"])
            self.assertFalse([m for m in info["missing"] if m.startswith("model:")])
        availability = describe_availability(self.p, self.comfy)
        self.assertEqual(availability["status"], "not_installed")
        self.assertEqual(availability["runtime_dependencies"][0]["ready"], False)

    def test_installed_only_after_runtime_is_provisioned(self) -> None:
        self.install("qwen-h3")
        self.assertTrue(self.status("qwen-h3")["installed"])
        self.install("qwen-text")
        self.assertTrue(self.status("qwen-text")["installed"])
        self.assertEqual(describe_availability(self.p, self.comfy)["status"], "installed")

    def test_incomplete_or_corrupt_runtime_is_detected(self) -> None:
        self.install("qwen-h3")
        config_path = self.node / "runtime_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        executable = self.node / config["executable"]
        original = executable.read_bytes()
        executable.write_bytes(b"")  # truncado
        self.assertEqual(self.p.backend_runtime_status(COMPONENT, self.comfy)["reason"], "RUNTIME_EXECUTABLE_MISSING")
        executable.unlink()
        self.assertFalse(self.status("qwen-h3")["installed"])
        executable.write_bytes(original)
        self.assertTrue(self.runtime_ready())
        for dll in executable.parent.glob("*.dll"): dll.unlink()
        self.assertEqual(self.p.backend_runtime_status(COMPONENT, self.comfy)["reason"], "RUNTIME_LIBRARY_MISSING")
        config_path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.p.backend_runtime_status(COMPONENT, self.comfy)["reason"], "RUNTIME_CONFIG_INVALID")

    def test_runtime_config_must_match_pinned_release_and_platform(self) -> None:
        self.install("qwen-h3")
        path = self.node / "runtime_config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**config, "llama_cpp_tag": "b1"}), encoding="utf-8")
        self.assertEqual(self.p.backend_runtime_status(COMPONENT, self.comfy)["reason"], "RUNTIME_VERSION_MISMATCH")
        path.write_text(json.dumps({**config, "platform": "linux"}), encoding="utf-8")
        self.assertEqual(self.p.backend_runtime_status(COMPONENT, self.comfy)["reason"], "RUNTIME_PLATFORM_MISMATCH")
        path.write_text(json.dumps({**config, "executable": "../../outside/llama-server.exe"}), encoding="utf-8")
        self.assertEqual(self.p.backend_runtime_status(COMPONENT, self.comfy)["reason"], "RUNTIME_CONFIG_INVALID")

    def test_windows_paths_with_spaces_and_exe_name(self) -> None:
        self.install("qwen-h3")
        self.assertIn(" ", str(self.node))
        config = json.loads((self.node / "runtime_config.json").read_text(encoding="utf-8"))
        self.assertTrue(config["executable"].endswith("llama-server.exe"))
        self.assertNotIn("\\", config["executable"])
        self.assertTrue(all(not Path(value).is_absolute() for value in config["library_dirs"]))
        self.assertEqual(config["llama_cpp_tag"], self.module.RELEASE_TAG)
        self.assertTrue((self.node / config["executable"]).is_file())
        # o node resolve o executável a partir do próprio diretório, sem PATH global
        spec_module = load_runtime_installer(self.node / "runtime.py")
        spec = spec_module.load_runtime_spec(self.node, "Windows")
        self.assertEqual(spec.executable, (self.node / config["executable"]).resolve())
        self.assertTrue(all(str(d).startswith(str(self.node.resolve())) for d in spec.library_dirs))
        # nome sem .exe no Windows não é aceito
        config["executable"] = config["executable"].removesuffix(".exe")
        (self.node / "runtime_config.json").write_text(json.dumps(config), encoding="utf-8")
        self.assertFalse(self.runtime_ready())

    def test_not_recommended_hardware_does_not_block_provisioning(self) -> None:
        self.assertEqual(self.hardware.gpu_names[0].split()[-3], "6GB")
        for profile in ("qwen-h3", "qwen-text"):
            self.assertTrue(self.p.profile_provisionability(profile)["installable"])
        self.install("qwen-h3")
        self.assertTrue(self.runtime_ready())
        self.assertEqual(json.loads((self.node / "runtime_config.json").read_text(encoding="utf-8"))["backend"], "cuda13")


class ProvisioningTests(LlamaRuntimeBase):
    def test_prepare_downloads_only_missing_runtime(self) -> None:
        self.install("qwen-h3", skip_runtime=True)
        self.assertEqual(sorted(self.model_requests()), sorted(MODELS))
        self.requests.clear()
        workflow_files = sorted((self.comfy / "user" / "default" / "workflows" / "EDx").rglob("*.json"))
        mtimes = [f.stat().st_mtime_ns for f in workflow_files]
        self.install("qwen-h3")
        self.assertEqual(self.zip_requests(), ["llama-fake-win-cuda13.zip"])
        self.assertEqual(self.model_requests(), [])
        self.assertEqual([f.stat().st_mtime_ns for f in workflow_files], mtimes)
        self.assertTrue(self.runtime_ready())

    def test_h3_then_text_share_one_installation(self) -> None:
        self.install("qwen-h3")
        self.assertEqual(len(self.zip_requests()), 1)
        self.install("qwen-text")
        self.assertEqual(len(self.zip_requests()), 1, "qwen-text não pode baixar outro llama.cpp")
        self.assertEqual(len(list((self.comfy / "custom_nodes").glob("*Qwen*"))), 1)
        self.assertEqual(self.text_model_requests(), list(TEXT_MODELS), "qwen-text baixa só o 14B")
        self.assertEqual(sorted(self.model_requests()), sorted(MODELS), "o 27B/mmproj do qwen-h3 não é baixado de novo")

    def test_text_then_h3_share_one_installation(self) -> None:
        self.install("qwen-text")
        self.assertEqual(len(self.zip_requests()), 1)
        self.assertEqual(self.model_requests(), [], "qwen-text não baixa o 27B nem o mmproj")
        self.install("qwen-h3")
        self.assertEqual(len(self.zip_requests()), 1, "qwen-h3 não pode baixar outro llama.cpp")
        self.assertEqual(self.text_model_requests(), list(TEXT_MODELS), "o 14B não é baixado de novo")
        self.assertTrue(self.status("qwen-h3")["installed"] and self.status("qwen-text")["installed"])

    def test_second_install_and_repair_are_idempotent(self) -> None:
        self.install("qwen-h3")
        config = self.node / "runtime_config.json"
        stamp = config.stat().st_mtime_ns
        self.requests.clear()
        events: list[dict] = []
        self.install("qwen-h3")
        self.install("qwen-h3", force_verify=True, progress=events.append)  # repair
        self.assertEqual(self.requests, [])
        self.assertEqual(config.stat().st_mtime_ns, stamp)
        self.assertIn("RUNTIME_CACHED", [e["phase"] for e in events])

    def test_repair_reinstalls_only_the_broken_runtime(self) -> None:
        self.install("qwen-h3")
        config = json.loads((self.node / "runtime_config.json").read_text(encoding="utf-8"))
        (self.node / config["executable"]).unlink()
        self.requests.clear()
        self.install("qwen-h3", force_verify=True)
        self.assertEqual(self.model_requests(), [])
        self.assertTrue(self.runtime_ready())
        self.assertEqual(len(self.zip_requests()), 0, "o zip válido em cache deve ser reutilizado")

    def test_download_goes_through_runtime_downloader_with_progress(self) -> None:
        events: list[dict] = []
        self.install("qwen-h3", progress=events.append)
        runtime_events = [e for e in events if e.get("kind") == "runtime" and e["phase"] == "DOWNLOADING_MODEL"]
        self.assertTrue(runtime_events)
        self.assertEqual(runtime_events[-1]["component"], COMPONENT)
        self.assertIn("llama-fake-win-cuda13.zip", runtime_events[-1]["asset"])
        self.assertFalse(list(self.node.rglob("*.partial")))
        self.assertFalse(list((self.node / "runtime" / ".staging").glob("*")) if (self.node / "runtime" / ".staging").exists() else [])

    def test_download_failure_never_marks_capability_ready(self) -> None:
        self.fail_urls["llama-fake-win-cuda13.zip"] = urllib.error.URLError("sem rede")
        with self.assertRaisesRegex(ProvisioningError, "RUNTIME_INSTALL_FAILED"):
            self.install("qwen-h3")
        self.assertFalse(self.runtime_ready())
        self.assertNotIn("qwen-h3", json.loads(self.p._state_path(self.comfy).read_text(encoding="utf-8")).get("profiles", {})
                         if self.p._state_path(self.comfy).exists() else {})
        self.assertFalse(self.status("qwen-h3")["installed"])
        self.assertEqual(self.model_requests(), [], "falha do runtime não deve disparar o download dos modelos")
        self.fail_urls.clear()
        self.install("qwen-h3")  # retry converge
        self.assertTrue(self.status("qwen-h3")["installed"])

    def test_extraction_failure_never_marks_capability_ready(self) -> None:
        broken = b"not a zip file"
        self.payloads["llama-fake-win-cuda13.zip"] = broken
        asset = self.module.Asset("llama-fake-win-cuda13.zip", hashlib.sha256(broken).hexdigest(), "https://example.invalid/llama")
        self.module.PLATFORM_PACKAGES["windows"]["x64"]["cuda13"] = self.module.RuntimePackage("cuda13", "fake", (asset,), ("CUDA",), (13, 4))
        with self.assertRaisesRegex(ProvisioningError, "RUNTIME_INSTALL_FAILED"):
            self.install("qwen-h3")
        self.assertFalse((self.node / "runtime_config.json").exists())
        self.assertFalse(self.status("qwen-h3")["installed"])

    def test_archive_without_server_executable_fails(self) -> None:
        archive = make_zip({"readme.txt": b"x"})
        self.payloads["llama-fake-win-cuda13.zip"] = archive
        asset = self.module.Asset("llama-fake-win-cuda13.zip", hashlib.sha256(archive).hexdigest(), "https://example.invalid/llama")
        self.module.PLATFORM_PACKAGES["windows"]["x64"]["cuda13"] = self.module.RuntimePackage("cuda13", "fake", (asset,), ("CUDA",), (13, 4))
        with self.assertRaisesRegex(ProvisioningError, "llama-server"):
            self.install("qwen-h3")
        self.assertFalse(self.runtime_ready())

    def test_sha_mismatch_is_rejected(self) -> None:
        self.payloads["llama-fake-win-cuda13.zip"] = make_zip({"llama-server.exe": b"tampered"})
        with self.assertRaises(ProvisioningError):
            self.install("qwen-h3")
        self.assertFalse(self.runtime_ready())

    def test_cancellation_aborts_without_fallback_or_ready(self) -> None:
        cancel = threading.Event()
        inner = self.p.opener

        def cancelling_opener(request, timeout):
            if request.full_url.endswith(".zip"): cancel.set()  # usuário cancela durante o download do llama.cpp
            return inner(request, timeout)
        self.p.opener = cancelling_opener
        with self.assertRaises(ProvisioningCancelled):
            self.install("qwen-h3", cancel_event=cancel)
        self.assertFalse(self.runtime_ready())
        self.assertFalse((self.node / "runtime_config.json").exists())
        self.assertEqual(self.zip_requests(), ["llama-fake-win-cuda13.zip"], "cancelamento não pode cair em backend de fallback")
        self.assertNotIn("qwen-h3", json.loads(self.p._state_path(self.comfy).read_text(encoding="utf-8")).get("profiles", {})
                         if self.p._state_path(self.comfy).exists() else {})

    def test_external_node_is_never_written(self) -> None:
        self.node.mkdir(parents=True)
        (self.node / "node.py").write_text("# externo", encoding="utf-8")
        self.p._backend_runtime(COMPONENT, self.comfy, {})
        self.assertFalse((self.node / "runtime").exists())
        self.assertFalse((self.node / "runtime_config.json").exists())
        self.assertEqual(self.requests, [])


class OwnershipTests(LlamaRuntimeBase):
    def plan(self, target: str):
        return self.p.plan_uninstall(target, self.comfy)

    def kinds(self, plan, kind: str) -> dict[str, str]:
        return {item.ident: item.action for item in plan.items if item.kind == kind}

    def test_uninstalling_text_keeps_runtime_used_by_h3(self) -> None:
        self.install("qwen-h3"); self.install("qwen-text")
        plan = self.plan("qwen-text")
        self.assertEqual(self.kinds(plan, "component")[COMPONENT], KEEP_SHARED)
        self.assertNotIn(COMPONENT, self.kinds(plan, "runtime"))
        self.p.uninstall("qwen-text", self.comfy)
        self.assertTrue(self.runtime_ready())
        self.assertTrue(self.status("qwen-h3")["installed"])

    def test_uninstalling_h3_keeps_runtime_shared_with_text(self) -> None:
        self.install("qwen-h3"); self.install("qwen-text")
        plan = self.plan("qwen-h3")
        self.assertEqual(self.kinds(plan, "component")[COMPONENT], KEEP_SHARED)
        self.assertNotIn(COMPONENT, self.kinds(plan, "runtime"))
        self.p.uninstall("qwen-h3", self.comfy)
        self.assertTrue(self.runtime_ready())
        self.assertTrue(self.status("qwen-text")["installed"])

    def test_last_consumer_removes_owned_runtime_only(self) -> None:
        self.install("qwen-h3"); self.install("qwen-text")
        self.p.uninstall("qwen-text", self.comfy)
        plan = self.plan("qwen-h3")
        self.assertEqual(self.kinds(plan, "runtime")[COMPONENT], REMOVE)
        outside = self.comfy / "models" / "keep.txt"; outside.write_text("x", encoding="utf-8")
        result = self.p.uninstall("qwen-h3", self.comfy)
        self.assertTrue(result.ok, result.errors)
        self.assertFalse((self.node / "runtime").exists())
        self.assertFalse((self.node / "runtime_config.json").exists())
        self.assertFalse(self.runtime_ready())
        self.assertTrue(outside.exists())
        state = json.loads(self.p._state_path(self.comfy).read_text(encoding="utf-8"))
        self.assertNotIn("backend_runtime", state.get("components", {}).get(COMPONENT, {}))

    def test_unowned_manual_runtime_is_preserved(self) -> None:
        self.install("qwen-h3")
        state = json.loads(self.p._state_path(self.comfy).read_text(encoding="utf-8"))
        state["components"][COMPONENT].pop("backend_runtime")  # instalação manual / sem prova de ownership
        self.p._state_path(self.comfy).write_text(json.dumps(state), encoding="utf-8")
        plan = self.plan("qwen-h3")
        self.assertEqual(self.kinds(plan, "runtime")[COMPONENT], KEEP_UNMANAGED)
        self.p.uninstall("qwen-h3", self.comfy)
        self.assertTrue((self.node / "runtime_config.json").is_file())
        self.assertTrue(self.runtime_ready())

    def test_ownership_survives_idempotent_reinstall(self) -> None:
        self.install("qwen-h3"); self.install("qwen-h3"); self.install("qwen-text")
        state = json.loads(self.p._state_path(self.comfy).read_text(encoding="utf-8"))
        record = state["components"][COMPONENT]["backend_runtime"]
        self.assertTrue(record["owned"])
        self.assertEqual(record["tag"], self.module.RELEASE_TAG)

    def test_links_inside_runtime_are_never_followed(self) -> None:
        self.install("qwen-h3")
        with mock.patch("installer.uninstall._is_link", side_effect=lambda path: Path(path).name == "llama.dll"):
            plan = self.plan("qwen-h3")
        self.assertEqual(self.kinds(plan, "runtime")[COMPONENT], KEEP_UNMANAGED)


if __name__ == "__main__":
    unittest.main()
