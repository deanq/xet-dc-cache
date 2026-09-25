"""Runpod Model Store (cached models) as a Mechanism: one Flash CPU endpoint per
model (platform limit: one cached model per endpoint), weights pre-staged by
the platform to /runpod-volume/huggingface-cache/hub/... outside the handler.
The handler only asserts presence + times a local read; the yardstick is the
driver's end-to-end wall on a scaled-from-zero worker (replica 0 of each burst).

Owned path: `flash deploy` creates xet-dl-m{i}; the cached model is then
declared per Phase 0 finding (b) — manual console step (default) or API.
Reuse path ([modelstore.endpoints] maps every model to an endpoint id from a
previous run that already has its model cached): validate the ids, deploy only
the baseline, drive, and tear down only what this run owns.
"""
from __future__ import annotations
import os

from runpod_testbed.mechanisms.base import (
    BASELINE_LABEL, ProvisionState, WorkerSpec, endpoint_name, state_path,
)
from runpod_testbed.provision.down import cli_undeploy
from runpod_testbed.provision.flash import deploy_env, flash_deploy, manifest_endpoint_ids
from runpod_testbed.worker.timing import MODELSTORE_ROOT

REUSED_PREFIX = "reused-"
_ENDPOINTS_QUERY = "query { myself { endpoints { id name } } }"


def model_label(i: int) -> str:
    return f"m{i}"


def manual_step_lines(cfg, state: ProvisionState) -> list[str]:
    lines = ["MANUAL STEP (Model Store is console-only per the Phase 0 spike):",
             "  In the Runpod console, Serverless -> edit each endpoint below -> declare its cached model,",
             f"  then wait until the worker shows the files under {MODELSTORE_ROOT}/models--<org>--<name>/snapshots/<hash>/.",
             "  Then run: make -C runpod_testbed drive RUNID=" + state.runid]
    for i, model in enumerate(cfg.models):
        label = model_label(i)
        if label in state.endpoints:
            lines.append(f"    - {endpoint_name(label)} ({state.endpoints[label]}): cache {model}")
    return lines


def _validate_reused(cfg, graphql, api_key: str) -> dict[str, str]:
    missing_models = [m for m in cfg.models if m not in cfg.modelstore_endpoints]
    if missing_models:
        raise ValueError(f"[modelstore.endpoints] must map every model; missing {missing_models}")
    live = {e["id"] for e in graphql(_ENDPOINTS_QUERY, api_key)["data"]["myself"]["endpoints"]}
    unknown = sorted(eid for eid in cfg.modelstore_endpoints.values() if eid not in live)
    if unknown:
        raise ValueError(f"[modelstore.endpoints] ids not found on the account: {unknown}")
    return {f"{REUSED_PREFIX}{model_label(i)}": cfg.modelstore_endpoints[m]
            for i, m in enumerate(cfg.models)}


class ModelStoreMechanism:
    name = "modelstore"

    def has_metrics(self) -> bool:
        return False

    def worker_spec(self, cfg) -> WorkerSpec:
        return WorkerSpec(handler="modelstore")

    def jobs(self, cfg) -> list[dict]:
        prefix = REUSED_PREFIX if cfg.modelstore_endpoints else ""
        return [{"mechanism": self.name, "endpoint": f"{prefix}{model_label(i)}", "model": m,
                 "phase": "warm", "replica": r}
                for i, m in enumerate(cfg.models) for r in range(cfg.burst)]

    def provision(self, cfg, runid: str, *, deploy=flash_deploy, manifest_ids=manifest_endpoint_ids,
                  graphql=None, environ=os.environ) -> ProvisionState:
        if graphql is None:
            from runpod_testbed.provision.fleet import _graphql as graphql
        state = ProvisionState(mechanism=self.name, runid=runid)
        os.makedirs("data", exist_ok=True)
        env = deploy_env(self.worker_spec(cfg), cfg, environ["HF_TOKEN"], runid)
        reused = {}
        if cfg.modelstore_endpoints:            # validate BEFORE any spend
            reused = _validate_reused(cfg, graphql, environ["RUNPOD_API_KEY"])
            env["MODELS"] = ""                  # plan_endpoints -> baseline only
        state.save(state_path(runid))
        deploy(env)
        state.endpoints = {**manifest_ids(), **reused}
        state.save(state_path(runid))
        with open("data/last-runid", "w") as fh:
            fh.write(runid)
        if not reused:
            self.declare_cached_models(cfg, state, environ)
        return state

    def declare_cached_models(self, cfg, state: ProvisionState, environ) -> None:
        # Branch (i) — console-only (default until the spike says otherwise).
        print("\n".join(manual_step_lines(cfg, state)), flush=True)

    def teardown(self, state: ProvisionState, *, flash_undeploy=None) -> None:
        owned = [endpoint_name(label) for label in state.endpoints
                 if label != BASELINE_LABEL and not label.startswith(REUSED_PREFIX)]
        if owned:
            (flash_undeploy or cli_undeploy)(f"xet-{state.runid}", names=tuple(owned))

    def report_sections(self, jobs: list, metrics_rows: list, state) -> list[str]:
        return []
