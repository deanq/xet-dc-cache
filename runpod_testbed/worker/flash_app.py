# Verified against the installed `runpod-flash` package (Endpoint signature
# inspected live via `inspect.signature`); no deltas from the researched shape —
# cpu/datacenter/workers/idle_timeout/dependencies/env all match, and
# DataCenter.EU_RO_1 exists as spelled.
import os
from runpod_flash import Endpoint, DataCenter
from runpod_testbed.worker.timing import run_download, hf_download

_DEPS = os.environ.get("WORKER_DEPS", "huggingface_hub,hf_xet").split(",")
_CPU = os.environ.get("WORKER_CPU", "cpu5c-4-8")
_MAX = int(os.environ.get("WORKER_MAX", "4"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")

def _mk(name: str, pod_addr: str):
    @Endpoint(name=name, cpu=_CPU, datacenter=DataCenter.EU_RO_1,
              workers=(0, _MAX), idle_timeout=5, dependencies=_DEPS,
              env={"HF_ENDPOINT": pod_addr, "HF_TOKEN": _HF_TOKEN})
    async def _fn(payload: dict) -> dict:
        return run_download(payload, hf_download)
    return _fn

# up.py exports POD_ADDR_A/B/C before `flash deploy`; groups map A/B/C -> pods.
# The A/B/C set (exactly three groups) is enforced in config.load (load_str) --
# a non-{A,B,C} overlap config raises ValueError there before any pod is
# provisioned, so this module can assume POD_ADDR_A/B/C always exist.
download_A = _mk("xet-dl-A", os.environ["POD_ADDR_A"])
download_B = _mk("xet-dl-B", os.environ["POD_ADDR_B"])
download_C = _mk("xet-dl-C", os.environ["POD_ADDR_C"])
