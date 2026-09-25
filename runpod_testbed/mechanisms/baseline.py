"""The naive-HF control: one plain Flash CPU endpoint, hf_hub_download straight
from HF (no HF_ENDPOINT, no VolumeCache, no cached model). Provisioned for every
run by the shared core (worker/plan.py always emits it); the driver measures a
`baseline` phase on it for the same models the mechanism run uses.
"""
from __future__ import annotations
from runpod_testbed.mechanisms.base import BASELINE_LABEL, WorkerSpec

BASELINE_MECHANISM = "baseline"
BASELINE_WORKER_SPEC = WorkerSpec(handler="baseline")


def baseline_jobs(models: list[str]) -> list[dict]:
    return [{"mechanism": BASELINE_MECHANISM, "endpoint": BASELINE_LABEL,
             "model": m, "phase": "baseline", "replica": 0} for m in models]
