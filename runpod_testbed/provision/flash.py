"""Shared `flash deploy` plumbing. Flash tracks deployed endpoints in
worker/.flash, so deploy AND undeploy must run from WORKER_DIR."""
from __future__ import annotations
import json
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only — a runtime import here would cycle:
    # flash -> mechanisms (package __init__ imports shim) -> shim -> flash (half-initialized)
    from runpod_testbed.mechanisms.base import WorkerSpec

WORKER_DIR = "runpod_testbed/worker"
MANIFEST_PATH = f"{WORKER_DIR}/.flash/flash_manifest.json"


def deploy_env(spec: WorkerSpec, cfg, hf_token: str, runid: str) -> dict[str, str]:
    env = {
        "MECHANISM": cfg.mechanism,
        "MODELS": ",".join(cfg.models),
        "HF_TOKEN": hf_token,
        "WORKER_CPU": cfg.worker_cpu,
        "WORKER_DEPS": ",".join(spec.deps or cfg.worker_deps),
        "WORKER_MAX": str(cfg.burst),
        "FLASH_ENV": f"xet-{runid}",
        **spec.env,
    }
    if spec.network_volume_gb is not None:
        env["VOLUME_NAME"] = f"xet-vc-{runid}"
        env["VOLUME_GB"] = str(spec.network_volume_gb)
    return env


def flash_deploy(env: dict[str, str], run=subprocess.run) -> None:
    run(["flash", "deploy", "--env", env["FLASH_ENV"]],
        cwd=WORKER_DIR, env={**os.environ, **env}, check=True)


def manifest_endpoint_ids(path: str = MANIFEST_PATH) -> dict[str, str]:
    from runpod_testbed.drive.run import endpoint_ids
    with open(path) as fh:
        return endpoint_ids(json.load(fh))
