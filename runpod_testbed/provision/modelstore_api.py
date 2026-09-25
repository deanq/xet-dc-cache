"""Model Store cached-model declaration via the undocumented `modelReferences`
field on the `saveEndpoint` GraphQL mutation (verified live 2026-09-25 against
the console's own JS; not in the REST API or SDKs — see CLAUDE.md).

`saveEndpoint` is an UPSERT: the caller must resend the endpoint's full config or
fields silently get dropped. There is no top-level `endpoint(id)` query, so we
read via `myself { endpoints { ... } }` and pick the matching id.

The mutation is sent with GraphQL **variables** (not an inlined object literal):
`EndpointInput` has enum fields (e.g. `flashBootType: FlashBootType`), and JSON
variable coercion maps a plain string like "FLASHBOOT" to the enum, whereas an
inlined literal `"FLASHBOOT"` is rejected as a non-enum value.
"""
from __future__ import annotations

import json
import os
import urllib.request

# Fields to read from `myself.endpoints` and resend verbatim in `saveEndpoint`'s
# `EndpointInput` alongside `modelReferences` — omitting any of these on the
# upsert silently clears it on the live endpoint.
# NOTE: `computeType` exists on the Endpoint output type but NOT on EndpointInput;
# `instanceIds` (e.g. ["cpu5c-4-8"]) is what marks a CPU endpoint on the input and
# stops saveEndpoint from demanding gpuIds. Only fields valid on EndpointInput.
RESEND_FIELDS = (
    "id", "name", "templateId", "instanceIds", "gpuIds", "gpuCount",
    "workersMin", "workersMax", "idleTimeout", "locations", "networkVolumeId",
    "scalerType", "scalerValue", "flashBootType", "executionTimeoutMs", "type",
    "allowedCudaVersions", "minCudaVersion", "flashEnvironmentId",
)
_GRAPHQL_TIMEOUT_S = 30
_USER_AGENT = "xet-dc-cache-testbed"  # api.runpod.io is behind Cloudflare; needs a UA


def post_graphql(query: str, variables: dict | None, api_key: str) -> dict:
    """POST a GraphQL query/mutation with variables to api.runpod.io and return
    the parsed response. Raises RuntimeError on a GraphQL `errors` payload."""
    base = os.environ.get("RUNPOD_API_BASE_URL", "https://api.runpod.io")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        f"{base}/graphql", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT,
                 "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=_GRAPHQL_TIMEOUT_S) as resp:
        payload = json.loads(resp.read() or b"{}")
    errors = payload.get("errors")
    if errors:
        messages = "; ".join(e.get("message", str(e)) for e in errors)
        raise RuntimeError(f"GraphQL error: {messages}")
    return payload


def endpoint_config(endpoint_id: str, api_key: str, *, post=post_graphql) -> dict:
    """Read one endpoint's current config via `myself { endpoints }` (there is no
    top-level `endpoint(id)` query)."""
    query = "query { myself { endpoints { %s } } }" % " ".join(RESEND_FIELDS)
    data = post(query, None, api_key)
    endpoints = ((data.get("data") or {}).get("myself") or {}).get("endpoints") or []
    for ep in endpoints:
        if ep.get("id") == endpoint_id:
            return ep
    raise RuntimeError(f"endpoint {endpoint_id} not found in myself.endpoints")


def set_model_references(endpoint_id: str, model_refs: list[str], api_key: str,
                         *, post=post_graphql) -> list[str]:
    """Upsert `modelReferences` onto an endpoint, resending its current full
    config (RESEND_FIELDS) so saveEndpoint doesn't drop other fields."""
    config = endpoint_config(endpoint_id, api_key, post=post)
    input_fields = {k: v for k, v in config.items() if k in RESEND_FIELDS and v is not None}
    input_fields["modelReferences"] = list(model_refs)
    mutation = ("mutation($input: EndpointInput!) { "
                "saveEndpoint(input: $input) { id modelReferences } }")
    data = post(mutation, {"input": input_fields}, api_key)
    result = (data.get("data") or {}).get("saveEndpoint") or {}
    return result.get("modelReferences") or []
