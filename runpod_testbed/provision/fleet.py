# Datacenter-pin finding (verified 2026-09-18, against runpod-python main branch
# runpod/api/ctl_commands.py `create_pod`): the functional kwarg is
# `data_center_id=` (Optional[str]). `country_code=` also exists in the
# signature but is explicitly rejected ("unsupported by the REST API v2") --
# passing it raises ValueError. `instance_id=` (matching `<flavor>-<vcpu>-<mem>`,
# e.g. "cpu3c-2-4") selects a CPU pod; CPU-pod availability in EU-RO-1 specifically
# was not verified against a live account/API key in this task -- confirm at
# first real `create_cache_pod` call (Task 8's integration run) and treat a
# capacity error there as "pin worked, DC lacks capacity" not "kwarg is wrong".
from __future__ import annotations
import json, os, urllib.request

def parse_external_addr(ports: list[dict]) -> str | None:
    for p in ports or []:
        if p.get("type") == "tcp" and p.get("isIpPublic") and p.get("privatePort") == 8000:
            return f"http://{p['ip']}:{p['publicPort']}"
    return None

_GQL = "https://api.runpod.io/graphql"

def _graphql(query: str, api_key: str) -> dict:
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(f"{_GQL}?api_key={api_key}", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

class Fleet:
    def __init__(self, api_key: str | None = None):
        import runpod
        self.api_key = api_key or os.environ["RUNPOD_API_KEY"]
        runpod.api_key = self.api_key
        self._runpod = runpod

    def get_pod_ports(self, pod_id: str) -> list[dict]:
        q = ('query { pod(input:{podId:"%s"}) { runtime { ports '
             '{ ip isIpPublic privatePort publicPort type } } } }' % pod_id)
        data = _graphql(q, self.api_key)
        rt = (data.get("data", {}).get("pod") or {}).get("runtime")
        return (rt or {}).get("ports") or []

    def create_cache_pod(self, name, image, instance_id, disk_gb, dc, env: dict) -> str:
        # dc kwarg name (data_center_id) confirmed in Task 1 -- see module docstring.
        pod = self._runpod.create_pod(
            name=name, image_name=image, instance_id=instance_id,
            container_disk_in_gb=disk_gb, ports="8000/tcp", env=env,
            data_center_id=dc)
        return pod["id"]

    def terminate_pod(self, pod_id: str) -> None:
        self._runpod.terminate_pod(pod_id)
