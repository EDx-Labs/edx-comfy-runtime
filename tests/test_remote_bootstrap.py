from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest import mock

import install_workflow
from installer.provisioning import Provisioner, ProvisioningError
from installer.remote_bootstrap import (
    ALLOW_INSECURE_LOCAL_ENV,
    TOKEN_ENV,
    TRUSTED_ORIGINS_ENV,
    CallbackError,
    ComfyAPI,
    ComfyUIController,
    ManifestValidationError,
    ReadinessCallbackClient,
    ReadinessError,
    RemoteBootstrap,
    RemoteBootstrapError,
    RemoteManifest,
    Schema2ManifestParser,
    load_service_token,
    main,
)
from installer.runtime import runtime_python


ORIGIN = "https://compute.example.test"
INSTANCE = "inst-123"
READINESS_URL = f"{ORIGIN}/api/v1/internal/instances/{INSTANCE}/readiness"


def manifest_payload(**overrides):
    payload = {
        "schema_version": 2,
        "instance_id": INSTANCE,
        "provider_instance_id": "5000",
        "capabilities": ["fixture-capability"],
        "callback": {"readiness_url": READINESS_URL},
    }
    payload.update(overrides)
    return payload


class FakeResponse:
    def __init__(self, status=200, payload=b"{}"):
        self.status = status
        self.payload = payload

    def read(self, _limit=-1):
        return self.payload

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "bootstrap.json"
        self.environment = {TRUSTED_ORIGINS_ENV: ORIGIN}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_valid_schema_2_manifest(self):
        self.write(manifest_payload(capabilities=["fixture-capability", "FIXTURE-CAPABILITY"]))
        value = Schema2ManifestParser(self.environment).load(self.path)
        self.assertEqual(value.instance_id, INSTANCE)
        self.assertEqual(value.provider_instance_id, "5000")
        self.assertEqual(value.capabilities, ("fixture-capability",))
        self.assertEqual(value.readiness_url, READINESS_URL)
        self.assertRegex(value.digest, r"^[0-9a-f]{64}$")

    def test_missing_manifest_is_clear(self):
        with self.assertRaisesRegex(ManifestValidationError, "ausente"):
            Schema2ManifestParser(self.environment).load(self.path)

    def test_invalid_json_is_rejected(self):
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ManifestValidationError, "JSON UTF-8"):
            Schema2ManifestParser(self.environment).load(self.path)

    def test_incompatible_schema_is_rejected(self):
        self.write(manifest_payload(schema_version=1))
        with self.assertRaisesRegex(ManifestValidationError, "schema_version"):
            Schema2ManifestParser(self.environment).load(self.path)

    def test_invalid_identifiers_are_rejected(self):
        for field, value in (("instance_id", "../bad"), ("provider_instance_id", "")):
            with self.subTest(field=field):
                payload = manifest_payload(**{field: value})
                self.write(payload)
                with self.assertRaisesRegex(ManifestValidationError, field):
                    Schema2ManifestParser(self.environment).load(self.path)

    def test_empty_or_malformed_capabilities_are_rejected(self):
        for value in ([], "fixture", ["bad/value"]):
            with self.subTest(value=value):
                self.write(manifest_payload(capabilities=value))
                with self.assertRaises(ManifestValidationError):
                    Schema2ManifestParser(self.environment).load(self.path)

    def test_callback_instance_mismatch_is_rejected(self):
        self.write(manifest_payload(callback={
            "readiness_url": f"{ORIGIN}/api/v1/internal/instances/other/readiness",
        }))
        with self.assertRaisesRegex(ManifestValidationError, "instance_id"):
            Schema2ManifestParser(self.environment).load(self.path)

    def test_arbitrary_callback_origin_is_rejected(self):
        self.write(manifest_payload(callback={
            "readiness_url": f"https://attacker.invalid/api/v1/internal/instances/{INSTANCE}/readiness",
        }))
        with self.assertRaisesRegex(ManifestValidationError, "nao autorizada"):
            Schema2ManifestParser(self.environment).load(self.path)

    def test_external_http_is_rejected(self):
        origin = "http://compute.example.test"
        self.write(manifest_payload(callback={
            "readiness_url": f"{origin}/api/v1/internal/instances/{INSTANCE}/readiness",
        }))
        with self.assertRaisesRegex(ManifestValidationError, "HTTPS"):
            Schema2ManifestParser({TRUSTED_ORIGINS_ENV: origin}).load(self.path)

    def test_explicitly_trusted_loopback_http_is_allowed(self):
        origin = "http://127.0.0.1:9000"
        url = f"{origin}/api/v1/internal/instances/{INSTANCE}/readiness"
        self.write(manifest_payload(callback={"readiness_url": url}))
        value = Schema2ManifestParser({
            TRUSTED_ORIGINS_ENV: origin,
            ALLOW_INSECURE_LOCAL_ENV: "1",
        }).load(self.path)
        self.assertEqual(value.readiness_url, url)


