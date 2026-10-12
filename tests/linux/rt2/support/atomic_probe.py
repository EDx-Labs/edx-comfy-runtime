"""Concurrent reader/writer over the REAL materialiser on the container filesystem: a reader must never see a partial or mixed file."""
import json
import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/edx-comfy-runtime")
from installer.remote_env import materialize  # noqa: E402

INSTANCE = "11111111-1111-4111-8111-111111111111"
ORIGIN = "https://compute.example.test"
TOKEN = "edxrt1." + INSTANCE + ".1760000000.k1." + "A" * 43
MANIFEST, CREDENTIAL = Path(sys.argv[1]), Path(sys.argv[2])
WRITES, READS = int(sys.argv[3]), int(sys.argv[4])


def env(caps):
    return {"EDX_BOOTSTRAP_SCHEMA_VERSION": "2", "EDX_BOOTSTRAP_INSTANCE_ID": INSTANCE, "EDX_BOOTSTRAP_CAPABILITIES": caps,
            "EDX_BOOTSTRAP_READINESS_URL": f"{ORIGIN}/api/v1/internal/instances/{INSTANCE}/readiness", "EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": ORIGIN,
            "EDX_BOOTSTRAP_READINESS_TOKEN": TOKEN, "CONTAINER_ID": "5000"}


def writer(stop):
    for i in range(WRITES):
        materialize(env("alpha,beta" if i % 2 else "gamma"), MANIFEST, CREDENTIAL)
    stop.set()


def reader(stop, out):
    seen = bad = missing = 0
    while True:
        for path in (MANIFEST, CREDENTIAL):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                missing += 1
                continue
            seen += 1
            try:
                if path == MANIFEST:
                    ok = json.loads(data)["capabilities"] in (["alpha", "beta"], ["gamma"])
                else:
                    ok = data.decode() == TOKEN
            except Exception:
                ok = False
            bad += not ok
        if stop.is_set() and seen >= READS:
            break
    out.put((seen, bad, missing))


if __name__ == "__main__":
    materialize(env("gamma"), MANIFEST, CREDENTIAL)
    stop, out = multiprocessing.Event(), multiprocessing.Queue()
    readers = [multiprocessing.Process(target=reader, args=(stop, out)) for _ in range(3)]
    w = multiprocessing.Process(target=writer, args=(stop,))
    t0 = time.time()
    for p in readers + [w]:
        p.start()
    results = [out.get() for _ in readers]
    for p in readers + [w]:
        p.join()
    print(json.dumps({
        "writes": WRITES, "reads": sum(r[0] for r in results), "partial_or_mixed_reads": sum(r[1] for r in results),
        "missing_file_reads": sum(r[2] for r in results), "seconds": round(time.time() - t0, 1),
        "leftover_tmp": sorted(p.name for d in (MANIFEST.parent, CREDENTIAL.parent) for p in d.iterdir() if p.name.endswith(".tmp")),
    }))
