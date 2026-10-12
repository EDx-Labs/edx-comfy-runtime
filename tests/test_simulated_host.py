"""RT2.2 isolated simulated host tests: no GPU, ComfyUI, models, or shared services."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from installer.remote_bootstrap import ALLOW_INSECURE_LOCAL_ENV
from installer.remote_env import (
    ENV_CAPABILITIES,
    ENV_INSTANCE_ID,
    ENV_READINESS_TOKEN,
    ENV_READINESS_URL,
    ENV_SCHEMA,
    PROVIDER_ID_ENV,
)
from installer.simulated_host import (
    ACKNOWLEDGEMENT,
    ACK_ENV,
    DEPLOYMENT_ENV,
    ENABLED_ENV,
    ENVELOPE_VERSION,
    OUTCOME_ENV,
    SECRETS_ENV,
    SPOOL_ENV,
    STATE_ENV,
    HostConfig,
    SimulatedHostError,
    SimulatedRuntimeHost,
)

INSTANCE = "11111111-1111-4111-8111-111111111111"
PROVIDER = "fake-" + "a" * 32
KEY = bytes.fromhex("11" * 32)
KID = "e1fake"


def token(expiry: int) -> str:
    message = f"edx-readiness|v1|{INSTANCE}|{expiry}".encode("ascii")
    signature = base64.urlsafe_b64encode(hmac.new(KEY, message, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
    return f"edxrt1.{INSTANCE}.{expiry}.{KID}.{signature}"


class CallbackServer:
    def __init__(self, statuses: list[int] | None = None, *, reject_expired: bool = False, verify_hmac: bool = False) -> None:
        self.statuses = list(statuses or [200])
        self.reject_expired = reject_expired
        self.verify_hmac = verify_hmac
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib callback name
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size)
                authorization = self.headers.get("Authorization", "")
                outer.requests.append({"path": self.path, "authorization": authorization, "body": body})
                status = outer.statuses.pop(0) if outer.statuses else 200
                if outer.verify_hmac:
                    try:
                        presented = authorization.removeprefix("Bearer ")
                        version, instance_id, expiry_text, kid, signature = presented.split(".")
                        message = f"edx-readiness|v1|{instance_id}|{expiry_text}".encode("ascii")
                        expected = base64.urlsafe_b64encode(hmac.new(KEY, message, hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
                        valid = (
                            version == "edxrt1"
                            and instance_id == INSTANCE
                            and kid == KID
                            and hmac.compare_digest(signature, expected)
                            and self.path.endswith(f"/{instance_id}/readiness")
                        )
                    except (ValueError, UnicodeEncodeError):
                        valid = False
                    if not valid:
                        status = 401
                if outer.reject_expired:
                    try:
                        expiry = int(authorization.split(".")[2])
                    except (IndexError, ValueError):
                        status = 401
                    else:
                        if expiry <= int(time.time()):
                            status = 401
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


class RuntimeHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool = self.root / "spool"
        self.state = self.root / "state"
        self.secrets = self.root / "secrets"
        self.base_environment = {
            ENABLED_ENV: "true",
            ACK_ENV: ACKNOWLEDGEMENT,
            DEPLOYMENT_ENV: "integration",
            SPOOL_ENV: str(self.spool),
            STATE_ENV: str(self.state),
            SECRETS_ENV: str(self.secrets),
            ALLOW_INSECURE_LOCAL_ENV: "1",
            "NVIDIA_VISIBLE_DEVICES": "void",
            "CUDA_VISIBLE_DEVICES": "-1",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, **changes) -> HostConfig:
        environment = {**self.base_environment, **changes}
        return HostConfig.from_environment(environment)

    def delivered(self, origin: str, readiness_token: str | None = None) -> dict[str, str]:
        return {
            ENV_SCHEMA: "2",
            ENV_INSTANCE_ID: INSTANCE,
            ENV_CAPABILITIES: "minimax-h3-fast",
            ENV_READINESS_URL: f"{origin}/api/v1/internal/instances/{INSTANCE}/readiness",
            "EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": origin,
            ENV_READINESS_TOKEN: readiness_token or token(int(time.time()) + 600),
            PROVIDER_ID_ENV: PROVIDER,
        }

    def publish(self, environment: dict[str, str], *, filename: str = INSTANCE) -> Path:
        self.spool.mkdir(parents=True, exist_ok=True)
        path = self.spool / f"{filename}.json"
        path.write_text(json.dumps({"version": ENVELOPE_VERSION, "environment": environment}), encoding="utf-8")
        path.chmod(0o600)
        return path

    def read_state(self) -> dict:
        return json.loads((self.state / INSTANCE / "state.json").read_text(encoding="utf-8"))

    def test_real_rt1_materialization_and_signed_correlated_callback(self):
        with CallbackServer(verify_hmac=True) as callback:
            expected_token = token(int(time.time()) + 600)
            self.publish(self.delivered(callback.origin, expected_token))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once()

        self.assertEqual(result, {"replayed": 0, "processed": 1, "failed": 0})
        request = callback.requests[0]
        self.assertEqual(request["path"], f"/api/v1/internal/instances/{INSTANCE}/readiness")
        self.assertEqual(request["authorization"], f"Bearer {expected_token}")
        self.assertEqual(json.loads(request["body"]), {"provider_instance_id": PROVIDER})
        manifest = json.loads((self.state / INSTANCE / "bootstrap.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["instance_id"], INSTANCE)
        self.assertEqual(manifest["provider_instance_id"], PROVIDER)
        self.assertEqual(self.read_state()["status"], "ready")
        self.assertFalse(list(self.spool.glob("*")))
        self.assertNotIn(expected_token, output.getvalue())
        self.assertNotIn(expected_token.split(".")[-1], output.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are authoritative only on Linux")
    def test_manifest_secret_state_and_input_have_private_permissions(self):
        with CallbackServer() as callback:
            incoming = self.publish(self.delivered(callback.origin))
            self.assertEqual(incoming.stat().st_mode & 0o777, 0o600)
            SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once()
        paths = [
            self.state / INSTANCE / "bootstrap.json",
            self.state / INSTANCE / "state.json",
            self.secrets / f"{INSTANCE}.token",
        ]
        for path in paths:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        for directory in (self.spool, self.state, self.secrets):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)

    def test_absent_readiness_and_startup_delay_are_controlled_without_callback(self):
        delays = []
        with CallbackServer() as callback:
            self.publish(self.delivered(callback.origin))
            config = self.config(**{OUTCOME_ENV: "absent", "EDX_SIMULATED_RUNTIME_STARTUP_DELAY_SECONDS": "7"})
            result = SimulatedRuntimeHost(config, sleeper=delays.append).run_once()
            self.assertEqual(callback.requests, [])
        self.assertEqual(delays, [7.0])
        self.assertEqual(result["processed"], 1)
        self.assertEqual(self.read_state()["status"], "readiness_suppressed")

    def test_invalid_callback_is_rejected_without_exposing_the_real_token(self):
        with CallbackServer(statuses=[401]) as callback:
            real_token = token(int(time.time()) + 600)
            self.publish(self.delivered(callback.origin, real_token))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = SimulatedRuntimeHost(self.config(**{OUTCOME_ENV: "invalid_token"}), sleeper=lambda _s: None).run_once()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.read_state()["status"], "callback_rejected")
        self.assertNotEqual(callback.requests[0]["authorization"], f"Bearer {real_token}")
        self.assertNotIn(real_token, output.getvalue())

    def test_expired_scoped_token_is_not_bypassed(self):
        expired = token(int(time.time()) - 1)
        with CallbackServer(reject_expired=True) as callback:
            self.publish(self.delivered(callback.origin, expired))
            result = SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.read_state()["status"], "callback_rejected")
        self.assertEqual(callback.requests[0]["authorization"], f"Bearer {expired}")

    def test_restart_recovers_transient_failure_and_replays_ready_idempotently(self):
        with CallbackServer(statuses=[503, 503, 503, 503, 200, 200]) as callback:
            self.publish(self.delivered(callback.origin))
            first = SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once()
            self.assertEqual(first["failed"], 1)
            self.assertEqual(self.read_state()["status"], "callback_failed")

            second = SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once(replay_existing=True)
            self.assertEqual(second["replayed"], 1)
            self.assertEqual(self.read_state()["status"], "ready")

            third = SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once(replay_existing=True)
            self.assertEqual(third["replayed"], 1)
        self.assertEqual(len(callback.requests), 6)
        self.assertEqual([json.loads(item["body"]) for item in callback.requests[-2:]], [{"provider_instance_id": PROVIDER}] * 2)

    def test_bad_schema_or_wrong_filename_fails_before_any_callback(self):
        with CallbackServer() as callback:
            environment = self.delivered(callback.origin)
            environment[ENV_SCHEMA] = "3"
            self.publish(environment)
            result = SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once()
            self.assertEqual(result["failed"], 1)
            self.assertEqual(callback.requests, [])
            self.assertFalse((self.state / INSTANCE / "bootstrap.json").exists())

        with CallbackServer() as callback:
            self.publish(self.delivered(callback.origin), filename="22222222-2222-4222-8222-222222222222")
            result = SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once()
            self.assertGreaterEqual(result["failed"], 1)
            self.assertEqual(callback.requests, [])

    def test_activation_is_fail_closed_in_production_and_with_gpu_visibility(self):
        cases = [
            {ENABLED_ENV: "false"},
            {ACK_ENV: "wrong"},
            {DEPLOYMENT_ENV: "production"},
            {"NVIDIA_VISIBLE_DEVICES": "all"},
            {"CUDA_VISIBLE_DEVICES": "0"},
        ]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(SimulatedHostError):
                self.config(**change)

    def test_fake_provider_identifier_uses_the_existing_manifest_grammar(self):
        with CallbackServer() as callback:
            self.publish(self.delivered(callback.origin))
            SimulatedRuntimeHost(self.config(), sleeper=lambda _s: None).run_once()
        manifest = json.loads((self.state / INSTANCE / "bootstrap.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["provider_instance_id"], PROVIDER)

    def test_docker_image_is_minimal_and_never_starts_the_real_runtime(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "deploy" / "simulated" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("installer.simulated_host", dockerfile)
        self.assertIn("NVIDIA_VISIBLE_DEVICES=void", dockerfile)
        for forbidden in ("remote_supervisor", "remote_bootstrap", "ComfyUI", "workflows", "models"):
            self.assertNotIn(forbidden, dockerfile)


if __name__ == "__main__":
    unittest.main()
