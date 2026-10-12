"""RT2 smoke harness: simulated heavy parts under the REAL Docker ENTRYPOINT.

Loaded by Python at startup ONLY when RT2_SMOKE=1 (PYTHONPATH=/opt/rt2). It replaces what would need a GPU, a network or gigabytes: the ComfyUI
runtime installer, the provisioner (no models are downloaded) and the ComfyUI process (a harmless child that dumps its own environment). The
REAL code keeps running: tini, remote_supervisor.main, remote_env (materialise + scrub), RemoteSupervisor, RemoteBootstrap, Schema2ManifestParser,
load_service_token and ReadinessCallbackClient. The callback goes to a fake Compute Manager on loopback that verifies the scoped token with the
reference oracle (the same rules as compute.readiness_auth). Nothing here is part of the image or of the product.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if os.environ.get("RT2_SMOKE") == "1" and os.environ.get("RT2_ROLE") != "child":
    sys.path[:0] = ["/opt/edx-comfy-runtime", "/opt/edx-comfy-runtime/tests", "/opt/rt2"]

    OUT = Path(os.environ.get("RT2_OUT", "/tmp/rt2"))
    OUT.mkdir(parents=True, exist_ok=True)
    MODE = os.environ.get("RT2_CALLBACK_MODE", "verify")  # verify | 500 | 409
    FAIL = os.environ.get("RT2_FAIL", "")                  # install | comfy
    PORT = int(os.environ.get("RT2_PORT", "8099"))
    KEY_HEX = Path("/opt/rt2/key.hex").read_text(encoding="utf-8").strip()  # synthetic key of the contract vectors, baked into the TEST layer (not the environment)
    SPEC = "/opt/rt2/child_probe.py"

    def log(event, **fields):
        with (OUT / "harness.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"t": time.time(), "pid": os.getpid(), "event": event, **fields}) + "\n")

    # ------------------------------------------------------------------------------------------ fake Compute Manager (callback)
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            import readiness_token_reference as ref

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            header = self.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            parts = self.path.rstrip("/").split("/")
            path_instance = parts[-2] if len(parts) >= 2 else ""
            status, payload = 200, {"instance_id": path_instance, "state": "READY", "changed": True}
            reason = "ok"
            if MODE == "500":
                status, payload, reason = 500, {"error": {"code": "internal_error"}}, "forced 500"
            elif MODE == "409":
                status, payload, reason = 409, {"error": {"code": "runtime_readiness_rejected"}}, "forced 409"
            else:
                verdict = ref.verify(token, path_instance_id=path_instance, now=int(time.time()), keys={"k1": bytes.fromhex(KEY_HEX)})
                if not verdict.valid:
                    status, reason = verdict.http_status, verdict.reason
                    payload = {"error": {"code": "forbidden" if status == 403 else "unauthenticated"}}
            log("callback", path=self.path, status=status, reason=reason, body=body, auth_scheme=header.split(" ")[0] if header else "",
                token_sha256=hashlib.sha256(token.encode()).hexdigest() if token else "", has_admin_token=("admin" in token))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("fake_compute_manager_listening", port=PORT, mode=MODE)

    # ------------------------------------------------------------------------------------------ fakes for the heavy parts
    import installer.remote_bootstrap as rb
    from installer.provisioning import ProvisioningError
    from test_remote_bootstrap import FakeAPI, FakeProvisioner

    class RuntimeInstaller:
        def ensure(self, comfy, backend=None, cancel_event=None):
            comfy = Path(comfy)
            (comfy / "models").mkdir(parents=True, exist_ok=True)
            (comfy / "main.py").write_text("", encoding="utf-8")
            log("runtime_installer_simulated", comfy=str(comfy))
            return {"installed": True, "backend": backend}

    class Provisioner(FakeProvisioner):
        def __init__(self, *args, **kwargs):
            super().__init__(install_failure=ProvisioningError("falha simulada de instalacao") if FAIL == "install" else None)

        def install(self, plan, cancel_event=None):  # the supervisor passes a cancel_event; the test fake does not take one
            return super().install(plan)

    class Controller:
        """Starts a REAL child process the way ComfyUIController would (it inherits os.environ as it is at that moment) and records what it sees."""

        def __init__(self, *args, **kwargs):
            self.api = FakeAPI()

        def ensure_running(self, comfy, base_url, **kwargs):
            if FAIL == "comfy":
                raise rb.ReadinessError("ComfyUI simulado nao ficou disponivel")
            probe = OUT / "comfy-child-environ.json"
            probe.unlink(missing_ok=True)
            process = subprocess.Popen(
                [sys.executable, "-S", SPEC, str(probe)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env={**os.environ, "RT2_ROLE": "child"},  # same environment the real controller passes on (Popen default = os.environ)
            )
            deadline = time.time() + 10
            while not probe.exists() and time.time() < deadline:
                time.sleep(0.05)
            log("comfy_simulated", pid=process.pid)
            return {"reused": False, "pid": process.pid}

    rb.BaseRuntimeInstaller = RuntimeInstaller
    rb.Provisioner = Provisioner
    rb.ComfyUIController = Controller
    log("harness_active", pid=os.getpid(), argv=sys.argv)
