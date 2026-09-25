"""Runpod REST network-volume helpers. Flash creates the volume (idempotently,
by name + datacenter) during `flash deploy`, but neither Flash's client nor
`flash undeploy` can delete one — teardown must, or the volume keeps billing."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

REST_BASE = "https://rest.runpod.io/v1"
_TIMEOUT_S = 20
_OK_DELETE = (200, 204)
_ALREADY_GONE = 404


def _request(method: str, url: str, api_key: str, opener) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url, method=method, headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with opener(req, timeout=_TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body


def list_network_volumes(api_key: str, opener=urllib.request.urlopen) -> list[dict]:
    status, body = _request("GET", f"{REST_BASE}/networkvolumes", api_key, opener)
    if status != 200:
        raise RuntimeError(f"GET /networkvolumes -> {status}: {body[:200]!r}")
    data = json.loads(body or b"[]")
    return data if isinstance(data, list) else data.get("networkVolumes", [])


def find_volume_id(name: str, volumes: list[dict]) -> str | None:
    for v in volumes:
        if v.get("name") == name:
            return v.get("id")
    return None


def delete_network_volume(
    volume_id: str, api_key: str | None = None, opener=urllib.request.urlopen
) -> None:
    api_key = api_key or os.environ["RUNPOD_API_KEY"]
    status, body = _request(
        "DELETE", f"{REST_BASE}/networkvolumes/{volume_id}", api_key, opener
    )
    if status in _OK_DELETE or status == _ALREADY_GONE:
        return
    raise RuntimeError(f"DELETE /networkvolumes/{volume_id} -> {status}: {body[:200]!r}")
