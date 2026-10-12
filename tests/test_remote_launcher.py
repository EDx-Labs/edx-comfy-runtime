"""RT2.1: o launcher do ENTRYPOINT materializa o bootstrap e executa o tini e o supervisor com ambiente sanitizado.

Offline e sem processo real do tini: ``execve`` e injetado. A prova com processos de verdade (ENTRYPOINT da imagem, /proc/*/environ, SIGTERM,
SIGKILL, reinicio) esta em ``tests/linux/rt2`` e roda em Linux/Docker.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import installer.remote_launcher as launcher  # noqa: E402
from installer.remote_bootstrap import TOKEN_ENV, TOKEN_FILE_ENV, TRUSTED_ORIGINS_ENV  # noqa: E402
from installer.remote_env import (  # noqa: E402
    ENV_CAPABILITIES,
    ENV_INSTANCE_ID,
    ENV_READINESS_TOKEN,
    ENV_READINESS_URL,
    ENV_SCHEMA,
    PROVIDER_ID_ENV,
    PROVIDER_SECRET_ENV,
    delivery_requested,
)
from test_remote_env import ADMIN_TOKEN, INSTANCE, ORIGIN, TOKEN, URL, VAST_KEY, delivered  # noqa: E402

JUPYTER = "jupyter-token-from-the-provider-0123456789"
SECRET_VALUES = (TOKEN, TOKEN.split(".")[4], ADMIN_TOKEN, VAST_KEY, JUPYTER)
SECRET_NAMES = (ENV_READINESS_TOKEN, TOKEN_ENV, PROVIDER_SECRET_ENV, "JUPYTER_TOKEN", ENV_SCHEMA, ENV_INSTANCE_ID, ENV_CAPABILITIES, ENV_READINESS_URL)


class FakeExec:
    """Stands in for os.execve: records the call and the files that existed at that instant (the publication must come BEFORE the exec)."""

    def __init__(self, manifest: Path | None = None, token: Path | None = None):
        self.calls, self.files_at_exec = [], {}
        self.manifest, self.token = manifest, token

    def __call__(self, path, argv, environment):
        self.calls.append((path, list(argv), dict(environment)))
        for name, target in (("manifest", self.manifest), ("token", self.token)):
            if target is not None:
                self.files_at_exec[name] = target.read_text(encoding="utf-8") if target.exists() else None


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "workspace" / ".edx" / "bootstrap.json"
        self.token = self.root / "run" / "secrets" / "edx-compute-manager-token"

    def tearDown(self):
        self.tmp.cleanup()

    def environment(self, **over):
        base = delivered(**{TOKEN_FILE_ENV: str(self.token), TOKEN_ENV: ADMIN_TOKEN, PROVIDER_SECRET_ENV: VAST_KEY, "JUPYTER_TOKEN": JUPYTER,
                            "PATH": "/usr/local/bin:/usr/bin", "HF_HOME": "/workspace/.cache/hf"})
        base.update(over)
        return {k: v for k, v in base.items() if v is not None}

    def launch(self, environment, forwarded=None, execve=None):
        execve = execve or FakeExec(self.manifest, self.token)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = launcher.main(forwarded if forwarded is not None else ["--manifest", str(self.manifest)], environment=environment, execve=execve)
        return code, execve, out.getvalue(), err.getvalue()

    def files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())


class DeliveryTests(Workspace):
    def test_the_files_are_published_before_the_definitive_processes_are_executed(self):
        code, run, out, err = self.launch(self.environment())
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(json.loads(run.files_at_exec["manifest"])["instance_id"], INSTANCE)
        self.assertEqual(run.files_at_exec["token"], TOKEN)
        self.assertEqual(code, 0)

    def test_tini_and_the_supervisor_are_executed_with_a_sanitised_environment(self):
        _, run, *_ = self.launch(self.environment())
        path, argv, child = run.calls[0]
        self.assertEqual(path, "/usr/bin/tini")
        self.assertEqual(argv, ["/usr/bin/tini", "--", "python", "-m", "installer.remote_supervisor", "--manifest", str(self.manifest)])
        blob = json.dumps(child)
        for secret in SECRET_VALUES:
            self.assertNotIn(secret, blob)
        for name in SECRET_NAMES:
            self.assertNotIn(name, child)
        # what the supervisor still needs, and none of it secret
        self.assertEqual(child[TOKEN_FILE_ENV], str(self.token))
        self.assertEqual(child[TRUSTED_ORIGINS_ENV], ORIGIN)
        self.assertEqual(child["PATH"], "/usr/local/bin:/usr/bin")
        self.assertEqual(child["HF_HOME"], "/workspace/.cache/hf")

    def test_the_command_line_of_the_definitive_processes_carries_no_secret(self):
        _, run, *_ = self.launch(self.environment())
        for argument in run.calls[0][1]:
            for secret in SECRET_VALUES:
                self.assertNotIn(secret, argument)

    def test_the_supervisor_that_starts_afterwards_sees_no_delivery_and_reads_the_credential_from_the_restricted_file(self):
        from installer.remote_bootstrap import load_service_token

        _, run, *_ = self.launch(self.environment())
        child = run.calls[0][2]
        self.assertFalse(delivery_requested(child))  # the supervisor takes the ordinary file-based path
        self.assertEqual(load_service_token(child), TOKEN)  # ...and the credential comes from the 0600 file, not from the environment

    def test_the_real_process_environment_is_scrubbed_too(self):
        injected = self.environment()
        with mock.patch.dict(os.environ, injected, clear=True):
            out, err = io.StringIO(), io.StringIO()
            run = FakeExec()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(launcher.signal, "signal"):
                code = launcher.main(["--manifest", str(self.manifest)], execve=run)
            remaining = dict(os.environ)
        self.assertEqual(code, 0)
        for name in SECRET_NAMES:
            self.assertNotIn(name, remaining)
        self.assertNotIn(TOKEN, json.dumps(remaining))

    def test_arguments_given_to_the_entrypoint_reach_the_supervisor_and_the_manifest_path_is_the_one_it_will_read(self):
        other = self.root / "elsewhere" / "bootstrap.json"
        _, run, *_ = self.launch(self.environment(), ["--manifest", str(other), "--wait-timeout", "30", "--comfyui", "/x"])
        self.assertEqual(run.calls[0][1][-6:], ["--manifest", str(other), "--wait-timeout", "30", "--comfyui", "/x"])
        self.assertTrue(other.is_file())
        self.assertFalse(self.manifest.exists())

    def test_the_default_manifest_path_is_the_supervisors_default(self):
        from installer.remote_bootstrap import DEFAULT_MANIFEST

        self.assertEqual(launcher.manifest_path([]), DEFAULT_MANIFEST)
        self.assertEqual(launcher.manifest_path(["--wait-timeout", "5"]), DEFAULT_MANIFEST)
        self.assertEqual(launcher.manifest_path(["--manifest=/a/b.json"]), Path("/a/b.json"))

    def test_the_permissions_and_atomic_publication_of_the_materialiser_are_unchanged(self):
        self.launch(self.environment())
        if os.name != "nt":
            self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p for p in self.files() if p.endswith(".tmp")], [])
        self.assertNotIn(TOKEN, self.manifest.read_text(encoding="utf-8"))


class WithoutDeliveryTests(Workspace):
    def test_without_edx_bootstrap_variables_the_environment_reaches_the_supervisor_exactly_as_it_came(self):
        environment = {"PATH": "/bin", TOKEN_ENV: "legacy-direct-token-0123456789", TRUSTED_ORIGINS_ENV: ORIGIN, "COMFYUI_PATH": "/workspace/ComfyUI"}
        before = dict(environment)
        code, run, out, err = self.launch(environment, [])
        self.assertEqual(code, 0)
        self.assertEqual(run.calls[0][2], before)  # nothing added, nothing removed: the legacy file/env credential keeps working
        self.assertEqual(run.calls[0][1], ["/usr/bin/tini", "--", "python", "-m", "installer.remote_supervisor"])
        self.assertEqual(self.files(), [])
        self.assertEqual(out + err, "")


class RefusedDeliveryTests(Workspace):
    def test_an_invalid_delivery_never_reaches_the_supervisor_and_writes_nothing(self):
        cases = {
            "missing_token": {ENV_READINESS_TOKEN: None}, "missing_capabilities": {ENV_CAPABILITIES: None}, "missing_instance": {ENV_INSTANCE_ID: None},
            "missing_url": {ENV_READINESS_URL: None}, "missing_provider_id": {PROVIDER_ID_ENV: None}, "schema_3": {ENV_SCHEMA: "3"},
            "empty_capability": {ENV_CAPABILITIES: "a,,b"}, "token_with_space": {ENV_READINESS_TOKEN: TOKEN + " x"},
            "url_http": {ENV_READINESS_URL: URL.replace("https://", "http://"), TRUSTED_ORIGINS_ENV: ORIGIN.replace("https://", "http://")},
            "other_instance": {ENV_READINESS_URL: f"{ORIGIN}/api/v1/internal/instances/22222222-2222-4222-8222-222222222222/readiness"},
            "origin_not_allowed": {TRUSTED_ORIGINS_ENV: "https://elsewhere.example"}, "no_allow_list": {TRUSTED_ORIGINS_ENV: None},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                code, run, out, err = self.launch(self.environment(**change))
                self.assertEqual(code, 2)
                self.assertEqual(run.calls, [], "the supervisor must never start after a refused delivery")
                self.assertEqual(self.files(), [], "a refused delivery must not publish any file")
                for secret in SECRET_VALUES:
                    self.assertNotIn(secret, out + err)
                self.assertIn("launcher remoto falhou", err)

    def test_every_required_field_is_validated_before_any_file_is_published(self):
        order = []
        real_stage = launcher.prepare_from_environment.__globals__["_stage"]

        def spy(path, data):
            order.append(("stage", path.name))
            return real_stage(path, data)

        with mock.patch.dict(launcher.prepare_from_environment.__globals__, {"_stage": spy}):
            self.launch(self.environment(**{ENV_CAPABILITIES: None}))
        self.assertEqual(order, [], "nothing may even be staged when a required field is missing")

    def test_a_failure_to_execute_tini_is_reported_without_secrets(self):
        def broken(path, argv, environment):
            raise FileNotFoundError(2, "No such file", path)

        code, run, out, err = self.launch(self.environment(), execve=broken)
        self.assertEqual(code, 2)
        self.assertIn("/usr/bin/tini", err)
        for secret in SECRET_VALUES:
            self.assertNotIn(secret, out + err)


class SignalTests(Workspace):
    def test_a_sigterm_while_materialising_exits_cleanly_and_never_executes_the_supervisor(self):
        captured = {}

        def fake_signal(number, handler):
            captured[number] = handler

        def interrupted(environment, manifest):
            captured[signal.SIGTERM](signal.SIGTERM, None)  # the signal arrives in the middle of the work

        run = FakeExec()
        err = io.StringIO()
        with mock.patch.object(launcher.signal, "signal", fake_signal), mock.patch.object(launcher, "prepare_from_environment", interrupted), \
                contextlib.redirect_stderr(err):
            code = self._main_with_os_environ(run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])
        self.assertIn(signal.SIGTERM, captured)
        self.assertIn(signal.SIGINT, captured)

    def _main_with_os_environ(self, run):
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            return launcher.main([], execve=run)

    def test_the_materialiser_removes_its_temporaries_when_interrupted(self):
        from installer.remote_env import materialize

        def interrupted(source, destination):
            raise SystemExit(0)  # what the SIGTERM handler raises

        with mock.patch("installer.remote_env.os.replace", interrupted):
            with self.assertRaises(SystemExit):
                materialize(self.environment(), self.manifest, self.token)
        self.assertEqual([p for p in self.files() if p.endswith(".tmp")], [])


class ImageTests(unittest.TestCase):
    def dockerfile(self):
        return (Path(__file__).resolve().parent.parent / "deploy" / "vast" / "Dockerfile").read_text(encoding="utf-8")

    def test_the_entrypoint_is_the_launcher_and_tini_remains_the_final_pid_1(self):
        text = self.dockerfile()
        entrypoint = re.search(r"^ENTRYPOINT (\[.*\])\s*$", text, re.M)
        self.assertIsNotNone(entrypoint)
        self.assertEqual(json.loads(entrypoint.group(1)), ["python", "-m", "installer.remote_launcher"])
        self.assertIn("tini", text)  # still installed: the launcher executes it
        self.assertEqual(launcher.TINI, "/usr/bin/tini")
        self.assertEqual(launcher.supervisor_argv([]), ["/usr/bin/tini", "--", "python", "-m", "installer.remote_supervisor"])

    def test_no_secret_is_baked_into_the_image_definition(self):
        text = self.dockerfile()
        for secret in SECRET_VALUES:
            self.assertNotIn(secret, text)
        self.assertNotRegex(text, r"(?im)^ENV\s+EDX_BOOTSTRAP_READINESS_TOKEN")
        self.assertNotRegex(text, r"(?im)^ENV\s+EDX_COMPUTE_MANAGER_SERVICE_TOKEN=")  # only the FILE path is declared


if __name__ == "__main__":
    unittest.main()