class SecretTests(unittest.TestCase):
    def test_token_is_required(self):
        with self.assertRaisesRegex(RemoteBootstrapError, "ausente"):
            load_service_token({})

    def test_token_comes_from_environment_without_logging(self):
        token = "super-secret-value"
        self.assertEqual(load_service_token({TOKEN_ENV: token}), token)

    def test_conflicting_secret_sources_are_rejected(self):
        with self.assertRaisesRegex(RemoteBootstrapError, "apenas"):
            load_service_token({TOKEN_ENV: "one", "EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE": "two"})

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not authoritative on Windows")
    def test_secret_file_must_be_private_on_posix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("secret", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(RemoteBootstrapError, "proprietario"):
                load_service_token({"EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE": str(path)})


class CallbackTests(unittest.TestCase):
    def manifest(self):
        return RemoteManifest(INSTANCE, "5000", ("x",), READINESS_URL, "a" * 64)

    def test_success_has_exact_payload_and_bearer(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(204)

        status = ReadinessCallbackClient(opener=opener, sleeper=lambda _: None).send(self.manifest(), "secret")
        self.assertEqual(status, 204)
        request = calls[0][0]
        self.assertEqual(json.loads(request.data), {"provider_instance_id": "5000"})
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.full_url, READINESS_URL)

    def test_repeated_success_is_idempotent_from_client_perspective(self):
        calls = []
        client = ReadinessCallbackClient(
            opener=lambda request, timeout: calls.append(request) or FakeResponse(200),
            sleeper=lambda _: None,
        )
        self.assertEqual(client.send(self.manifest(), "secret"), 200)
        self.assertEqual(client.send(self.manifest(), "secret"), 200)
        self.assertEqual(len(calls), 2)

    def test_contract_and_auth_errors_are_not_retried(self):
        for status in (302, 400, 401, 403, 404, 409):
            calls = []

            def opener(request, timeout, status=status):
                calls.append(1)
                raise urllib.error.HTTPError(request.full_url, status, "rejected", {}, None)

            with self.subTest(status=status):
                with self.assertRaises(CallbackError) as caught:
                    ReadinessCallbackClient(opener=opener, sleeper=lambda _: None).send(self.manifest(), "secret")
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("secret", str(caught.exception))

    def test_5xx_retries_are_limited_with_backoff(self):
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
            return FakeResponse(200)

        status = ReadinessCallbackClient(
            opener=opener, sleeper=sleeps.append, attempts=4,
        ).send(self.manifest(), "secret")
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_timeout_retries_only_configured_number(self):
        calls = []

        def opener(request, timeout):
            calls.append(1)
            raise TimeoutError()

        with self.assertRaisesRegex(CallbackError, "indisponivel"):
            ReadinessCallbackClient(opener=opener, sleeper=lambda _: None, attempts=3).send(self.manifest(), "secret")
        self.assertEqual(len(calls), 3)


class FakeRuntimeInstaller:
    def __init__(self):
        self.calls = []

    def ensure(self, comfy, backend=None):
        comfy = Path(comfy)
        self.calls.append((comfy, backend))
        (comfy / "models").mkdir(parents=True, exist_ok=True)
        (comfy / "main.py").write_text("", encoding="utf-8")
        python = runtime_python(comfy)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"python")
        return {"installed": True, "backend": backend}


