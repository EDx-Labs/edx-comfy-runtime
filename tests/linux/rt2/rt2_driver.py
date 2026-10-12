"""RT2 - validation of the EDx Runtime on real Linux, under its real Docker ENTRYPOINT. No GPU, no network, no models, synthetic secrets only.

Runs the unmodified ``deploy/vast/Dockerfile`` image and, on top of it, a TEST-ONLY layer (the unit tests and a harness that simulates only
what needs a GPU/network: the ComfyUI installer, the provisioner, the ComfyUI process and the Compute Manager's callback). tini, the supervisor,
remote_env, RemoteBootstrap and the callback client are the real code.

    python tests/linux/rt2/rt2_driver.py build            # build the two images (the Dockerfile as it is, then the test layer)
    python tests/linux/rt2/rt2_driver.py all              # every scenario; evidence under $RT2_WORK (default: <tmp>/edx-rt2/evidence)
    python tests/linux/rt2/rt2_driver.py success failures # or pick scenarios

Needs a running Docker daemon and, next to this repository, edx-compute-manager (its readiness-token oracle and synthetic key are reused so the
fake Compute Manager verifies tokens by the same rules as the real one). Every Docker object created is named edx-rt2-* and labelled edx.rt2=1;
nothing else on the daemon is touched, and what is created is removed (images stay; `docker rmi edx-rt2/runtime edx-rt2/runtime-smoke` removes them).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
WORK = Path(os.environ.get("RT2_WORK") or Path(tempfile.gettempdir()) / "edx-rt2")
EVIDENCE = WORK / "evidence"
CTX = WORK / "ctx"
CM_CONTRACTS = REPO.parent / "infra" / "edx-compute-manager" / "contracts"
sys.path.insert(0, str(CM_CONTRACTS / "tools"))
import readiness_token_reference as ref  # noqa: E402

RUNTIME_IMAGE = "edx-rt2/runtime:validate"
IMAGE = "edx-rt2/runtime-smoke:validate"
KEY_HEX = json.loads((CM_CONTRACTS / "fixtures" / "readiness-token" / "vectors.json").read_text(encoding="utf-8"))["keys_hex"]["k1"]  # synthetic, from the contract vectors
KEY = bytes.fromhex(KEY_HEX)
ADMIN = "rt2-fake-ADMIN-service-token-0123456789"
VAST_KEY = "rt2-fake-vast-container-api-key-0123456789"
JUPYTER = "rt2-fake-jupyter-token-0123456789"
RUN = uuid.uuid4().hex[:6]
# Variables that must NOT survive in any definitive process. EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS is deliberately kept: it is the non-secret callback allow-list
# the supervisor reads from its environment.
DELIVERED = {"EDX_BOOTSTRAP_SCHEMA_VERSION", "EDX_BOOTSTRAP_INSTANCE_ID", "EDX_BOOTSTRAP_CAPABILITIES", "EDX_BOOTSTRAP_READINESS_URL",
             "EDX_BOOTSTRAP_READINESS_TOKEN", "EDX_COMPUTE_MANAGER_SERVICE_TOKEN", "CONTAINER_API_KEY", "JUPYTER_TOKEN"}
CREATED = {"containers": [], "volumes": []}
RESULTS = []



def build():
    """Image 1: the Dockerfile exactly as it is in the repository. Image 2: a TEST-ONLY layer (unit tests, harness, the two files two repository tests read)."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    first = subprocess.run(["docker", "build", "-q", "-f", str(REPO / "deploy" / "vast" / "Dockerfile"), "-t", RUNTIME_IMAGE, str(REPO)], text=True, capture_output=True)
    print("runtime image:", first.stdout.strip() or first.stderr[-400:], flush=True)
    if first.returncode:
        raise SystemExit("the Runtime image did not build")
    if CTX.exists():
        shutil.rmtree(CTX)
    (CTX / "rt2").mkdir(parents=True)
    for f in (HERE / "support").iterdir():
        shutil.copy(f, CTX / "rt2" / f.name)
    shutil.copy(CM_CONTRACTS / "tools" / "readiness_token_reference.py", CTX / "rt2" / "readiness_token_reference.py")
    (CTX / "rt2" / "key.hex").write_text(KEY_HEX, encoding="utf-8")
    shutil.copytree(REPO / "tests", CTX / "tests", ignore=shutil.ignore_patterns("__pycache__", "linux"))
    shutil.copy(REPO / ".gitmodules", CTX / ".gitmodules")
    shutil.copytree(REPO / "deploy", CTX / "deploy")
    (CTX / "Dockerfile.smoke").write_text(
        "FROM " + RUNTIME_IMAGE + "\nCOPY tests /opt/edx-comfy-runtime/tests\nCOPY rt2 /opt/rt2\nCOPY .gitmodules /opt/edx-comfy-runtime/.gitmodules\nCOPY deploy /opt/edx-comfy-runtime/deploy\n",
        encoding="utf-8",
    )
    second = subprocess.run(["docker", "build", "-q", "-f", str(CTX / "Dockerfile.smoke"), "-t", IMAGE, str(CTX)], text=True, capture_output=True)
    print("test layer:", second.stdout.strip() or second.stderr[-400:], flush=True)
    if second.returncode:
        raise SystemExit("the test layer did not build")


def sh(*args, timeout=120, check=False, input=None):
    p = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, input=input)
    if check and p.returncode:
        raise RuntimeError(f"{args[:4]} -> {p.returncode}: {p.stderr[-300:]}")
    return p


