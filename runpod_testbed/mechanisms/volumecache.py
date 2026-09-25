"""VolumeCache as a Mechanism: one Flash CPU endpoint with a network volume
attached at /runpod-volume (Flash `Endpoint(volume=NetworkVolume(...))`, built
in worker/flash_app.py from VOLUME_NAME/VOLUME_GB). The worker mirrors HF_HOME
onto the volume via runpod.serverless.VolumeCache; the driver runs
populate -> warm burst per model.
"""
from __future__ import annotations
import os

from runpod_testbed.mechanisms.base import ProvisionState, WorkerSpec, endpoint_name, state_path
from runpod_testbed.provision.down import cli_undeploy
from runpod_testbed.provision.flash import deploy_env, flash_deploy, manifest_endpoint_ids, volume_name
from runpod_testbed.provision.volumes import find_volume_id, list_network_volumes

VOLUME_LABEL = "volumecache"


class VolumeCacheMechanism:
    name = "volumecache"

    def has_metrics(self) -> bool:
        return False

    def worker_spec(self, cfg) -> WorkerSpec:
        return WorkerSpec(handler="volumecache", network_volume_gb=cfg.volume_gb)

    def jobs(self, cfg) -> list[dict]:
        jobs = []
        for m in cfg.models:   # populate (mirror empty -> WAN pull -> sync) then warm burst (hydrate)
            jobs.append(self._job(m, "populate", 0))
            jobs += [self._job(m, "warm", r) for r in range(cfg.burst)]
        return jobs

    def _job(self, model: str, phase: str, replica: int) -> dict:
        return {"mechanism": self.name, "endpoint": VOLUME_LABEL, "model": model,
                "phase": phase, "replica": replica}

    def provision(self, cfg, runid: str, *, deploy=flash_deploy, manifest_ids=manifest_endpoint_ids,
                  list_volumes=list_network_volumes, environ=os.environ) -> ProvisionState:
        state = ProvisionState(mechanism=self.name, runid=runid)
        os.makedirs("data", exist_ok=True)
        state.save(state_path(runid))
        deploy(deploy_env(self.worker_spec(cfg), cfg, environ["HF_TOKEN"], runid))
        state.endpoints = manifest_ids()
        vid = find_volume_id(volume_name(runid), list_volumes(environ["RUNPOD_API_KEY"]))
        if vid:
            state.volumes[VOLUME_LABEL] = vid
        else:
            print(f"warning: network volume {volume_name(runid)!r} not found after deploy — "
                  f"teardown cannot remove it; check Storage in the Runpod console")
        state.save(state_path(runid))
        with open("data/last-runid", "w") as fh:
            fh.write(runid)
        return state

    def teardown(self, state: ProvisionState, *, flash_undeploy=None) -> None:
        # The volume itself is deleted by down.teardown_all from state.volumes.
        (flash_undeploy or cli_undeploy)(f"xet-{state.runid}", names=(endpoint_name(VOLUME_LABEL),))

    def report_sections(self, jobs: list, metrics_rows: list, state) -> list[str]:
        return []
