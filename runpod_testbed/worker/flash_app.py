# Flash app on runpod-python >= 2.1 (`runpod.apps`), deployed with
# `rp flash deploy . --env xet-<runid>` from this directory (provision/flash.py).
# Verified against the runpod 2.1.0 source: App.queue(name/cpu/gpu/gpu_count/
# datacenter/workers/idle_timeout/dependencies/mounts/env), NetworkVolume(name,
# size=, datacenter=), and the deployed queue runtime (runpod-workers/sdk-runtime)
# which imports this module, finds the FunctionHandle whose spec.name equals
# FLASH_RESOURCE_NAME by scanning module globals, and calls fn(**job["input"]).
import os

from runpod.apps import App, NetworkVolume
from runpod.apps.gpu import GpuGroup, GpuType
# rp flash deploy packages this worker/ dir as the project root, so timing.py /
# plan.py are top-level siblings here — NOT importable as runpod_testbed.worker.*.
from timing import run_download, hf_download, volumecache_download, modelstore_local_read
from plan import APP_NAME, gpu_names, plan_endpoints

# Vendored into the build artifact at deploy time; the runtime puts the artifact's
# env/ first on sys.path, so these pins (>= the hf_xet that honors HF_ENDPOINT for
# xorb fetches) are what the worker imports — no cold-start pip upgrade needed.
_DEPS = os.environ.get("WORKER_DEPS", "huggingface_hub>=1.32.0,hf_xet>=1.6.0").split(",")
_CPU = os.environ.get("WORKER_CPU", "cpu5c-4-8")
_DC = os.environ.get("WORKER_DC", "EU-RO-1")
# Non-empty WORKER_GPU switches every endpoint from cpu= to gpu= (Model Store
# staging is GPU-only). Empty (default) keeps CPU endpoints.
_GPU_NAMES = gpu_names(os.environ)
_MAX = int(os.environ.get("WORKER_MAX", "4"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")
_IDLE_TIMEOUT_S = 30
_VOLUME_MOUNT = "/runpod-volume"   # queue endpoints support one volume, mounted here
# MECHANISM/MODELS are present at deploy time (provision/flash.py::deploy_env)
# AND at worker runtime (baked into every endpoint's env by plan_endpoints), so
# the same bindings exist in both imports. Default "shim" keeps a bare import safe.
_MECHANISM = os.environ.get("MECHANISM", "shim")
_MODELS = [m for m in os.environ.get("MODELS", "").split(",") if m]
_INVOKED: list = []  # once-flag: the first job a worker process serves is its cold one

_DOWNLOADERS = {"shim": hf_download, "baseline": hf_download,
                "volumecache": volumecache_download, "modelstore": modelstore_local_read}

# FLASH_APP (xet-testbed-<runid>) is set at deploy time by provision/flash.py;
# the deployed queue runtime never reads the app name, so the default is safe.
app = App(os.environ.get("FLASH_APP", APP_NAME))


def _first_invocation() -> bool:
    if _INVOKED:
        return False
    _INVOKED.append(True)
    return True


def _resolve_gpus(names: list[str]) -> list:
    """WORKER_GPU entries name a GpuGroup or GpuType enum member; resolve each
    against both, in that order, and fail loudly on a typo rather than let
    app.queue(gpu=...) reject it with a less specific error."""
    gpus = []
    for name in names:
        if name in GpuGroup.__members__:
            gpus.append(GpuGroup[name])
        elif name in GpuType.__members__:
            gpus.append(GpuType[name])
        else:
            raise ValueError(f"WORKER_GPU: {name!r} is not a GpuGroup or GpuType member")
    return gpus


def _compute() -> dict:
    if _GPU_NAMES:
        return {"gpu": _resolve_gpus(_GPU_NAMES), "gpu_count": 1}
    return {"cpu": _CPU}


def _mk(plan):
    download_fn = _DOWNLOADERS[plan.downloader]

    async def handler(**payload) -> dict:
        return run_download(payload, download_fn, cold_first_invocation=_first_invocation())

    handler.__name__ = handler.__qualname__ = plan.binding
    mounts = None
    if plan.volume_gb is not None:
        mounts = {_VOLUME_MOUNT: NetworkVolume(plan.env["VOLUME_NAME"], size=plan.volume_gb,
                                               datacenter=_DC)}
    return app.queue(name=plan.name, datacenter=_DC, workers=(0, _MAX),
                     idle_timeout=_IDLE_TIMEOUT_S, dependencies=_DEPS, mounts=mounts,
                     env={**plan.env, "HF_TOKEN": _HF_TOKEN}, **_compute())(handler)


# One module attribute per endpoint (xet_dl_A, xet_dl_baseline, xet_dl_m0, ...).
# The deployed runtime scans vars(module) for the handle, so dynamic bindings work.
for _plan in plan_endpoints(_MECHANISM, _MODELS, os.environ):
    globals()[_plan.binding] = _mk(_plan)
del _plan
