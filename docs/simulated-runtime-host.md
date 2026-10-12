# RT2.2: simulated Runtime host

RT2.2 is a separate, non-production process for the isolated E1 environment. It proves the CM5 bootstrap and CM4 authenticated
readiness boundary without loading model weights, starting ComfyUI, importing CUDA, running inference, or obtaining provider
resources.

## Reused production components

The host does not define another manifest or callback protocol. `installer.simulated_host` calls the existing RT1
`remote_env.materialize`, `Schema2ManifestParser`, `load_service_token`, and `ReadinessCallbackClient`. The resulting manifest is
schema 2, the credential is a separate `0600` file, and the callback body and Bearer header are therefore identical to the real
Runtime flow. It deliberately does not invoke `RemoteSupervisor`, `RemoteBootstrap`, `Provisioner`, or `ComfyUIController`.

The CM5 contract has no capacity-request identifier. The host correlates by `instance_id`; the Compute Manager remains the owner
of the mapping from that instance to its capacity request. `provider_instance_id` is correlated exactly by the readiness endpoint.

## Private CM6 handoff

The real provider injects `env` and `secret_env` in the create operation. CM6 cannot launch a container, and previously discarded
`secret_env`, so a separate process could not receive the scoped readiness token. The isolated bridge is an atomic private spool:

1. `FakeIntegrationProvider`, only when `COMPUTE_FAKE_RUNTIME_SPOOL_DIR` is explicitly set, writes one `0600` envelope after the
   simulated create. It contains the exact CM5 environment plus the deterministic fake `CONTAINER_ID`.
2. The host atomically claims the envelope, validates its closed shape, materialises it through RT1, and deletes the transport copy.
3. Manifest, credential and secret-free recovery state are kept in separate private directories. A restart retries a pending
   callback and replays a successful callback once; the CM readiness transition is idempotent.

This spool is not canonical API and is never used by the Vast provider. It adds no HTTP route or administrative credential and
does not put the token in the CM database or logs. The in-process CM6 auto-readiness mode and the spool are mutually exclusive.
The producer first writes a non-consumable `.pending` envelope in the create transaction and exposes it only after commit. If a
worker stops between those steps, provider query/adoption finalises the pending handoff after restart.

## Fail-closed activation

The host starts only when all of these are present:

```text
EDX_SIMULATED_RUNTIME_ENABLED=true
EDX_SIMULATED_RUNTIME_ACKNOWLEDGEMENT=I_UNDERSTAND_SIMULATED_RUNTIME_RUNS_NO_COMFYUI_OR_GPU
EDX_SIMULATED_RUNTIME_ENVIRONMENT=integration   # local, test or integration only
EDX_SIMULATED_RUNTIME_SPOOL_DIR=/run/edx-runtime-bootstrap
EDX_SIMULATED_RUNTIME_STATE_DIR=/workspace/.edx/simulated
EDX_SIMULATED_RUNTIME_SECRETS_DIR=/run/secrets/edx-simulated-runtime
NVIDIA_VISIBLE_DEVICES=void
CUDA_VISIBLE_DEVICES=-1
```

Paths must be absolute and distinct. Symbolic-link directories are rejected. The host also refuses production and visible GPU
device settings. `deploy/simulated/Dockerfile` contains no models, workflows, ComfyUI entrypoint, or CUDA base image and runs as a
non-root user.

`EDX_SIMULATED_RUNTIME_OUTCOME` is `ready`, `absent`, or `invalid_token`. `absent` materialises but never calls readiness;
`invalid_token` sends a deliberately altered scoped credential so the normal CM4 verifier rejects it. Startup delay is controlled
by `EDX_SIMULATED_RUNTIME_STARTUP_DELAY_SECONDS` (0–300). An actually expired token is passed unchanged and rejected by CM4; there
is no signature bypass.

## Docker and E1

Build only the simulated image from the Runtime repository root:

```bash
docker build -f deploy/simulated/Dockerfile -t edx-comfy-runtime:rt2.2 .
```

E1 must mount one private spool into the CM worker and Runtime host, mount persistent private state/secrets for restart recovery,
disable `COMPUTE_FAKE_RUNTIME_AUTO_READY`, and set `COMPUTE_FAKE_RUNTIME_SPOOL_DIR` on the worker. The callback URL remains HTTPS.
The existing CM7 integration stack still needs its planned internal TLS proxy/test certificate and name resolution before the
separate container can perform a real callback. RT2.2 does not add that proxy and does not run E1.

The shared volume must be writable by the non-root users of both images and unavailable to unrelated containers. E1 should use
explicit numeric UIDs or a pre-created bind directory with a shared group; relaxing file modes is not acceptable.

## Tests

`tests/test_simulated_host.py` uses an isolated loopback HTTP server and temporary directories. It covers the CM5 envelope,
schema validation, RT1 materialisation, private files, signed Bearer callback and correlation, invalid/expired credentials,
absent/delayed readiness, restart recovery, callback replay, fail-closed activation, and the minimal Docker definition.