class FakeProvisioner:
    def __init__(self, *, install_failure=None, missing_model=False, installed=True):
        self.install_failure = install_failure
        self.missing_model = missing_model
        self.installed = installed
        self.install_calls = []
        self.components = {
            "fixture-node": {"name": "Fixture-Node", "source": "fixtures/Fixture-Node"},
        }
        self.models = {
            "fixture-model": {"filename": "fixture.bin", "directory": "checkpoints", "url": "https://example.invalid/model"},
        }
        self.workflow_sets = {"fixture-workflows": {"workflows": ["fixtures/workflow.json"]}}

    def resolve_profile(self, capability):
        if capability != "fixture-capability":
            raise ProvisioningError(f"capability inexistente: {capability}")
        return "fixture-profile"

    def plan(self, profile, comfy):
        return SimpleNamespace(profile_id=profile, comfy=Path(comfy))

    def install(self, plan):
        self.install_calls.append(plan.profile_id)
        if self.install_failure:
            raise self.install_failure
        comfy = plan.comfy
        node = comfy / "custom_nodes" / "Fixture-Node"
        node.mkdir(parents=True, exist_ok=True)
        (node / "__init__.py").write_text("", encoding="utf-8")
        model = self._model_path(comfy, self.models["fixture-model"])
        model.parent.mkdir(parents=True, exist_ok=True)
        if not self.missing_model:
            model.write_bytes(b"model")
        workflow = comfy / "user" / "default" / "workflows" / "EDx" / "fixture-workflows" / "workflow.json"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text("{}", encoding="utf-8")

    def profile_installation(self, profile, comfy):
        return {"installed": self.installed, "missing": [] if self.installed else ["dependency:missing"]}

    def profile_requirements(self, profile):
        return {
            "models": ["fixture-model"],
            "components": ["fixture-node"],
            "workflow_sets": ["fixture-workflows"],
        }

    def _model_path(self, comfy, model):
        return Path(comfy) / "models" / model["directory"] / model["filename"]

    def _valid_model(self, path, model):
        return Path(path).is_file() and Path(path).stat().st_size > 0

    def backend_runtime_status(self, ident, comfy):
        return {"ready": True}


class FakeAPI:
    def __init__(self, *, object_info=None):
        self.object_info = object_info if object_info is not None else {
            "FixtureNode": {"python_module": "custom_nodes.Fixture-Node"},
        }

    def get_json(self, url):
        if url.endswith("/system_stats"):
            return {"system": {"os": "test"}}
        if url.endswith("/object_info"):
            return self.object_info
        raise AssertionError(url)


class FakeController:
    def __init__(self, *, error=None, object_info=None):
        self.error = error
        self.api = FakeAPI(object_info=object_info)
        self.calls = []

    def ensure_running(self, comfy, base_url, **kwargs):
        self.calls.append((Path(comfy), base_url, kwargs))
        if self.error:
            raise self.error
        return {"reused": False, "pid": 123}


