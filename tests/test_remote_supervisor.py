from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from installer.provisioning import ProvisioningError
from installer.remote_bootstrap import (
    TOKEN_FILE_ENV,
    TRUSTED_ORIGINS_ENV,
    ManifestValidationError,
    RemoteBootstrap,
)
from installer.remote_supervisor import RemoteSupervisor, RemoteSupervisorError, SupervisorLock


class FakeBootstrap:
    def __init__(self, *, pid=4321, error=None, stop_event=None, write_state=False):
        self.pid = pid
        self.error = error
        self.stop_event = stop_event
        self.write_state = write_state
        self.calls = []

    def run(self, manifest, comfy, **kwargs):
        self.calls.append((Path(manifest), Path(comfy), kwargs))
        if self.write_state:
            state = Path(comfy) / "user" / "default" / "EDx" / "remote-comfyui-process.json"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"pid": self.pid}), encoding="utf-8")
        if self.error:
            raise self.error
        if self.stop_event is not None:
            self.stop_event.set()
        return {"comfyui_pid": self.pid, "callback_status": 204}


class RemoteSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = self.root / ".edx" / "bootstrap.json"
        self.secret = self.root / "private" / "service-token"
        self.secret.parent.mkdir(parents=True)
        self.secret.write_text("test-service-token\n", encoding="utf-8")
        self.secret.chmod(0o600)
        self.environment = {TOKEN_FILE_ENV: str(self.secret)}
        self.comfy = self.root / "ComfyUI"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_manifest(self, body="{}"):
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(body, encoding="utf-8")

    def test_manifest_can_arrive_after_container_start(self):
        stop = threading.Event()
        fake = FakeBootstrap(stop_event=stop)
        terminated = []

        def deliver_manifest():
            time.sleep(0.03)
            self._write_manifest()

        writer = threading.Thread(target=deliver_manifest)
        writer.start()
        supervisor = RemoteSupervisor(
            environment=self.environment,
            bootstrap=fake,
            process_state_fn=lambda pid: "alive",
            terminate_process=lambda pid, timeout: terminated.append((pid, timeout)),
        )
        result = supervisor.run(
            self.manifest,
            self.comfy,
            stop_event=stop,
            wait_timeout=1,
            poll_interval=0.005,
        )
        writer.join(timeout=1)
        self.assertEqual(result["callback_status"], 204)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(terminated, [(4321, 20)])

    def test_invalid_manifest_stops_before_provisioning_or_callback(self):
        self._write_manifest("{")
        environment = {
            **self.environment,
            TRUSTED_ORIGINS_ENV: "https://compute.example",
        }
        supervisor = RemoteSupervisor(
            environment=environment,
            bootstrap=RemoteBootstrap(environment=environment),
            process_state_fn=lambda pid: "dead",
        )
        with self.assertRaises(ManifestValidationError):
            supervisor.run(
                self.manifest,
                self.comfy,
                stop_event=threading.Event(),
                wait_timeout=0.1,
                poll_interval=0.005,
            )
        self.assertFalse((self.manifest.parent / "bootstrap-state.json").exists())

    def test_missing_credential_configuration_fails_without_bootstrap(self):
        self._write_manifest()
        fake = FakeBootstrap()
        supervisor = RemoteSupervisor(environment={}, bootstrap=fake)
        with self.assertRaisesRegex(RemoteSupervisorError, "credencial remota"):
            supervisor.run(
                self.manifest,
                self.comfy,
                stop_event=threading.Event(),
                wait_timeout=0.1,
                poll_interval=0.005,
            )
        self.assertEqual(fake.calls, [])

    def test_configured_but_absent_secret_file_times_out_without_bootstrap(self):
        self._write_manifest()
        fake = FakeBootstrap()
        supervisor = RemoteSupervisor(
            environment={TOKEN_FILE_ENV: str(self.root / "missing-token")},
            bootstrap=fake,
        )
        with self.assertRaisesRegex(RemoteSupervisorError, "arquivo de credencial"):
            supervisor.run(
                self.manifest,
                self.comfy,
                stop_event=threading.Event(),
                wait_timeout=0.02,
                poll_interval=0.005,
            )
        self.assertEqual(fake.calls, [])

    def test_entrypoint_restart_recovers_released_lock_without_overlap(self):
        self._write_manifest()
        terminations = []
        for pid in (4101, 4102):
            stop = threading.Event()
            fake = FakeBootstrap(pid=pid, stop_event=stop)
            supervisor = RemoteSupervisor(
                environment=self.environment,
                bootstrap=fake,
                process_state_fn=lambda candidate: "alive",
                terminate_process=lambda candidate, timeout: terminations.append(candidate),
            )
            supervisor.run(
                self.manifest,
                self.comfy,
                stop_event=stop,
                poll_interval=0.005,
            )
        self.assertEqual(terminations, [4101, 4102])
        self.assertFalse((self.manifest.parent / "remote-supervisor.lock").exists())

    def test_live_supervisor_lock_prevents_duplicate_processes(self):
        self._write_manifest()
        lock = self.manifest.parent / "remote-supervisor.lock"
        lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        fake = FakeBootstrap()
        supervisor = RemoteSupervisor(
            environment=self.environment,
            bootstrap=fake,
            process_state_fn=lambda pid: "alive",
        )
        with self.assertRaisesRegex(RemoteSupervisorError, "ja esta ativo"):
            supervisor.run(
                self.manifest,
                self.comfy,
                stop_event=threading.Event(),
                poll_interval=0.005,
            )
        self.assertEqual(fake.calls, [])

    def test_stale_lock_with_reused_pid_is_recovered(self):
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.manifest.parent / "remote-supervisor.lock"
        lock_path.write_text(
            json.dumps({"pid": 2, "process_identity": "old-boot:old-start"}),
            encoding="utf-8",
        )
        lock = SupervisorLock(
            lock_path,
            state=lambda pid: "alive",
            identity=lambda pid: "new-boot:new-start",
        )
        lock.acquire()
        self.assertTrue(lock.acquired)
        lock.release()
        self.assertFalse(lock_path.exists())

    def test_provisioning_failure_terminates_started_comfyui(self):
        self._write_manifest()
        terminated = []
        fake = FakeBootstrap(
            pid=987,
            error=ProvisioningError("fixture provisioning failed"),
            write_state=True,
        )
        supervisor = RemoteSupervisor(
            environment=self.environment,
            bootstrap=fake,
            process_state_fn=lambda pid: "alive",
            terminate_process=lambda pid, timeout: terminated.append(pid),
        )
        with self.assertRaises(ProvisioningError):
            supervisor.run(
                self.manifest,
                self.comfy,
                stop_event=threading.Event(),
                poll_interval=0.005,
            )
        self.assertEqual(terminated, [987])

    def test_shutdown_event_cleanly_terminates_tracked_process(self):
        self._write_manifest()
        stop = threading.Event()
        calls = []
        supervisor = RemoteSupervisor(
            environment=self.environment,
            bootstrap=FakeBootstrap(pid=2468, stop_event=stop),
            process_state_fn=lambda pid: "alive",
            terminate_process=lambda pid, timeout: calls.append((pid, timeout)),
        )
        supervisor.run(
            self.manifest,
            self.comfy,
            stop_event=stop,
            shutdown_timeout=7,
            poll_interval=0.005,
        )
        self.assertEqual(calls, [(2468, 7)])

    def test_docker_entrypoint_uses_supervisor_and_contains_no_secret(self):
        from installer import remote_launcher

        dockerfile = Path("deploy/vast/Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"installer.remote_launcher"', dockerfile)  # RT2.1: the ENTRYPOINT is the launcher, which executes tini and the supervisor
        self.assertIn("tini", dockerfile)
        self.assertEqual(remote_launcher.supervisor_argv([]), ["/usr/bin/tini", "--", "python", "-m", "installer.remote_supervisor"])
        self.assertIn("EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE", dockerfile)
        self.assertNotIn("test-service-token", dockerfile)

    def test_desktop_entrypoint_has_no_remote_supervisor_dependency(self):
        source = Path("install_workflow.py").read_text(encoding="utf-8")
        self.assertNotIn("remote_supervisor", source)
        self.assertNotIn("remote_bootstrap", source)
        self.assertNotIn("EDX_COMPUTE_MANAGER", source)


if __name__ == "__main__":
    unittest.main()
