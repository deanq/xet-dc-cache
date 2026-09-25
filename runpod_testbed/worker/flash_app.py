# Verified against the installed `runpod-flash` package (Endpoint signature
# inspected live via `inspect.signature`): cpu/datacenter/workers/idle_timeout/
# dependencies/env/volume all exist; DataCenter.EU_RO_1 exists as spelled.
import os
from runpod_flash import Endpoint, DataCenter
from runpod_flash.core.resources.network_volume import NetworkVolume
# Flash packages this worker/ dir as the deploy root, so timing.py / plan.py are
# top-level siblings here — NOT importable as runpod_testbed.worker.*.
from timing import run_download, hf_download, volumecache_download, modelstore_local_read
from plan import plan_endpoints, upgrade_pkgs

# Pin >= the versions that honor HF_ENDPOINT for Xet xorb fetches. Flash's base
# image ships huggingface_hub 1.6.0 + hf_xet 1.3.2, and hf_xet 1.3.2 pulls xorbs
# straight from the CAS (bypassing the shim); >=1.6.0 uses the rewritten URLs.
_DEPS = os.environ.get("WORKER_DEPS", "huggingface_hub>=1.32.0,hf_xet>=1.6.0").split(",")
_CPU = os.environ.get("WORKER_CPU", "cpu5c-4-8")
_MAX = int(os.environ.get("WORKER_MAX", "4"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")
_IDLE_TIMEOUT_S = 30
# MECHANISM/MODELS are present at deploy time (provision/flash.py::deploy_env)
# AND at worker runtime (baked into every endpoint's env by plan_endpoints), so
# the same bindings exist in both imports. Default "shim" keeps a bare import safe.
_MECHANISM = os.environ.get("MECHANISM", "shim")
_MODELS = [m for m in os.environ.get("MODELS", "").split(",") if m]
_UPGRADED: list = []  # once-flag for the runtime hf_xet upgrade workaround

_DOWNLOADERS = {"shim": hf_download, "baseline": hf_download,
                "volumecache": volumecache_download, "modelstore": modelstore_local_read}


def _upgrade_once(pkgs: list[str]) -> tuple[bool, int]:
    # WORKER_DEPS does not upgrade the Flash base image's pre-installed packages,
    # so force-upgrade the ones this downloader needs once at first invocation,
    # BEFORE they are lazily imported (timing.hf_download / volumecache_download
    # import huggingface_hub / runpod.serverless lazily). See plan.upgrade_pkgs.
    import importlib, subprocess, sys, time
    if _UPGRADED or not pkgs:
        return False, 0
    t0 = time.monotonic()
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *pkgs],
                   check=False, capture_output=True)
    # The runpod serverless harness imports runpod.serverless before our handler,
    # so the pre-upgrade module is cached; drop it so the lazy VolumeCache import
    # loads the freshly-installed files.
    if any(p.startswith("runpod") for p in pkgs):
        importlib.invalidate_caches()
        for name in [m for m in sys.modules if m == "runpod" or m.startswith("runpod.")]:
            del sys.modules[name]
    _UPGRADED.append(True)
    return True, round((time.monotonic() - t0) * 1000)


def _mk(plan):
    # The generated deployed handler resolves `flash_app.<func_name>` and CALLS
    # it as `func(**job_input)` — the job's `input` dict is splatted as kwargs.
    # So the module-level binding MUST be named exactly the handler __name__.
    download_fn = _DOWNLOADERS[plan.downloader]
    pkgs = upgrade_pkgs(plan.downloader)

    async def handler(**payload) -> dict:
        cold, dep_upgrade_ms = _upgrade_once(pkgs)
        return run_download(payload, download_fn,
                            cold_first_invocation=cold, dep_upgrade_ms=dep_upgrade_ms)

    handler.__name__ = handler.__qualname__ = plan.binding
    volume = None
    if plan.volume_gb is not None:
        volume = NetworkVolume(name=plan.env["VOLUME_NAME"], size=plan.volume_gb,
                               datacenter=DataCenter.EU_RO_1)
    return Endpoint(name=plan.name, cpu=_CPU, datacenter=DataCenter.EU_RO_1,
                    workers=(0, _MAX), idle_timeout=_IDLE_TIMEOUT_S, dependencies=_DEPS,
                    volume=volume, env={**plan.env, "HF_TOKEN": _HF_TOKEN})(handler)


# One module attribute per endpoint (xet_dl_A, xet_dl_baseline, xet_dl_ms_0, ...).
# Flash's scanner finds Endpoint instances via dir(module), so dynamic bindings work.
for _plan in plan_endpoints(_MECHANISM, _MODELS, os.environ):
    globals()[_plan.binding] = _mk(_plan)
del _plan
