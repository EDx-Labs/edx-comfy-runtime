"""RT1: bootstrap remoto por variaveis de ambiente da instancia.

Offline, sem GPU, sem rede, sem instalar nada: o ComfyUI, o provisionador e o callback sao os fakes de ``test_remote_bootstrap``. O que
so uma instancia real prova (o provedor injetar ``CONTAINER_ID``, o ambiente por instancia chegar ao entrypoint) esta em
``docs/remote-bootstrap.md``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import installer.remote_env as remote_env  # noqa: E402
from installer.provisioning import ProvisioningError  # noqa: E402
from installer.remote_bootstrap import (  # noqa: E402
    TOKEN_ENV,
    TOKEN_FILE_ENV,
    TRUSTED_ORIGINS_ENV,
    ManifestValidationError,
    ReadinessCallbackClient,
    RemoteBootstrap,
    RemoteBootstrapError,
    Schema2ManifestParser,
    load_service_token,
)
from installer.remote_env import (  # noqa: E402
    ENV_CAPABILITIES,
    ENV_INSTANCE_ID,
    ENV_READINESS_TOKEN,
    ENV_READINESS_URL,
    ENV_SCHEMA,
    PROVIDER_ID_ENV,
    PROVIDER_SECRET_ENV,
    EnvDeliveryError,
    delivery_requested,
    materialize,
    prepare_from_environment,
    private_environment,
    scrub_process_environment,
)
from test_remote_bootstrap import FakeAPI, FakeController, FakeProvisioner, FakeResponse, FakeRuntimeInstaller  # noqa: E402

ORIGIN = "https://compute.example.test"
INSTANCE = "11111111-1111-4111-8111-111111111111"
TOKEN = "edxrt1." + INSTANCE + ".1760000000.k1." + "A" * 43
ADMIN_TOKEN = "dev-administrative-token-0123456789"
VAST_KEY = "vast-container-api-key-0123456789"
URL = f"{ORIGIN}/api/v1/internal/instances/{INSTANCE}/readiness"
POSIX = os.name != "nt"


def delivered(**overrides):
    environment = {
        ENV_SCHEMA: "2",
        ENV_INSTANCE_ID: INSTANCE,
        ENV_CAPABILITIES: "fixture-capability",
        ENV_READINESS_URL: URL,
        TRUSTED_ORIGINS_ENV: ORIGIN,
        ENV_READINESS_TOKEN: TOKEN,
        PROVIDER_ID_ENV: "5000",
    }
    environment.update(overrides)
    return {k: v for k, v in environment.items() if v is not None}


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "workspace" / ".edx" / "bootstrap.json"
        self.token = self.root / "run" / "secrets" / "edx-compute-manager-token"

    def tearDown(self):
        self.tmp.cleanup()

    def leftovers(self):
        return sorted(p.name for d in (self.manifest.parent, self.token.parent) if d.exists() for p in d.iterdir() if p.name.endswith(".tmp"))

    def files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())


class ManifestGenerationTests(Workspace):
    def test_the_environment_becomes_the_schema_2_manifest_the_existing_parser_accepts(self):
        result = materialize(delivered(), self.manifest, self.token)
        written = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(written, {
            "schema_version": 2, "instance_id": INSTANCE, "provider_instance_id": "5000",
            "capabilities": ["fixture-capability"], "callback": {"readiness_url": URL},
        })
        parsed = Schema2ManifestParser({TRUSTED_ORIGINS_ENV: ORIGIN}).load(self.manifest)
        self.assertEqual((parsed.instance_id, parsed.provider_instance_id, parsed.capabilities), (INSTANCE, "5000", ("fixture-capability",)))
        self.assertEqual((result.instance_id, result.provider_instance_id), (INSTANCE, "5000"))

    def test_the_credential_goes_to_a_separate_file_and_never_into_the_manifest(self):
        materialize(delivered(), self.manifest, self.token)
        self.assertEqual(self.token.read_text(encoding="utf-8"), TOKEN)
        self.assertNotIn(TOKEN, self.manifest.read_text(encoding="utf-8"))
        self.assertNotIn("token", self.manifest.read_text(encoding="utf-8").lower())
        self.assertEqual(load_service_token({TOKEN_FILE_ENV: str(self.token)}), TOKEN)

    def test_several_capabilities_keep_their_order_and_the_vast_label_form_is_normalised(self):
        materialize(delivered(**{ENV_CAPABILITIES: "krea2, minimax", PROVIDER_ID_ENV: "C.5000"}), self.manifest, self.token)
        written = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(written["capabilities"], ["krea2", "minimax"])
        self.assertEqual(written["provider_instance_id"], "5000")

    def test_provider_agnostic_identifier_is_preserved_for_the_simulated_host(self):
        fake_id = "fake-" + "a" * 32
        materialize(delivered(**{PROVIDER_ID_ENV: fake_id}), self.manifest, self.token)
        written = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(written["provider_instance_id"], fake_id)

    def test_missing_required_variables_are_named_and_nothing_is_written(self):
        for name in (ENV_SCHEMA, ENV_INSTANCE_ID, ENV_CAPABILITIES, ENV_READINESS_URL, ENV_READINESS_TOKEN):
            with self.subTest(name=name):
                with self.assertRaisesRegex(EnvDeliveryError, name):
                    materialize(delivered(**{name: None}), self.manifest, self.token)
                self.assertEqual(self.files(), [])

    def test_empty_values_count_as_missing(self):
        for name in (ENV_CAPABILITIES, ENV_INSTANCE_ID, ENV_READINESS_TOKEN):
            with self.subTest(name=name):
                with self.assertRaisesRegex(EnvDeliveryError, name):
                    materialize(delivered(**{name: "   "}), self.manifest, self.token)
        self.assertEqual(self.files(), [])

    def test_the_provider_instance_id_is_required_and_validated(self):
        with self.assertRaisesRegex(EnvDeliveryError, PROVIDER_ID_ENV):
            materialize(delivered(**{PROVIDER_ID_ENV: None}), self.manifest, self.token)
        for bad in ("C.", "5000; rm -rf /", "../5000", "5000 5001", "x" * 129):
            with self.subTest(bad=bad), self.assertRaisesRegex(EnvDeliveryError, PROVIDER_ID_ENV):
                materialize(delivered(**{PROVIDER_ID_ENV: bad}), self.manifest, self.token)
        self.assertEqual(self.files(), [])

    def test_an_incompatible_schema_version_is_refused(self):
        for version in ("1", "3", "two", ""):
            with self.subTest(version=version), self.assertRaises(EnvDeliveryError):
                materialize(delivered(**{ENV_SCHEMA: version}), self.manifest, self.token)
        self.assertEqual(self.files(), [])

    def test_an_invalid_manifest_is_refused_by_the_real_parser_and_leaves_no_file(self):
        cases = {
            "url_http": {ENV_READINESS_URL: URL.replace("https://", "http://"), TRUSTED_ORIGINS_ENV: ORIGIN.replace("https://", "http://")},
            "url_with_query": {ENV_READINESS_URL: URL + "?token=x"},
            "url_other_instance": {ENV_READINESS_URL: f"{ORIGIN}/api/v1/internal/instances/22222222-2222-4222-8222-222222222222/readiness"},
            "origin_not_allowed": {TRUSTED_ORIGINS_ENV: "https://elsewhere.example"},
            "no_allow_list": {TRUSTED_ORIGINS_ENV: None},
            "bad_instance_id": {ENV_INSTANCE_ID: "../x"},
            "bad_capability": {ENV_CAPABILITIES: "good,bad/value"},
            "empty_capability_item": {ENV_CAPABILITIES: "good,,other"},
            "only_commas": {ENV_CAPABILITIES: ",,"},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(RemoteBootstrapError) as raised:
                    materialize(delivered(**change), self.manifest, self.token)
                self.assertNotIn(TOKEN, str(raised.exception))  # errors name variables and fields, never values
                self.assertEqual(self.files(), [], "an invalid manifest must leave nothing behind")

    def test_a_credential_with_whitespace_or_control_characters_is_refused_without_echoing_it(self):
        for bad in (TOKEN + " extra", "tok\nen-0123456789", "tok\x00en-0123456789", "x" * 5000):
            with self.subTest(bad=bad[:12]), self.assertRaises(EnvDeliveryError) as raised:
                materialize(delivered(**{ENV_READINESS_TOKEN: bad}), self.manifest, self.token)
            self.assertNotIn(bad.strip(), str(raised.exception))
        self.assertEqual(self.files(), [])

    def test_delivery_is_opt_in(self):
        self.assertFalse(delivery_requested({}))
        self.assertFalse(delivery_requested({TOKEN_ENV: "x", TOKEN_FILE_ENV: "/x", "PATH": "/bin"}))
        self.assertTrue(delivery_requested({ENV_INSTANCE_ID: "x"}))
        self.assertTrue(delivery_requested({ENV_READINESS_TOKEN: "x"}))


class PrivateFileTests(Workspace):
    @unittest.skipIf(not POSIX, "bits de permissao POSIX nao sao autoritativos no Windows")
    def test_files_are_created_with_mode_0600_and_new_directories_with_0700(self):
        materialize(delivered(), self.manifest, self.token)
        for path in (self.manifest, self.token):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)
        for directory in (self.manifest.parent, self.token.parent):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory.name)

    @unittest.skipIf(not POSIX, "bits de permissao POSIX nao sao autoritativos no Windows")
    def test_a_restrictive_umask_cannot_loosen_it_and_a_permissive_one_does_not_either(self):
        previous = os.umask(0o000)
        try:
            materialize(delivered(), self.manifest, self.token)
        finally:
            os.umask(previous)
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)

    def test_0600_is_requested_explicitly_at_creation_on_every_platform(self):
        """No Windows os.stat nao distingue 0600; aqui se prova o PEDIDO: o temporario nasce de mkstemp (0600) e recebe chmod 0600 explicito
        ANTES de qualquer dado ser escrito, para o manifesto e para a credencial."""
        calls = []
        real_chmod, real_mkstemp = os.chmod, remote_env.tempfile.mkstemp

        def chmod(path, mode, *args, **kwargs):
            calls.append(("chmod", Path(path).name, mode, Path(path).stat().st_size))
            return real_chmod(path, mode, *args, **kwargs)

        def mkstemp(*args, **kwargs):
            calls.append(("mkstemp", kwargs.get("prefix")))
            return real_mkstemp(*args, **kwargs)

        with mock.patch.object(remote_env.os, "chmod", chmod), mock.patch.object(remote_env.tempfile, "mkstemp", mkstemp):
            materialize(delivered(), self.manifest, self.token)
        chmods = [c for c in calls if c[0] == "chmod" and c[1].endswith(".tmp")]
        self.assertEqual(len(chmods), 2)
        for _, name, mode, size in chmods:
            self.assertEqual(mode, 0o600)
            self.assertEqual(size, 0, f"{name}: the mode was set before any data was written")
        self.assertEqual([c[1] for c in calls if c[0] == "mkstemp"], [".bootstrap.json.", ".edx-compute-manager-token."])

    def test_publication_is_an_atomic_rename_of_a_complete_temporary_file(self):
        seen = []
        real_replace = os.replace

        def replace(source, destination):
            source, destination = Path(source), Path(destination)
            seen.append((destination.name, source.parent == destination.parent, source.read_bytes(), destination.exists()))
            return real_replace(source, destination)

        with mock.patch.object(remote_env.os, "replace", replace):
            materialize(delivered(), self.manifest, self.token)
        self.assertEqual([name for name, *_ in seen], ["edx-compute-manager-token", "bootstrap.json"])  # the manifest last: the 'complete' signal
        for name, same_directory, content, existed in seen:
            self.assertTrue(same_directory, f"{name}: the temporary file must share the directory (same filesystem) for the rename to be atomic")
            self.assertTrue(content, name)
            self.assertFalse(existed, name)
        self.assertEqual(self.leftovers(), [])

    def test_a_reader_never_sees_a_partial_file_while_it_is_replaced(self):
        materialize(delivered(), self.manifest, self.token)
        first = self.manifest.read_text(encoding="utf-8")
        reads = []
        real_replace = os.replace

        def replace(source, destination):
            reads.append(Path(destination).read_text(encoding="utf-8"))  # at the instant before the swap the old file is still whole
            return real_replace(source, destination)

        with mock.patch.object(remote_env.os, "replace", replace):
            materialize(delivered(**{ENV_CAPABILITIES: "other-capability"}), self.manifest, self.token)
        self.assertIn(first, reads)
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["capabilities"], ["other-capability"])

    def test_a_failure_before_publication_keeps_the_previous_files_and_removes_the_temporaries(self):
        materialize(delivered(), self.manifest, self.token)
        before = (self.manifest.read_bytes(), self.token.read_bytes())
        with self.assertRaises(ManifestValidationError):
            materialize(delivered(**{ENV_READINESS_URL: URL + "?x=1"}), self.manifest, self.token)
        self.assertEqual((self.manifest.read_bytes(), self.token.read_bytes()), before)
        self.assertEqual(self.leftovers(), [])

    def test_a_failure_while_publishing_leaves_no_temporary_file(self):
        with mock.patch.object(remote_env.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                materialize(delivered(), self.manifest, self.token)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(self.manifest.exists())  # the manifest is the 'complete' signal: it never exists without its credential

    def test_destinations_that_are_symlinks_are_refused(self):
        if not POSIX:
            self.skipTest("links simbolicos exigem privilegio no Windows")
        self.manifest.parent.mkdir(parents=True)
        decoy = self.root / "decoy"
        decoy.write_text("x", encoding="utf-8")
        self.manifest.symlink_to(decoy)
        with self.assertRaisesRegex(EnvDeliveryError, "link simbolico"):
            materialize(delivered(), self.manifest, self.token)
        self.assertEqual(decoy.read_text(encoding="utf-8"), "x")

    def test_a_symlink_destination_is_refused_on_every_platform(self):
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(EnvDeliveryError, "link simbolico"):
                materialize(delivered(), self.manifest, self.token)
        self.assertEqual(self.files(), [])

    def test_a_restart_rewrites_the_same_content(self):
        materialize(delivered(), self.manifest, self.token)
        first = (self.manifest.read_bytes(), self.token.read_bytes())
        materialize(delivered(), self.manifest, self.token)
        self.assertEqual((self.manifest.read_bytes(), self.token.read_bytes()), first)
        self.assertEqual(self.leftovers(), [])

    def test_a_volume_reused_by_another_instance_gets_the_new_manifest(self):
        materialize(delivered(), self.manifest, self.token)
        other = "22222222-2222-4222-8222-222222222222"
        materialize(delivered(**{ENV_INSTANCE_ID: other, ENV_READINESS_URL: f"{ORIGIN}/api/v1/internal/instances/{other}/readiness"}), self.manifest, self.token)
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["instance_id"], other)


class EnvironmentScrubbingTests(Workspace):
    def test_the_private_mapping_keeps_what_the_bootstrap_needs_and_nothing_secret(self):
        environment = delivered(**{TOKEN_ENV: ADMIN_TOKEN, PROVIDER_SECRET_ENV: VAST_KEY, "HF_HOME": "/workspace/.cache/hf"})
        private = private_environment(environment, self.token)
        self.assertEqual(private[TOKEN_FILE_ENV], str(self.token))
        self.assertEqual(private[TRUSTED_ORIGINS_ENV], ORIGIN)
        self.assertEqual(private["HF_HOME"], "/workspace/.cache/hf")
        text = json.dumps(private)
        for secret in (TOKEN, ADMIN_TOKEN, VAST_KEY):
            self.assertNotIn(secret, text)
        self.assertNotIn(TOKEN_ENV, private)
        self.assertNotIn(ENV_READINESS_TOKEN, private)

    def test_secrets_and_consumed_variables_leave_the_real_environment(self):
        environment = delivered(**{TOKEN_ENV: ADMIN_TOKEN, PROVIDER_SECRET_ENV: VAST_KEY, "JUPYTER_TOKEN": "j-0123456789", "PATH": "/bin"})
        removed = scrub_process_environment(environment)
        self.assertEqual(sorted(removed), sorted([ENV_READINESS_TOKEN, TOKEN_ENV, PROVIDER_SECRET_ENV, "JUPYTER_TOKEN", ENV_SCHEMA, ENV_INSTANCE_ID, ENV_CAPABILITIES, ENV_READINESS_URL, TRUSTED_ORIGINS_ENV]))
        self.assertEqual(environment, {PROVIDER_ID_ENV: "5000", "PATH": "/bin"})  # what is not ours is untouched

    def test_a_real_child_process_inherits_no_secret(self):
        """Prova com um processo de verdade: depois de preparar o ambiente, um filho NAO enxerga nenhum segredo, mas enxerga o resto."""
        injected = delivered(**{TOKEN_ENV: ADMIN_TOKEN, PROVIDER_SECRET_ENV: VAST_KEY, "EDX_TEST_KEEP": "kept"})
        with mock.patch.dict(os.environ, injected):
            with contextlib.redirect_stdout(io.StringIO()):
                private = prepare_from_environment(os.environ, self.manifest)
            script = "import json, os; print(json.dumps(dict(os.environ)))"
            child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        seen = json.loads(child.stdout)
        for name in (ENV_READINESS_TOKEN, TOKEN_ENV, PROVIDER_SECRET_ENV, ENV_SCHEMA, ENV_INSTANCE_ID, ENV_CAPABILITIES, ENV_READINESS_URL):
            self.assertNotIn(name, seen)
        for secret in (TOKEN, ADMIN_TOKEN, VAST_KEY):
            self.assertNotIn(secret, child.stdout)
        self.assertEqual(seen.get("EDX_TEST_KEEP"), "kept")
        self.assertEqual(seen.get(PROVIDER_ID_ENV), "5000")
        self.assertIn(TOKEN_FILE_ENV, private)

    def test_the_bootstrap_still_gets_everything_it_needs_through_the_private_mapping(self):
        environment = delivered(**{TOKEN_ENV: ADMIN_TOKEN, TOKEN_FILE_ENV: str(self.token)})
        with contextlib.redirect_stdout(io.StringIO()):
            private = prepare_from_environment(environment, self.manifest)
        parsed = Schema2ManifestParser(private).load(self.manifest)
        self.assertEqual(parsed.readiness_url, URL)
        self.assertEqual(load_service_token(private), TOKEN)  # the credential FILE, never the administrative token in the environment
        self.assertNotIn(TOKEN_ENV, private)

    def test_an_administrative_token_in_the_environment_is_ignored_and_reported_without_its_value(self):
        environment = delivered(**{TOKEN_ENV: ADMIN_TOKEN, TOKEN_FILE_ENV: str(self.token)})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            private = prepare_from_environment(environment, self.manifest)
        self.assertIn(TOKEN_ENV, out.getvalue())
        self.assertNotIn(ADMIN_TOKEN, out.getvalue())
        self.assertEqual(self.token.read_text(encoding="utf-8"), TOKEN)  # the callback uses the per-instance token, never the administrative one
        self.assertNotIn(ADMIN_TOKEN, json.dumps(private))
        self.assertNotIn(TOKEN_ENV, environment)

    def test_nothing_secret_is_printed(self):
        environment = delivered(**{TOKEN_FILE_ENV: str(self.token), PROVIDER_SECRET_ENV: VAST_KEY})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            prepare_from_environment(environment, self.manifest)
        for secret in (TOKEN, VAST_KEY, TOKEN.split(".")[4]):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())

    def test_a_failed_preparation_leaves_the_environment_alone(self):
        environment = delivered(**{ENV_CAPABILITIES: None, TOKEN_FILE_ENV: str(self.token)})
        before = dict(environment)
        with self.assertRaises(EnvDeliveryError):
            prepare_from_environment(environment, self.manifest)
        self.assertEqual(environment, before)  # the process stops with an error; nothing half-done
        self.assertEqual(self.files(), [])


class SupervisorEntrypointTests(Workspace):
    def run_main(self, environment, argv=None):
        from installer import remote_supervisor

        captured = {}

        class FakeSupervisor:
            def __init__(self, *, environment=None, **kwargs):
                captured["environment"] = environment

            def run(self, manifest, comfy, **kwargs):
                captured["manifest_exists"] = Path(manifest).is_file()
                captured["token_exists"] = self_token.is_file()
                return {}

        self_token = self.token
        arguments = argv or ["--manifest", str(self.manifest), "--comfyui", str(self.root / "ComfyUI")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(remote_supervisor, "RemoteSupervisor", FakeSupervisor), \
                mock.patch.object(remote_supervisor.signal, "signal"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = remote_supervisor.main(arguments)
            remaining = dict(os.environ)
        return code, captured, remaining, out.getvalue() + err.getvalue()

    def test_the_supervisor_refuses_an_environment_delivery_when_started_outside_the_launcher(self):
        """RT2.1: only the launcher (the image ENTRYPOINT) materialises the delivery and executes the supervisor with a clean environment. A
        supervisor that finds EDX_BOOTSTRAP_* was started with the original environment, so it refuses instead of accepting half the guarantee."""
        code, captured, remaining, output = self.run_main(delivered(**{TOKEN_FILE_ENV: str(self.token), PROVIDER_SECRET_ENV: VAST_KEY}))
        self.assertEqual(code, 2)
        self.assertIn("remote_launcher", output)
        self.assertEqual(self.files(), [])  # no second mechanism: it writes nothing
        self.assertNotIn("environment", captured)  # and the supervisor never started
        for secret in (TOKEN, VAST_KEY):
            self.assertNotIn(secret, output)

    def test_without_the_delivery_variables_the_previous_behaviour_is_untouched(self):
        code, captured, remaining, _ = self.run_main({TOKEN_FILE_ENV: str(self.token), "PATH": "/bin"})
        self.assertEqual(code, 0)
        self.assertIsNone(captured["environment"])  # RemoteSupervisor() with its default os.environ, exactly as before RT1
        self.assertEqual(self.files(), [])
        self.assertEqual(remaining, {TOKEN_FILE_ENV: str(self.token), "PATH": "/bin"})

    def test_a_partial_delivery_is_refused_too(self):
        code, captured, remaining, output = self.run_main(delivered(**{ENV_CAPABILITIES: None, TOKEN_FILE_ENV: str(self.token)}))
        self.assertEqual(code, 2)
        self.assertNotIn(TOKEN, output)
        self.assertEqual(self.files(), [])


class EndToEndWithTheExistingBootstrapTests(Workspace):
    """The existing RemoteBootstrap, parser and callback client run on what the materialiser produced: no second execution infrastructure."""

    def test_the_callback_presents_the_per_instance_token_read_from_the_private_file(self):
        environment = delivered(**{TOKEN_FILE_ENV: str(self.token), TOKEN_ENV: ADMIN_TOKEN})
        with contextlib.redirect_stdout(io.StringIO()):
            private = prepare_from_environment(environment, self.manifest)
        sent = []

        def opener(request, timeout):
            sent.append(request)
            return FakeResponse(200)

        bootstrap = RemoteBootstrap(
            environment=private, runtime_installer=FakeRuntimeInstaller(), provisioner=FakeProvisioner(), controller=FakeController(),
            callback_client=ReadinessCallbackClient(opener=opener, sleeper=lambda _s: None),
        )
        result = bootstrap.run(self.manifest, self.root / "ComfyUI")
        self.assertEqual(result["instance_id"], INSTANCE)
        self.assertEqual(result["provider_instance_id"], "5000")
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")  # never the administrative token
        self.assertEqual(json.loads(request.data), {"provider_instance_id": "5000"})
        state = (self.manifest.parent / "bootstrap-state.json").read_text(encoding="utf-8")
        for secret in (TOKEN, ADMIN_TOKEN):
            self.assertNotIn(secret, state)
            self.assertNotIn(secret, self.manifest.read_text(encoding="utf-8"))

    def test_a_bootstrap_error_never_sends_the_callback_and_never_leaks_the_token(self):
        environment = delivered(**{ENV_CAPABILITIES: "unknown-capability", TOKEN_FILE_ENV: str(self.token)})
        with contextlib.redirect_stdout(io.StringIO()):
            private = prepare_from_environment(environment, self.manifest)
        sent = []
        bootstrap = RemoteBootstrap(
            environment=private, runtime_installer=FakeRuntimeInstaller(), provisioner=FakeProvisioner(), controller=FakeController(),
            callback_client=ReadinessCallbackClient(opener=lambda r, timeout: sent.append(r) or FakeResponse(200), sleeper=lambda _s: None),
        )
        with self.assertRaises((ProvisioningError, RemoteBootstrapError)) as raised:
            bootstrap.run(self.manifest, self.root / "ComfyUI")
        self.assertEqual(sent, [])
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn(TOKEN, (self.manifest.parent / "bootstrap-state.json").read_text(encoding="utf-8"))

    def test_the_manifest_the_materialiser_wrote_is_what_the_legacy_path_would_have_written(self):
        """Same schema 2 content as the Compute Manager's remote-exec transport: only the transport differs."""
        materialize(delivered(), self.manifest, self.token)
        self.assertEqual(
            self.manifest.read_text(encoding="utf-8"),
            json.dumps({"schema_version": 2, "instance_id": INSTANCE, "provider_instance_id": "5000", "capabilities": ["fixture-capability"],
                        "callback": {"readiness_url": URL}}, separators=(",", ":")),
        )


if __name__ == "__main__":
    unittest.main()
