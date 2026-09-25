# Verified against the installed `runpod-flash` package (Endpoint signature
# inspected live via `inspect.signature`): cpu/datacenter/workers/idle_timeout/
# dependencies/env/volume all exist; DataCenter.EU_RO_1 exists as spelled.
"""Pure plan of which Flash endpoints flash_app.py builds for a mechanism.

Kept SDK-free so it is unit-testable; flash_app.py turns each EndpointPlan into
a runpod_flash.Endpoint. Flash packages worker/ as the deploy root, so inside
the worker this module is imported as top-level `plan`, not
`runpod_testbed.worker.plan`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping

HF_HOME_DEFAULT = "/root/.cache/huggingface"
SHIM_LABELS = ("A", "B", "C")
ENDPOINT_PREFIX = "xet-dl"


@dataclass
class EndpointPlan:
    name: str            # Flash endpoint name, e.g. xet-dl-A
    binding: str         # module attribute + handler __name__, e.g. xet_dl_A
    downloader: str      # key into flash_app._DOWNLOADERS
    env: dict[str, str] = field(default_factory=dict)   # runtime env baked into the endpoint
    volume_gb: int | None = None


def _plan(label: str, downloader: str, common: dict, extra: dict, volume_gb=None) -> EndpointPlan:
    name = f"{ENDPOINT_PREFIX}-{label}"
    return EndpointPlan(name=name, binding=name.replace("-", "_"), downloader=downloader,
                        env={**extra, **common}, volume_gb=volume_gb)


def plan_endpoints(mechanism: str, models: list[str], env: Mapping[str, str]) -> list[EndpointPlan]:
    common = {"MECHANISM": mechanism, "MODELS": ",".join(models)}
    plans = [_plan("baseline", "baseline", common, {})]
    if mechanism == "shim":
        plans += [_plan(g, "shim", common, {"HF_ENDPOINT": env.get(f"POD_ADDR_{g}", "")})
                  for g in SHIM_LABELS]
        return plans
    if mechanism == "volumecache":
        gb = int(env.get("VOLUME_GB", "0")) or None
        extra = {"HF_HOME": HF_HOME_DEFAULT, "VOLUME_NAME": env.get("VOLUME_NAME", "")}
        return plans + [_plan("volumecache", "volumecache", common, extra, volume_gb=gb)]
    if mechanism == "modelstore":
        return plans + [_plan(f"m{i}", "modelstore", common, {"MODEL": m})
                        for i, m in enumerate(models)]
    raise ValueError(f"unknown mechanism {mechanism!r}")
