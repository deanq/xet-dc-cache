"""Model Store cached-model declaration via the undocumented `modelReferences`
field on the `saveEndpoint` GraphQL mutation (verified live 2026-09-25 against
the console's own JS; not in the REST API or SDKs — see CLAUDE.md).

`saveEndpoint` is an UPSERT: the caller must resend the endpoint's full config
or fields silently get dropped. There is no top-level `endpoint(id)` query, so
we read via `myself { endpoints { ... } }` and pick the matching id.
"""
from __future__ import annotations

from runpod_testbed.provision import fleet

# Fields to read from `myself.endpoints` and resend verbatim in `saveEndpoint`'s
# `EndpointInput` alongside `modelReferences` — omitting any of these on the
# upsert silently clears it on the live endpoint.
RESEND_FIELDS = (
    "id", "name", "templateId", "gpuIds", "gpuCount", "workersMin", "workersMax",
    "idleTimeout", "locations", "networkVolumeId", "scalerType", "scalerValue",
    "flashBootType", "executionTimeoutMs", "type", "allowedCudaVersions",
    "minCudaVersion", "flashEnvironmentId",
)


def _gql_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_gql_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return _gql_object(value)
    if value is None:
        return "null"
    raise TypeError(f"unsupported GraphQL literal type: {type(value)}")


def _gql_object(fields: dict) -> str:
    body = ", ".join(f"{key}: {_gql_value(v)}" for key, v in fields.items())
    return "{ " + body + " }"


def _raise_on_errors(response: dict, action: str) -> None:
    errors = response.get("errors")
    if errors:
        messages = "; ".join(e.get("message", str(e)) for e in errors)
        raise RuntimeError(f"GraphQL error while {action}: {messages}")


def endpoint_config(endpoint_id: str, api_key: str, *, graphql=fleet._graphql) -> dict:
    """Read the current full config of one endpoint via `myself { endpoints }`
    (no top-level `endpoint(id)` query exists)."""
    query = "query { myself { endpoints { %s } } }" % " ".join(RESEND_FIELDS)
    response = graphql(query, api_key)
    _raise_on_errors(response, f"reading config for endpoint {endpoint_id}")
    endpoints = ((response.get("data") or {}).get("myself") or {}).get("endpoints") or []
    for ep in endpoints:
        if ep.get("id") == endpoint_id:
            return ep
    raise RuntimeError(f"endpoint {endpoint_id} not found in myself.endpoints")


def set_model_references(endpoint_id: str, model_refs: list[str], api_key: str,
                          *, graphql=fleet._graphql) -> list[str]:
    """Upsert `modelReferences` onto an endpoint, resending its current full
    config (RESEND_FIELDS) so saveEndpoint doesn't drop other fields."""
    config = endpoint_config(endpoint_id, api_key, graphql=graphql)
    input_fields = {k: v for k, v in config.items() if k in RESEND_FIELDS and v is not None}
    input_fields["modelReferences"] = list(model_refs)
    mutation = "mutation { saveEndpoint(input: %s) { id modelReferences } }" % _gql_object(input_fields)
    response = graphql(mutation, api_key)
    _raise_on_errors(response, f"declaring model references on endpoint {endpoint_id}")
    result = (response.get("data") or {}).get("saveEndpoint") or {}
    return result.get("modelReferences") or []
