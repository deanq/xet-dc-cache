# Verified against the installed `runpod-flash` package (Endpoint signature
# inspected live via `inspect.signature`); no deltas from the researched shape —
# cpu/datacenter/workers/idle_timeout/dependencies/env all match, and
# DataCenter.EU_RO_1 exists as spelled.
import os
from runpod_flash import Endpoint, DataCenter
# Flash packages this worker/ dir as the deploy root, so timing.py is a
# top-level sibling here — NOT importable as runpod_testbed.worker.timing.
from timing import run_download, hf_download

# Pin >= the versions that honor HF_ENDPOINT for Xet xorb fetches. Flash's base
# image ships huggingface_hub 1.6.0 + hf_xet 1.3.2, and hf_xet 1.3.2 pulls xorbs
# straight from the CAS (bypassing the shim); >=1.6.0 uses the rewritten URLs.
_DEPS = os.environ.get("WORKER_DEPS", "huggingface_hub>=1.32.0,hf_xet>=1.6.0").split(",")
_CPU = os.environ.get("WORKER_CPU", "cpu5c-4-8")
_MAX = int(os.environ.get("WORKER_MAX", "4"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")
_UPGRADED: list = []  # once-flag for the runtime hf_xet upgrade workaround (test)

def _mk(name: str, pod_addr: str):
    # The generated deployed handler (build_utils/handler_generator.py) resolves
    # this function as `flash_app.<func_name>` and CALLS it as `func(**job_input)`
    # — the job's `input` dict is splatted as kwargs, not passed as one arg. So:
    #  (1) the module-level binding MUST be named exactly `func_name` (the handler
    #      __name__), or `getattr(flash_app, func_name)` raises AttributeError at
    #      handler import → the worker exits 1 before any job runs; and
    #  (2) the handler must accept the input keys as kwargs, hence **payload.
    async def handler(**payload) -> dict:
        # Flash's base image ships hf_xet 1.3.2, which fetches Xet xorbs DIRECTLY
        # from the CAS and bypasses the shim — it ignores the reconstruction
        # manifest's rewritten (HF_ENDPOINT) xorb URLs. WORKER_DEPS does not
        # upgrade the base's pre-installed version, so force-upgrade once at first
        # invocation, BEFORE huggingface_hub is first imported (timing.hf_download
        # imports it lazily), so the new hf_xet takes effect. Verified end-to-end:
        # hf_xet>=1.6.0 routes xorbs through the shim (served_bytes/hits GB-scale,
        # cross-pod peering). Proper fix belongs upstream (Flash bumping hf_xet).
        import time
        cold = not _UPGRADED
        dep_upgrade_ms = 0
        if cold:
            import subprocess, sys
            t0 = time.monotonic()
            subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade",
                            "huggingface_hub>=1.32.0", "hf_xet>=1.6.0"],
                           check=False, capture_output=True)
            dep_upgrade_ms = round((time.monotonic() - t0) * 1000)
            _UPGRADED.append(True)
        return run_download(payload, hf_download,
                            cold_first_invocation=cold, dep_upgrade_ms=dep_upgrade_ms)
    # __name__ drives both the manifest function name and the getattr above, and
    # must be unique across A/B/C (the scanner rejects duplicate names). Rename
    # BEFORE Endpoint wraps it (a post-decoration rename lands on the wrapper).
    handler.__name__ = handler.__qualname__ = name.replace("-", "_")
    return Endpoint(name=name, cpu=_CPU, datacenter=DataCenter.EU_RO_1,
                    workers=(0, _MAX), idle_timeout=30, dependencies=_DEPS,
                    env={"HF_ENDPOINT": pod_addr, "HF_TOKEN": _HF_TOKEN})(handler)

# up.py exports POD_ADDR_A/B/C for the `flash deploy` process, where _mk reads
# them to bake each endpoint's HF_ENDPOINT env. They are ABSENT at worker
# runtime (the deployed worker's env carries only HF_ENDPOINT/HF_TOKEN), yet the
# worker re-imports this whole module to locate the handler — so reading
# os.environ["POD_ADDR_A"] here crashes every worker at import (exit code 1).
# Default to "" so runtime import is safe; the value is unused at runtime (the
# handler downloads through HF_ENDPOINT, already set on the endpoint). At deploy
# time the real addrs are present, so HF_ENDPOINT is still baked correctly.
#
# The binding names MUST equal each handler's __name__ (xet_dl_A/B/C) — the
# generated handler does `importlib.import_module('flash_app').xet_dl_A`.
xet_dl_A = _mk("xet-dl-A", os.environ.get("POD_ADDR_A", ""))
xet_dl_B = _mk("xet-dl-B", os.environ.get("POD_ADDR_B", ""))
xet_dl_C = _mk("xet-dl-C", os.environ.get("POD_ADDR_C", ""))