class FakeCallback:
    def __init__(self, *, error=None, status=204):
        self.error = error
        self.status = status
        self.calls = []

    def send(self, manifest, token):
        self.calls.append((manifest, token))
        if self.error:
            raise self.error
        return self.status


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / ".edx" / "bootstrap.json"
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps(manifest_payload()), encoding="utf-8")
        self.comfy = self.root / "ComfyUI"
        self.environment = {TRUSTED_ORIGINS_ENV: ORIGIN, TOKEN_ENV: "secret-token"}

    def tearDown(self):
        self.tmp.cleanup()

    def bootstrap(self, **overrides):
        values = {
            "environment": self.environment,
            "runtime_installer": FakeRuntimeInstaller(),
            "provisioner": FakeProvisioner(),
            "controller": FakeController(),
            "callback_client": FakeCallback(),
        }
        values.update(overrides)
        return RemoteBootstrap(**values), values

    def test_valid_bootstrap_provisions_checks_and_callbacks(self):
        bootstrap, values = self.bootstrap()
        result = bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(result["profiles"], ["fixture-profile"])
        self.assertFalse(result["inference_validated"])
        self.assertEqual(values["provisioner"].install_calls, ["fixture-profile"])
        self.assertEqual(len(values["callback_client"].calls), 1)
        state = json.loads((self.manifest.parent / "bootstrap-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], "READINESS_CONFIRMED")
        self.assertNotIn("secret-token", json.dumps(state))

    def test_restart_is_idempotent_and_callback_can_repeat(self):
        bootstrap, values = self.bootstrap()
        first = bootstrap.run(self.manifest, self.comfy)
        second = bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(first["profiles"], second["profiles"])
        self.assertEqual(len(values["callback_client"].calls), 2)
        self.assertEqual(len(values["runtime_installer"].calls), 2)

    def test_unknown_profile_fails_before_callback(self):
        self.manifest.write_text(json.dumps(manifest_payload(capabilities=["not-real"])), encoding="utf-8")
        bootstrap, values = self.bootstrap()
        with self.assertRaisesRegex(ProvisioningError, "inexistente"):
            bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(values["callback_client"].calls, [])

    def test_required_dependency_missing_blocks_callback(self):
        bootstrap, values = self.bootstrap(provisioner=FakeProvisioner(installed=False))
        with self.assertRaisesRegex(ReadinessError, "assets obrigatorios"):
            bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(values["callback_client"].calls, [])

    def test_download_or_install_failure_blocks_callback(self):
        error = ProvisioningError("download falhou")
        bootstrap, values = self.bootstrap(provisioner=FakeProvisioner(install_failure=error))
        with self.assertRaisesRegex(ProvisioningError, "download falhou"):
            bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(values["callback_client"].calls, [])

    def test_missing_model_after_install_blocks_callback(self):
        bootstrap, values = self.bootstrap(provisioner=FakeProvisioner(missing_model=True))
        with self.assertRaisesRegex(ReadinessError, "fixture-model"):
            bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(values["callback_client"].calls, [])

    def test_comfy_unavailable_blocks_callback(self):
        controller = FakeController(error=ReadinessError("ComfyUI indisponivel"))
        bootstrap, values = self.bootstrap(controller=controller)
        with self.assertRaisesRegex(ReadinessError, "indisponivel"):
            bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(values["callback_client"].calls, [])

    def test_http_api_without_required_component_blocks_callback(self):
        controller = FakeController(object_info={"Core": {"python_module": "nodes"}})
        bootstrap, values = self.bootstrap(controller=controller)
        with self.assertRaisesRegex(ReadinessError, "nao carregados"):
            bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(values["callback_client"].calls, [])

    def test_missing_token_fails_before_provisioning(self):
        environment = {TRUSTED_ORIGINS_ENV: ORIGIN}
        bootstrap, values = self.bootstrap(environment=environment)
        with self.assertRaisesRegex(RemoteBootstrapError, "token"):
            bootstrap.run(self.manifest, self.comfy)
        self.assertEqual(values["runtime_installer"].calls, [])
        self.assertEqual(values["callback_client"].calls, [])

    def test_callback_failure_leaves_diagnostic_state_without_secret(self):
        callback = FakeCallback(error=CallbackError("HTTP 401", status=401))
        bootstrap, _values = self.bootstrap(callback_client=callback)
        with self.assertRaises(CallbackError):
            bootstrap.run(self.manifest, self.comfy)
        state_text = (self.manifest.parent / "bootstrap-state.json").read_text(encoding="utf-8")
        self.assertIn('"phase": "FAILED"', state_text)
        self.assertNotIn("secret-token", state_text)


class ApiTests(unittest.TestCase):
    def test_http_200_without_valid_json_is_not_ready(self):
        api = ComfyAPI(opener=lambda request, timeout: FakeResponse(200, b"not-json"))
        with self.assertRaisesRegex(ReadinessError, "JSON"):
            api.get_json("http://127.0.0.1:8189/system_stats")

    def test_open_port_with_wrong_http_status_is_not_ready(self):
        api = ComfyAPI(opener=lambda request, timeout: FakeResponse(404, b"{}"))
        with self.assertRaisesRegex(ReadinessError, "status"):
            api.get_json("http://127.0.0.1:8189/system_stats")

    def test_existing_healthy_comfyui_is_reused_without_new_process(self):
        api = mock.Mock()
        api.get_json.return_value = {"system": {"os": "test"}}
        controller = ComfyUIController(
            api=api,
            process_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate process")),
        )
        result = controller.ensure_running(
            Path("unused"), "http://127.0.0.1:8189",
            host="127.0.0.1", port=8189, timeout=1,
        )
        self.assertTrue(result["reused"])

    def test_tracked_live_process_is_waited_for_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            state = comfy / "user" / "default" / "EDx" / "remote-comfyui-process.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"pid": 321}), encoding="utf-8")

            class StartingAPI:
                def __init__(self):
                    self.calls = 0

                def get_json(self, url):
                    self.calls += 1
                    return {} if self.calls < 3 else {"system": {"os": "test"}}

            controller = ComfyUIController(
                api=StartingAPI(), sleeper=lambda _: None,
                process_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate process")),
            )
            with mock.patch("installer.remote_bootstrap.process_state", return_value="alive"):
                result = controller.ensure_running(
                    comfy, "http://127.0.0.1:8189",
                    host="127.0.0.1", port=8189, timeout=1,
                )
            self.assertTrue(result["reused"])
            self.assertEqual(result["pid"], 321)

    def test_profile_backend_and_minimum_vram_are_enforced(self):
        provisioner = mock.Mock()
        provisioner.profile_requirements.return_value = {
            "backend_contract_declared": True,
            "supported_backends": ["cuda"],
            "min_vram_mb": 8192,
        }
        RemoteBootstrap._verify_hardware(
            provisioner, ["profile"], {"devices": [{"vram_total": 10 * 1024**3}]}, "nvidia",
        )
        with self.assertRaisesRegex(ReadinessError, "VRAM"):
            RemoteBootstrap._verify_hardware(
                provisioner, ["profile"], {"devices": [{"vram_total": 4 * 1024**3}]}, "nvidia",
            )
        with self.assertRaisesRegex(ReadinessError, "backend"):
            RemoteBootstrap._verify_hardware(
                provisioner, ["profile"], {"devices": [{"vram_total": 10 * 1024**3}]}, "rocm",
            )


class DesktopRegressionTests(unittest.TestCase):
    def test_local_list_needs_no_manifest_or_remote_token_and_sends_no_callback(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sys, "argv", ["install_workflow.py", "--list"],
        ), mock.patch.object(
            ReadinessCallbackClient, "send", side_effect=AssertionError("callback local indevido"),
        ), contextlib.redirect_stdout(output):
            install_workflow.main()
        self.assertIn("minimax-h3-fast", output.getvalue())

    def test_previous_profile_and_comfyui_arguments_still_work(self):
        with tempfile.TemporaryDirectory() as directory:
            comfy = Path(directory) / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            output = io.StringIO()
            with mock.patch.object(sys, "argv", [
                "install_workflow.py", "qwen-asr", "--comfyui", str(comfy), "--dry-run",
            ]), contextlib.redirect_stdout(output):
                install_workflow.main()
            self.assertIn("PROFILE", output.getvalue())
            self.assertFalse((comfy / "user").exists())

    def test_existing_profile_resolution_contract_is_unchanged(self):
        provisioner = Provisioner()
        self.assertEqual(provisioner.resolve_profile("minimax"), "minimax-h3-fast")
        self.assertEqual(provisioner.resolve_profile("krea2"), "krea2")
        self.assertEqual(provisioner.resolve_profile("seedvr2"), "seedvr2")

    def test_runtime_python_keeps_windows_and_linux_layouts(self):
        with mock.patch("installer.runtime.os.name", "nt"), mock.patch(
            "installer.runtime.Path", side_effect=PureWindowsPath,
        ):
            root = PureWindowsPath("ComfyUI")
            self.assertEqual(runtime_python(root), root / ".venv" / "Scripts" / "python.exe")
        with mock.patch("installer.runtime.os.name", "posix"), mock.patch(
            "installer.runtime.Path", side_effect=PurePosixPath,
        ):
            root = PurePosixPath("ComfyUI")
            self.assertEqual(runtime_python(root), root / ".venv" / "bin" / "python")

    def test_remote_cli_help_does_not_require_configuration(self):
        result = subprocess.run(
            [sys.executable, "-m", "installer.remote_bootstrap", "--help"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--manifest", result.stdout)

    def test_remote_cli_error_does_not_echo_token(self):
        secret = "do-not-print-this-token"
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "definitely-missing.json"
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {TOKEN_ENV: secret}, clear=True), contextlib.redirect_stderr(stderr):
                exit_code = main(["--manifest", str(missing)])
            self.assertEqual(exit_code, 2)
            self.assertNotIn(secret, stderr.getvalue())
            self.assertFalse((missing.parent / "bootstrap-state.json").exists())


if __name__ == "__main__":
    unittest.main()