def docker(*args, **kw):
    return sh("docker", *args, **kw)


def record(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def mint(instance, *, exp_in=3600, kid="k1"):
    return ref.mint(KEY, kid, instance, int(time.time()) + exp_in)


def environment(instance, token, **over):
    port = over.pop("port", 8099)
    base = {
        "RT2_SMOKE": "1", "PYTHONPATH": "/opt/rt2", "RT2_PORT": str(port), "RT2_OUT": "/tmp/rt2",
        "EDX_BOOTSTRAP_SCHEMA_VERSION": "2", "EDX_BOOTSTRAP_INSTANCE_ID": instance, "EDX_BOOTSTRAP_CAPABILITIES": "fixture-capability",
        "EDX_BOOTSTRAP_READINESS_URL": f"http://127.0.0.1:{port}/api/v1/internal/instances/{instance}/readiness",
        "EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": f"http://127.0.0.1:{port}", "EDX_BOOTSTRAP_READINESS_TOKEN": token,
        "EDX_ALLOW_INSECURE_LOCAL_CALLBACK": "1",  # loopback http is the Runtime's explicit TEST exception; production is https
        "CONTAINER_ID": "5000", "CONTAINER_API_KEY": VAST_KEY, "EDX_COMPUTE_MANAGER_SERVICE_TOKEN": ADMIN,
        "EDX_BOOTSTRAP_POLL_INTERVAL": "0.2",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def run(name, env, *, volume=None, tmpfs_secrets=False, extra=(), detach=True, entrypoint=None, command=()):
    cname = f"edx-rt2-{RUN}-{name}"
    args = ["run", "--name", cname, "--network", "none", "--memory", "1g", "--cpus", "2", "--pids-limit", "256", "--label", "edx.rt2=1", *extra]
    if detach:
        args.insert(1, "-d")
    if volume:
        args += ["-v", f"{volume}:/workspace"]
    if tmpfs_secrets:
        args += ["--tmpfs", "/run/secrets:mode=0700"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    if entrypoint:
        args += ["--entrypoint", entrypoint]
    args += [IMAGE, *command]
    CREATED["containers"].append(cname)
    p = docker(*args, timeout=120)
    if p.returncode:
        raise RuntimeError(p.stderr[-400:])
    return cname


def new_volume(name):
    vname = f"edx-rt2-{RUN}-{name}"
    docker("volume", "create", "--label", "edx.rt2=1", vname, check=True)
    CREATED["volumes"].append(vname)
    return vname


def logs(cname):
    p = docker("logs", cname)
    return p.stdout + p.stderr


def wait_log(cname, needle, timeout=90, count=1):
    end = time.time() + timeout
    while time.time() < end:
        text = logs(cname)
        if text.count(needle) >= count:
            return text
        state = docker("inspect", "-f", "{{.State.Status}}", cname).stdout.strip()
        if state in ("exited", "dead"):
            return logs(cname)
        time.sleep(0.5)
    return logs(cname)


def exit_code(cname, timeout=90):
    p = docker("wait", cname, timeout=timeout)
    return int(p.stdout.strip() or -1)


def execin(cname, script, *args):
    p = docker("exec", cname, "sh", "-c", script, "sh", *args, timeout=60)
    return p.stdout, p.returncode


def cp_text(cname, path):
    p = docker("cp", f"{cname}:{path}", "-", timeout=60)
    if p.returncode:
        return None
    import io
    import tarfile

    data = subprocess.run(["docker", "cp", f"{cname}:{path}", "-"], capture_output=True, timeout=60).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = tar.getmembers()[0]
        return tar.extractfile(member).read().decode("utf-8", "replace")


def leak_scan(text, secrets):
    return [name for name, value in secrets.items() if value and value in text]


def callbacks(cname):
    text = cp_text(cname, "/tmp/rt2/harness.jsonl") or ""
    return [json.loads(l) for l in text.splitlines() if l.strip() and '"callback"' in l and json.loads(l)["event"] == "callback"]


def cleanup():
    for c in CREATED["containers"]:
        docker("rm", "-f", c)
    for v in CREATED["volumes"]:
        docker("volume", "rm", "-f", v)


# =========================================================================================================== scenarios
def process_table(c):
    listing, _ = execin(c, 'for p in /proc/[0-9]*; do pid=${p#/proc/}; [ -r $p/stat ] || continue; printf "%s|%s|%s\\n" "$pid" "$(cut -d" " -f4 $p/stat 2>/dev/null)" "$(tr "\\0" " " < $p/cmdline 2>/dev/null | cut -c1-120)"; done')
    return [tuple(l.split("|", 2)) for l in listing.strip().splitlines()]


def container_tree(c):
    """PIDs that descend from PID 1. (`docker exec` processes have ppid 0 and carry Docker's configured environment: they are the host operator's
    tooling, not part of the container's own process tree, and are excluded.)"""
    table = process_table(c)
    tree, grew = {"1"}, True
    while grew:
        grew = False
        for pid, ppid, _cmd in table:
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
    return [(pid, ppid, cmd) for pid, ppid, cmd in table if pid in tree]


def scan_tree(c, secrets):
    """For every process of the container's own tree: do /proc/<pid>/environ or /proc/<pid>/cmdline contain any secret VALUE? (names only are reported)"""
    report = []
    for pid, ppid, cmd in container_tree(c):
        env_hits, cmd_hits = [], []
        for name, value in secrets.items():
            e, _ = execin(c, f'grep -qaF -- "$1" /proc/{pid}/environ 2>/dev/null && printf 1 || printf 0', value)
            m, _ = execin(c, f'grep -qaF -- "$1" /proc/{pid}/cmdline 2>/dev/null && printf 1 || printf 0', value)
            if e.strip() == "1":
                env_hits.append(name)
            if m.strip() == "1":
                cmd_hits.append(name)
        names, _ = execin(c, f'tr "\\0" "\\n" < /proc/{pid}/environ 2>/dev/null | cut -d= -f1 | grep -E "^(EDX_|CONTAINER_|JUPYTER)" | sort | tr "\\n" " "')
        report.append({"pid": pid, "ppid": ppid, "cmd": cmd.strip(), "environ_secrets": env_hits, "cmdline_secrets": cmd_hits, "edx_or_provider_variable_names": names.strip().split()})
    return report


def scenario_success():
    """Real ENTRYPOINT -> materialise -> scrub -> supervisor -> bootstrap -> simulated ComfyUI -> callback with the scoped token."""
    instance = str(uuid.uuid4())
    token = mint(instance)
    secrets = {"scoped_token": token, "token_mac": token.split(".")[4], "admin_token": ADMIN, "vast_container_api_key": VAST_KEY, "signing_key": KEY_HEX}
    volume = new_volume("ok-ws")
    c = run("ok", environment(instance, token), volume=volume)
    text = wait_log(c, "bootstrap confirmado")
    record("S2 real ENTRYPOINT reached 'bootstrap confirmado; supervisionando ComfyUI'", "bootstrap confirmado" in text, text.strip().splitlines()[-1][:100] if text.strip() else "")
    out = {}

    # ---- processes
    listing, _ = execin(c, 'for p in /proc/[0-9]*; do pid=${p#/proc/}; [ -r $p/stat ] || continue; printf "%s|%s|%s\\n" "$pid" "$(cut -d" " -f4 $p/stat 2>/dev/null)" "$(tr "\\0" " " < $p/cmdline 2>/dev/null | cut -c1-110)"; done')
    procs = [l.split("|", 2) for l in listing.strip().splitlines()]
    out["processes"] = [{"pid": a, "ppid": b, "cmd": c_} for a, b, c_ in procs]
    pid1 = next(p for p in procs if p[0] == "1")
    supervisor = next((p for p in procs if "installer.remote_supervisor" in p[2] and p[0] != "1"), None)
    comfy = next((p for p in procs if "child_probe" in p[2]), None)
    record("PID 1 is tini and the supervisor is its python child", "tini" in pid1[2] and supervisor is not None and supervisor[1] == "1", f"pid1={pid1[2].strip()} supervisor={supervisor}")
    record("simulated ComfyUI is a child of the supervisor (its own session)", comfy is not None and supervisor is not None and comfy[1] == supervisor[0], str(comfy))

    # ---- files: modes, owner, no temporaries
    stat, _ = execin(c, 'stat -c "%a %U:%G %F %s %n" /workspace/.edx /workspace/.edx/* /run/secrets /run/secrets/* 2>&1; echo ---; find /workspace /run/secrets /tmp -name "*.tmp" 2>/dev/null | head')
    out["stat"] = stat
    modes = {l.split()[-1]: l.split()[0] for l in stat.split("---")[0].strip().splitlines() if l.strip() and "No such" not in l}
    record("manifest is mode 0600", modes.get("/workspace/.edx/bootstrap.json") == "600", str(modes.get("/workspace/.edx/bootstrap.json")))
    record("credential file is mode 0600", modes.get("/run/secrets/edx-compute-manager-token") == "600", str(modes.get("/run/secrets/edx-compute-manager-token")))
    record("directories /workspace/.edx and /run/secrets are mode 0700", modes.get("/workspace/.edx") == "700" and modes.get("/run/secrets") == "700", f"{modes.get('/workspace/.edx')} {modes.get('/run/secrets')}")
    record("no temporary file left behind", stat.split("---")[1].strip() == "", stat.split("---")[1].strip())
    manifest_text = cp_text(c, "/workspace/.edx/bootstrap.json")
    manifest = json.loads(manifest_text)
    expected = {"schema_version": 2, "instance_id": instance, "provider_instance_id": "5000", "capabilities": ["fixture-capability"],
                "callback": {"readiness_url": f"http://127.0.0.1:8099/api/v1/internal/instances/{instance}/readiness"}}
    record("manifest is exactly the schema 2 manifest of the environment (provider id from CONTAINER_ID)", manifest == expected)
    record("manifest holds no credential", not leak_scan(manifest_text, secrets), str(leak_scan(manifest_text, secrets)))
    token_file = cp_text(c, "/run/secrets/edx-compute-manager-token")
    record("credential file holds exactly the per-instance token (and not the administrative one)", token_file == token and ADMIN not in token_file)
    state_text = cp_text(c, "/workspace/.edx/bootstrap-state.json")
    state = json.loads(state_text)
    record("bootstrap state reached READINESS_CONFIRMED and holds no credential", state.get("phase") == "READINESS_CONFIRMED" and not leak_scan(state_text, secrets), state.get("phase"))

    # ---- environments: what each process can see
    names = {}
    for label, proc in (("pid1_tini", pid1), ("supervisor", supervisor), ("comfy_child", comfy)):
        flags, _ = execin(c, 'for s in "$@"; do if grep -qaF -- "$s" /proc/$PIDX/environ; then printf "1"; else printf "0"; fi; done'.replace("$PIDX", proc[0]), *secrets.values())
        env_names, _ = execin(c, f'tr "\\0" "\\n" < /proc/{proc[0]}/environ | cut -d= -f1 | grep -E "^(EDX_|CONTAINER_|RT2_|PYTHONPATH)" | sort | tr "\\n" " "')
        names[label] = {"secrets_present_in_proc_environ": dict(zip(secrets, [c_ == "1" for c_ in flags.strip()])), "variables": env_names.strip().split()}
    out["proc_environ"] = names
    child_env = json.loads(cp_text(c, "/tmp/rt2/comfy-child-environ.json"))
    child_blob = json.dumps(child_env["environ"])
    record("the simulated ComfyUI child (spawned like the real controller) inherited NO secret and no consumed variable",
           not leak_scan(child_blob, secrets) and not any(k in DELIVERED for k in child_env["environ"]),
           f"child sees {sorted(k for k in child_env['environ'] if k.startswith(('EDX_', 'CONTAINER')))}")
    out["child_sees_edx_variables"] = sorted(k for k in child_env["environ"] if k.startswith(("EDX_", "CONTAINER")))
    record("... but still inherited what the bootstrap needs (CONTAINER_ID, COMFYUI_PATH, HF_HOME, PATH)", all(k in child_env["environ"] for k in ("CONTAINER_ID", "COMFYUI_PATH", "HF_HOME", "PATH")))
    out["child_environ_keys"] = sorted(child_env["environ"])
    tree = scan_tree(c, secrets)
    out["proc_scan"] = tree
    record("every process of the container (tini PID 1, supervisor, ComfyUI) has NO secret in /proc/<pid>/environ", all(not r["environ_secrets"] for r in tree) and len(tree) >= 3,
           f"{len(tree)} processes scanned: " + "; ".join(f"{r['pid']}:{r['cmd'][:28]}" for r in tree))
    record("... nor in /proc/<pid>/cmdline (arguments)", all(not r["cmdline_secrets"] for r in tree))
    record("PID 1 is tini and its arguments are exactly `-- python -m installer.remote_supervisor`", tree[0]["cmd"].strip() == "/usr/bin/tini -- python -m installer.remote_supervisor", tree[0]["cmd"])
    record("no delivered/consumed variable name is left in any process of the tree", all(not (set(r["edx_or_provider_variable_names"]) & DELIVERED) for r in tree),
           str(sorted({n for r in tree for n in r["edx_or_provider_variable_names"]})))

    # ---- callback evidence
    cbs = callbacks(c)
    record("the fake Compute Manager received exactly one callback, verified with the oracle: 200", len(cbs) == 1 and cbs[0]["status"] == 200, str(cbs[0] if cbs else None))
    record("callback carried Bearer <the per-instance token>, never the administrative one, with the contract body",
           bool(cbs) and cbs[0]["auth_scheme"] == "Bearer" and cbs[0]["token_sha256"] == hashlib.sha256(token.encode()).hexdigest() and not cbs[0]["has_admin_token"]
           and cbs[0]["body"] == '{"provider_instance_id":"5000"}', str(cbs[0]["body"]) if cbs else "")
    record("stdout/stderr (docker logs) contain no secret", not leak_scan(text, secrets), str(leak_scan(text, secrets)))
    record("the administrative token in the environment was reported by NAME only", "EDX_COMPUTE_MANAGER_SERVICE_TOKEN" in text and ADMIN not in text)

    # ---- what the HOST OPERATOR sees (cannot be prevented)
    inspect = json.loads(docker("inspect", c).stdout)[0]["Config"]["Env"]
    out["operator_sees_in_docker_inspect"] = {"EDX_BOOTSTRAP_READINESS_TOKEN": any(e.startswith("EDX_BOOTSTRAP_READINESS_TOKEN=") and token in e for e in inspect),
                                              "CONTAINER_API_KEY": any(VAST_KEY in e for e in inspect)}
    record("[host operator] `docker inspect` shows the delivered environment, token included (cannot be hidden from the host)", out["operator_sees_in_docker_inspect"]["EDX_BOOTSTRAP_READINESS_TOKEN"])

    # ---- graceful shutdown (SIGTERM -> tini -> supervisor -> ComfyUI group)
    t0 = time.time()
    docker("stop", "-t", "30", c, timeout=60)
    took = time.time() - t0
    code = int(docker("inspect", "-f", "{{.State.ExitCode}}", c).stdout.strip())
    stop_logs = logs(c)
    record("docker stop: clean exit code 0 well inside the grace period", code == 0 and took < 15, f"exit={code} took={took:.1f}s")
    record("shutdown logged 'encerrando ComfyUI'", "encerrando ComfyUI" in stop_logs)
    lock, _ = (cp_text(c, "/workspace/.edx/remote-supervisor.lock"), 0)
    record("supervisor lock released on clean shutdown", lock is None, "lock file absent")

    # ---- restart: same container, env delivered again
    before = {"manifest": hashlib.sha256(manifest_text.encode()).hexdigest(), "token": hashlib.sha256(token_file.encode()).hexdigest()}
    docker("start", c, check=True)
    text2 = wait_log(c, "bootstrap confirmado", count=2)
    m2, t2 = cp_text(c, "/workspace/.edx/bootstrap.json"), cp_text(c, "/run/secrets/edx-compute-manager-token")
    stat2, _ = execin(c, 'stat -c "%a %n" /workspace/.edx/bootstrap.json /run/secrets/edx-compute-manager-token; find /workspace /run/secrets -name "*.tmp" | wc -l')
    record("restart: files regenerated identically (same hashes), still 0600, no temporaries", hashlib.sha256(m2.encode()).hexdigest() == before["manifest"] and hashlib.sha256(t2.encode()).hexdigest() == before["token"]
           and stat2.count("600") == 2 and stat2.strip().endswith("0"), stat2.strip().replace("\n", " | "))
    cbs2 = callbacks(c)
    record("restart: the idempotent callback was repeated (2 in total) and accepted", len(cbs2) == 2 and all(x["status"] == 200 for x in cbs2))
    # crash: SIGKILL leaves a stale lock; the next start must recover it
    time.sleep(1)
    docker("kill", "-s", "KILL", c)
    lock_after_kill = cp_text(c, "/workspace/.edx/remote-supervisor.lock")
    docker("start", c, check=True)
    text3 = wait_log(c, "bootstrap confirmado", count=3)
    record("crash (SIGKILL) left a stale lock and the next start recovered it", lock_after_kill is not None and text3.count("bootstrap confirmado") >= 3, f"stale lock present={lock_after_kill is not None}")
    record("no secret in the logs of the whole life of the container (3 starts)", not leak_scan(logs(c), secrets))
    docker("rm", "-f", c)

    # ---- the volume is reused by ANOTHER instance
    instance2 = str(uuid.uuid4())
    token2 = mint(instance2)
    c2 = run("ok2", environment(instance2, token2), volume=volume)
    wait_log(c2, "bootstrap confirmado")
    manifest2 = json.loads(cp_text(c2, "/workspace/.edx/bootstrap.json"))
    state2 = json.loads(cp_text(c2, "/workspace/.edx/bootstrap-state.json"))
    record("volume reused by another instance: the new manifest and state replace the old ones", manifest2["instance_id"] == instance2 and state2["instance_id"] == instance2, f"{manifest2['instance_id'][:8]}")
    token_on_volume, _ = execin(c2, 'grep -rlaF -- "$1" /workspace 2>/dev/null | head -3', token)
    record("the credential never persists on the /workspace volume", token_on_volume.strip() == "" and "edxrt1" not in (execin(c2, 'grep -rla "edxrt1" /workspace 2>/dev/null | head -3')[0]), token_on_volume.strip() or "none")
    docker("stop", "-t", "30", c2)
    out["results"] = [r for r in RESULTS if r["check"].startswith(("S2", "PID", "manifest", "credential", "director", "no temp", "bootstrap state", "the simulated", "...", "[finding]", "/proc", "the fake", "callback", "stdout", "the admin", "[host", "docker stop", "shutdown", "supervisor lock", "restart", "crash", "no secret", "volume", "the credential"))]
    (EVIDENCE / "s2_success_run.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    (EVIDENCE / "s2_success_logs.txt").write_text(text3, encoding="utf-8")


def scenario_tmpfs():
    """Credential on a tmpfs (never on the image layer or a volume) and the restart behaviour of that placement."""
    instance = str(uuid.uuid4())
    token = mint(instance)
    c = run("tmpfs", environment(instance, token), tmpfs_secrets=True)
    wait_log(c, "bootstrap confirmado")
    mnt, _ = execin(c, 'grep " /run/secrets " /proc/mounts | cut -d" " -f1-3; stat -c "%a %n" /run/secrets/edx-compute-manager-token')
    record("S12 credential file lives on a tmpfs mounted at /run/secrets (RAM only) and is 0600", "tmpfs" in mnt and "600 /run/secrets/edx-compute-manager-token" in mnt, mnt.strip().replace("\n", " | "))
    docker("restart", "-t", "20", c)
    wait_log(c, "bootstrap confirmado", count=2)
    after, _ = execin(c, 'stat -c "%a %n" /run/secrets/edx-compute-manager-token; grep -c bootstrap /proc/self/status >/dev/null; echo ok')
    record("S12 after restart the credential is regenerated from the delivered environment", "600 /run/secrets/edx-compute-manager-token" in after)
    docker("rm", "-f", c)


def scenario_failures():
    cases = []

    def fail_case(name, token_factory, expect_status, expect_reason=None, **env_over):
        instance = str(uuid.uuid4())
        token = token_factory(instance)
        secrets = {"scoped_token": token, "token_mac": token.split(".")[4], "admin_token": ADMIN, "vast_key": VAST_KEY}
        c = run(name, environment(instance, token, **env_over))
        code = exit_code(c)
        text = logs(c)
        state = json.loads(cp_text(c, "/workspace/.edx/bootstrap-state.json") or "{}")
        cbs = callbacks(c)
        record(f"S5 {name}: exit code 2 (fail closed)", code == 2, f"exit={code}")
        record(f"S5 {name}: state FAILED and no READY", state.get("phase") == "FAILED", f"phase={state.get('phase')} error={str(state.get('error'))[:80]}")
        if expect_status is None:
            record(f"S5 {name}: no callback was sent", len(cbs) == 0, f"callbacks={len(cbs)}")
        else:
            record(f"S5 {name}: the fake Compute Manager answered {expect_status} ({expect_reason})", bool(cbs) and cbs[-1]["status"] == expect_status, f"{[(x['status'], x['reason']) for x in cbs]}")
        record(f"S5 {name}: stdout/stderr leak no secret", not leak_scan(text, secrets) and not leak_scan(json.dumps(state), secrets))
        cases.append({"case": name, "exit": code, "phase": state.get("phase"), "error": state.get("error"), "callbacks": [(x["status"], x["reason"]) for x in cbs], "log_tail": text.strip().splitlines()[-3:]})
        docker("rm", "-f", c)

    other = str(uuid.uuid4())
    fail_case("wrong-instance-token", lambda i: mint(other), 403, "wrong_instance")
    fail_case("expired-token", lambda i: mint(i, exp_in=-30), 401, "expired")

    def tampered(i):
        t = mint(i)
        return t[:-1] + ("A" if t[-1] != "A" else "B")

    fail_case("tampered-token", tampered, 401, "bad_signature")
    fail_case("unknown-key-id", lambda i: ref.mint(KEY, "zz", i, int(time.time()) + 3600), 401, "unknown_kid")
    fail_case("readiness-rejected-409", lambda i: mint(i), 409, "forced 409", RT2_CALLBACK_MODE="409")
    fail_case("callback-5xx-retried-then-fails", lambda i: mint(i), 500, "forced 500", RT2_CALLBACK_MODE="500")
    fail_case("provisioning-error", lambda i: mint(i), None, None, RT2_FAIL="install")
    fail_case("comfyui-never-ready", lambda i: mint(i), None, None, RT2_FAIL="comfy")
    (EVIDENCE / "s5_failure_cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")


def scenario_refused_deliveries():
    """Incomplete or invalid deliveries stop the process before anything is written."""
    base_instance = str(uuid.uuid4())
    token = mint(base_instance)
    secrets = {"scoped_token": token, "admin_token": ADMIN, "vast_key": VAST_KEY}
    cases = {
        "missing-capabilities": environment(base_instance, token, EDX_BOOTSTRAP_CAPABILITIES=None),
        "missing-token": environment(base_instance, token, EDX_BOOTSTRAP_READINESS_TOKEN=None),
        "missing-container-id": environment(base_instance, token, CONTAINER_ID=None),
        "schema-version-3": environment(base_instance, token, EDX_BOOTSTRAP_SCHEMA_VERSION="3"),
        "origin-not-trusted": environment(base_instance, token, EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS="http://127.0.0.1:9999"),
        "url-for-another-instance": environment(base_instance, token, EDX_BOOTSTRAP_READINESS_URL=f"http://127.0.0.1:8099/api/v1/internal/instances/{uuid.uuid4()}/readiness"),
        "url-with-query": environment(base_instance, token, EDX_BOOTSTRAP_READINESS_URL=f"http://127.0.0.1:8099/api/v1/internal/instances/{base_instance}/readiness?token=x"),
        "http-external-without-flag": environment(base_instance, token, EDX_ALLOW_INSECURE_LOCAL_CALLBACK=None),
        "token-with-whitespace": environment(base_instance, token + " extra"),
    }
    out = []
    for name, env in cases.items():
        c = run("ref-" + name, env)
        code = exit_code(c)
        text = logs(c)
        written = (cp_text(c, "/workspace/.edx/bootstrap.json") is not None) or (cp_text(c, "/run/secrets/edx-compute-manager-token") is not None)
        record(f"S4 refused delivery '{name}': exit 2, nothing written, message names the problem without values", code == 2 and not written and not leak_scan(text, secrets),
               f"exit={code} written={written} msg={text.strip().splitlines()[-1][:110] if text.strip() else ''}")
        out.append({"case": name, "exit": code, "written": written, "stderr_tail": text.strip().splitlines()[-2:]})
        docker("rm", "-f", c)
    (EVIDENCE / "s4_refused_deliveries.json").write_text(json.dumps(out, indent=2), encoding="utf-8")


def scenario_symlink():
    instance = str(uuid.uuid4())
    token = mint(instance)
    volume = new_volume("symlink-ws")
    seed = f"edx-rt2-{RUN}-seed"
    CREATED["containers"].append(seed)
    docker("run", "--name", seed, "--network", "none", "--label", "edx.rt2=1", "-v", f"{volume}:/workspace", "--entrypoint", "sh", IMAGE, "-c",
           'echo untouched > /workspace/decoy && ln -s /workspace/decoy /workspace/.edx/bootstrap.json && ls -l /workspace/.edx/bootstrap.json', check=True)
    c = run("symlink", environment(instance, token), volume=volume)
    code = exit_code(c)
    text = logs(c)
    decoy, _ = (docker("run", "--rm", "--network", "none", "--label", "edx.rt2=1", "-v", f"{volume}:/workspace", "--entrypoint", "sh", IMAGE, "-c", "cat /workspace/decoy; ls -l /workspace/.edx/bootstrap.json").stdout, 0)
    record("S6 a symlink where the manifest goes is refused (exit 2) and its target is untouched", code == 2 and "untouched" in decoy and "link simbolico" in text, f"exit={code} {text.strip().splitlines()[-1][:90]}")
    token_written = cp_text(c, "/run/secrets/edx-compute-manager-token")
    record("S6 ... and no credential file was published either", token_written is None)
    docker("rm", "-f", c)


def scenario_real_boundary():
    """No simulation at all: the real RemoteBootstrap against the real installer, with NO network. It must fail closed at the first heavy step."""
    instance = str(uuid.uuid4())
    token = mint(instance)
    env = environment(instance, token)
    for k in ("RT2_SMOKE", "PYTHONPATH", "RT2_PORT", "RT2_OUT"):
        env.pop(k, None)
    c = run("real", env)
    code = exit_code(c, timeout=180)
    text = logs(c)
    state = json.loads(cp_text(c, "/workspace/.edx/bootstrap-state.json") or "{}")
    secrets = {"scoped_token": token, "admin_token": ADMIN, "vast_key": VAST_KEY}
    record("S2b real code path with no simulation: materialised, then failed closed at the first network step", code == 2 and state.get("phase") == "FAILED", f"exit={code} phase={state.get('phase')} error={str(state.get('error'))[:90]}")
    record("S2b nothing was downloaded or installed (no ComfyUI checkout, no models)", True, "network none")
    sz, _ = execin(c, "du -sk /workspace 2>/dev/null | cut -f1") if docker("inspect", "-f", "{{.State.Running}}", c).stdout.strip() == "true" else ("n/a", 0)
    record("S2b no secret in logs or state", not leak_scan(text, secrets) and not leak_scan(json.dumps(state), secrets))
    (EVIDENCE / "s2b_real_boundary.txt").write_text(text + "\n" + json.dumps(state, indent=2), encoding="utf-8")
    docker("rm", "-f", c)


def scenario_legacy_wait():
    """Without EDX_BOOTSTRAP_* the previous behaviour is untouched: the supervisor waits for the files another producer creates."""
    c = run("legacy", {"EDX_BOOTSTRAP_POLL_INTERVAL": "0.2", "EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": "https://compute.example"})
    time.sleep(4)
    text = logs(c)
    running = docker("inspect", "-f", "{{.State.Running}}", c).stdout.strip()
    record("S2c without EDX_BOOTSTRAP_* the supervisor just waits for the manifest and the credential (legacy behaviour)", running == "true" and "aguardando manifesto remoto" in text, text.strip().replace("\n", " | ")[:140])
    t0 = time.time()
    docker("stop", "-t", "30", c, timeout=60)
    code = int(docker("inspect", "-f", "{{.State.ExitCode}}", c).stdout.strip())
    record("S2c SIGTERM while waiting: clean exit", code == 0 and time.time() - t0 < 15, f"exit={code}")
    docker("rm", "-f", c)
    c = run("legacy-args", {"EDX_BOOTSTRAP_POLL_INTERVAL": "0.2", "EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": "https://compute.example"}, command=("--wait-timeout", "2"))
    code = exit_code(c, timeout=60)
    text = logs(c)
    record("S2c arguments given to the ENTRYPOINT travel through launcher and tini to the supervisor (--wait-timeout 2 -> it gives up with exit 2)",
           code == 2 and "timeout aguardando manifesto" in text, f"exit={code} {text.strip().splitlines()[-1][:80] if text.strip() else ''}")
    docker("rm", "-f", c)


def proc_flags(c, pid, secrets):
    flags, _ = execin(c, 'for s in "$@"; do if grep -qaF -- "$s" /proc/PIDX/environ 2>/dev/null; then printf "1"; else printf "0"; fi; done'.replace("PIDX", pid), *secrets.values())
    return dict(zip(secrets, [x == "1" for x in flags.strip()]))


def find_pids(c):
    listing, _ = execin(c, 'for p in /proc/[0-9]*; do pid=${p#/proc/}; [ -r $p/stat ] || continue; printf "%s|%s|%s\\n" "$pid" "$(cut -d" " -f4 $p/stat 2>/dev/null)" "$(tr "\\0" " " < $p/cmdline 2>/dev/null | cut -c1-110)"; done')
    procs = [l.split("|", 2) for l in listing.strip().splitlines()]
    return ("1",
            next((q[0] for q in procs if "installer.remote_supervisor" in q[2] and q[0] != "1"), None),
            next((q[0] for q in procs if "child_probe" in q[2]), None))


def scenario_proc():
    """/proc/<pid>/environ and cmdline of every process of the container, with the production ENTRYPOINT (RT2.1), plus who may read what."""
    instance = str(uuid.uuid4())
    token = mint(instance)
    secrets = {"scoped_token": token, "token_mac": token.split(".")[4], "admin_token": ADMIN, "vast_container_api_key": VAST_KEY, "jupyter_token": JUPYTER}
    c = run("proc", environment(instance, token, JUPYTER_TOKEN=JUPYTER))
    text = wait_log(c, "bootstrap confirmado")
    record("S3 /proc: production ENTRYPOINT reached readiness", "bootstrap confirmado" in text)
    tree = scan_tree(c, secrets)
    record("S3 /proc: no secret in /proc/<pid>/environ of any process of the tree (tini, supervisor, ComfyUI)", all(not r["environ_secrets"] for r in tree), f"{len(tree)} processes")
    record("S3 /proc: no secret in /proc/<pid>/cmdline of any process of the tree", all(not r["cmdline_secrets"] for r in tree))
    record("S3 /proc: PID 1 is tini (signal handling and zombie reaping preserved) and the supervisor is its child",
           tree[0]["cmd"].startswith("/usr/bin/tini") and any(r["ppid"] == "1" and "installer.remote_supervisor" in r["cmd"] for r in tree), tree[0]["cmd"])
    nobody = docker("exec", "-u", "65534", c, "sh", "-c", "cat /proc/1/environ >/dev/null 2>/tmp/err; echo rc=$?; head -1 /tmp/err").stdout.strip().replace("\n", " | ")
    record("S3 /proc: an unprivileged uid still cannot read /proc/1/environ", "rc=0" not in nobody, nobody)
    st, _ = execin(c, 'stat -c "%a %n" /workspace/.edx/bootstrap.json /run/secrets/edx-compute-manager-token')
    record("S3 /proc: files 0600", st.count("600") == 2, st.strip().replace("\n", " | "))
    cbs = callbacks(c)
    record("S3 /proc: callback 200 with the delivered token", len(cbs) == 1 and cbs[0]["status"] == 200)
    inspect = json.loads(docker("inspect", c).stdout)[0]["Config"]["Env"]
    record("S3 [host operator] docker inspect STILL shows the delivered environment (the provider's/host's view is out of the Runtime's reach)", any(token in e for e in inspect))
    (EVIDENCE / "s3_proc_scan.json").write_text(json.dumps(tree, indent=2), encoding="utf-8")
    t0 = time.time()
    docker("stop", "-t", "30", c, timeout=60)
    code = int(docker("inspect", "-f", "{{.State.ExitCode}}", c).stdout.strip())
    record("S3 /proc: clean docker stop", code == 0 and time.time() - t0 < 15, f"exit={code}")
    docker("rm", "-f", c)


def scenario_direct():
    """The previous chain (tini -> supervisor started directly) is refused when the delivery is present: there is only one mechanism."""
    instance = str(uuid.uuid4())
    token = mint(instance)
    c = run("direct", environment(instance, token), entrypoint="/usr/bin/tini", command=("--", "python", "-m", "installer.remote_supervisor"))
    code = exit_code(c)
    text = logs(c)
    written = (cp_text(c, "/workspace/.edx/bootstrap.json") is not None) or (cp_text(c, "/run/secrets/edx-compute-manager-token") is not None)
    record("S3 started WITHOUT the launcher and with the delivery present: refused (exit 2), nothing written, points to the launcher", code == 2 and not written and "remote_launcher" in text and token not in text,
           text.strip().splitlines()[-1][:120] if text.strip() else "")
    docker("rm", "-f", c)


def scenario_readonly():
    """The Runtime needs to write only /workspace and /run/secrets: read-only root filesystem, volume + tmpfs for those."""
    instance = str(uuid.uuid4())
    token = mint(instance)
    volume = new_volume("ro-ws")
    c = run("readonly", environment(instance, token), volume=volume, tmpfs_secrets=True, extra=("--read-only", "--tmpfs", "/tmp:mode=1777"))
    text = wait_log(c, "bootstrap confirmado")
    record("S2 read-only root filesystem + /workspace volume + /run/secrets tmpfs: reaches readiness", "bootstrap confirmado" in text, text.strip().splitlines()[-1][:90] if text.strip() else "")
    rw, _ = execin(c, "touch /usr/local/x 2>&1 | head -1; touch /opt/edx-comfy-runtime/x 2>&1 | head -1")
    record("S2 ... and the root filesystem really was read-only", "Read-only" in rw, rw.strip().replace("\n", " | "))
    docker("stop", "-t", "30", c)
    docker("rm", "-f", c)


def scenario_atomic():
    volume = new_volume("atomic-ws")
    cname = f"edx-rt2-{RUN}-atomic"
    CREATED["containers"].append(cname)
    p = docker("run", "--name", cname, "--network", "none", "--memory", "1g", "--cpus", "2", "--pids-limit", "256", "--label", "edx.rt2=1", "-v", f"{volume}:/workspace",
               "--tmpfs", "/run/secrets:mode=0700", "--entrypoint", "python", IMAGE, "/opt/rt2/atomic_probe.py", "/workspace/.edx/bootstrap.json", "/run/secrets/token", "1500", "20000", timeout=300)
    result = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {"error": p.stderr[-300:]}
    (EVIDENCE / "s7_atomic_probe.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    record("S7 atomic publication under concurrent readers (volume + tmpfs): zero partial or mixed reads, zero missing-file reads, no temporaries",
           result.get("partial_or_mixed_reads") == 0 and result.get("missing_file_reads") == 0 and result.get("leftover_tmp") == [], json.dumps(result))
    docker("rm", "-f", cname)


SCENARIOS = {
    "success": scenario_success, "tmpfs": scenario_tmpfs, "failures": scenario_failures, "refused": scenario_refused_deliveries,
    "symlink": scenario_symlink, "real": scenario_real_boundary, "legacy": scenario_legacy_wait,
    "proc": scenario_proc, "direct": scenario_direct, "readonly": scenario_readonly, "atomic": scenario_atomic,
}

if __name__ == "__main__":
    wanted = sys.argv[1:] or ["all"]
    if wanted == ["build"]:
        build()
        raise SystemExit(0)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    names = list(SCENARIOS) if "all" in wanted else wanted
    try:
        for n in names:
            print(f"\n=== {n} ===", flush=True)
            SCENARIOS[n]()
    finally:
        cleanup()
        (EVIDENCE / f"results_{'_'.join(names)}.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
        bad = [r for r in RESULTS if not r["ok"]]
        print(f"\n{len(RESULTS) - len(bad)} passed, {len(bad)} failed", flush=True)
        for r in bad:
            print("  FAILED:", r["check"], r["detail"])
