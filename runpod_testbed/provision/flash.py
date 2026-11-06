"""Shared Flash plumbing on runpod-python >= 2.1 (`runpod.apps` + `rp flash`).

Every run deploys the worker/ project as its own Flash app
`xet-testbed-<runid>` into environment `xet-<runid>`. Flash 2.1 keeps no local
state (no .flash manifest): the app registry is the source of truth, so
endpoint ids are read back from the environment after deploy, and teardown
deletes by environment, then the app, then the endpoint templates it leaks.

Why an app per run (not one shared app with an env per run): 2.1.0 names each
endpoint template `<app>-<resource>-template` and undeploy leaves it held by
the deleted endpoint for up to ~90 min, so a second deploy of the same app
name within that window fails ("endpoint template names must be unique").
See provision/templates.py.
"""
from __future__ import annotations
import asyncio
import os
import subprocess
import sys
from typing import TYPE_CHECKING

from runpod_testbed.provision.templates import delete_flash_templates
from runpod_testbed.worker.plan import APP_NAME  # default App(...) name; per-run prefix

if TYPE_CHECKING:  # annotation only — a runtime import here would cycle:
    # flash -> mechanisms (package __init__ imports shim) -> shim -> flash (half-initialized)
    from runpod_testbed.mechanisms.base import WorkerSpec

WORKER_DIR = "runpod_testbed/worker"
# `python -m runpod.rp_cli.main` is the `rp` console script; invoking it through
# the current interpreter guarantees the CLI matches the imported SDK version.
RP_CLI = (sys.executable, "-m", "runpod.rp_cli.main")


def volume_name(runid: str) -> str:
    return f"xet-vc-{runid}"


_ENV_PREFIX = "xet-"


def flash_env_name(runid: str) -> str:
    return f"{_ENV_PREFIX}{runid}"


def flash_app_name(env_name: str) -> str:
    """The run's app, derived from its env name (xet-<runid> -> xet-testbed-<runid>)."""
    return f"{APP_NAME}-{env_name.removeprefix(_ENV_PREFIX)}"


def deploy_env(spec: WorkerSpec, cfg, hf_token: str, runid: str) -> dict[str, str]:
    env = {
        "MECHANISM": cfg.mechanism,
        "MODELS": ",".join(cfg.models),
        "HF_TOKEN": hf_token,
        "WORKER_CPU": cfg.worker_cpu,
        "WORKER_GPU": cfg.worker_gpu,
        "WORKER_DEPS": ",".join(spec.deps or cfg.worker_deps),
        "WORKER_MAX": str(cfg.burst),
        "WORKER_DC": cfg.dc,
        "FLASH_ENV": flash_env_name(runid),
        "FLASH_APP": flash_app_name(flash_env_name(runid)),   # read by worker/flash_app.py
        **spec.env,
    }
    if spec.network_volume_gb is not None:
        env["VOLUME_NAME"] = volume_name(runid)
        env["VOLUME_GB"] = str(spec.network_volume_gb)
    return env


def flash_deploy(env: dict[str, str], run=subprocess.run) -> None:
    """`rp flash deploy . --env <FLASH_ENV>` from WORKER_DIR. The deploy-time env
    is what worker/flash_app.py reads at discovery to build its endpoints."""
    run([*RP_CLI, "flash", "deploy", ".", "--env", env["FLASH_ENV"]],
        cwd=WORKER_DIR, env={**os.environ, **env}, check=True)


def endpoint_labels(endpoints: list[dict]) -> dict[str, str]:
    """[{"name": "xet-dl-A", "id": ...}, ...] -> {"A": id}: key each endpoint by
    the label after the last '-' (xet-dl-baseline -> "baseline")."""
    return {e["name"].rsplit("-", 1)[-1]: e["id"] for e in endpoints}


async def _environment(env_name: str, api=None) -> dict | None:
    from runpod.apps.manage import EnvironmentNotFound, get_environment
    try:
        return await get_environment(flash_app_name(env_name), env_name, api=api)
    except EnvironmentNotFound:
        return None


def environment_endpoint_ids(env_name: str) -> dict[str, str]:
    env = asyncio.run(_environment(env_name))
    if env is None:
        raise RuntimeError(f"Flash environment {flash_app_name(env_name)}/{env_name} "
                           f"not found after deploy")
    return endpoint_labels(env.get("endpoints") or [])


async def _undeploy_endpoints(env_name: str, names: tuple[str, ...]) -> list[str]:
    from runpod.apps.api import AppsApiClient
    client = AppsApiClient()
    env = await _environment(env_name, api=client)
    if env is None:  # never deployed, or already torn down
        return []
    failures = []
    for ep in env.get("endpoints") or []:
        if ep["name"] not in names:
            continue
        try:
            await client.delete_endpoint(ep["id"])
        except Exception as e:
            failures.append(f"endpoint {ep['name']} ({ep['id']}): {e}")
    return failures


def undeploy_endpoints(env_name: str, names: tuple[str, ...]) -> None:
    """Delete the named endpoints of one environment. A missing environment or
    endpoint is not an error (teardown-on-failure often runs before deploy)."""
    failures = asyncio.run(_undeploy_endpoints(env_name, names))
    if failures:
        raise RuntimeError("; ".join(failures))


async def _delete_environment(env_name: str, resources: tuple[str, ...]) -> list[str]:
    from runpod.apps.api import AppsApiClient
    from runpod.apps.manage import AppNotFound, get_app, undeploy_environment
    client = AppsApiClient()
    app_name = flash_app_name(env_name)
    env = await _environment(env_name, api=client)
    names = set(resources)
    if env is not None:
        names |= {ep["name"] for ep in env.get("endpoints") or []}
        result = await undeploy_environment(app_name, env_name, api=client)
        if result.failures:
            return result.failures
    try:
        app = await get_app(app_name, api=client)
    except AppNotFound:
        app = None
    if app is not None and not app.get("flashEnvironments"):
        await client.delete_app(app["id"])
    return delete_flash_templates(app_name, sorted(names))


def delete_environment(env_name: str, resources: tuple[str, ...] = ()) -> None:
    """Undeploy every remaining endpoint in the run's environment, delete the
    environment and the run's app, then the templates of `resources` (endpoint
    names; the env's own endpoints are added automatically)."""
    failures = asyncio.run(_delete_environment(env_name, tuple(resources)))
    if failures:
        raise RuntimeError("; ".join(failures))
