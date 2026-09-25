import pytest

from runpod_testbed.provision.modelstore_api import (
    RESEND_FIELDS, endpoint_config, set_model_references,
)

_FULL_ENDPOINT = {
    "id": "ep-1", "name": "xet-dl-m0", "templateId": "tmpl-1", "gpuIds": "AMPERE_16",
    "gpuCount": 1, "workersMin": 0, "workersMax": 3, "idleTimeout": 5,
    "locations": None, "networkVolumeId": None, "scalerType": "QUEUE_DELAY",
    "scalerValue": 4, "flashBootType": "STANDARD", "executionTimeoutMs": 0,
    "type": "SERVERLESS", "allowedCudaVersions": None, "minCudaVersion": None,
    "flashEnvironmentId": "env-1",
}


def _fake_graphql(endpoints, mutation_result=None, errors=None):
    calls = []

    def graphql(query, api_key):
        calls.append((query, api_key))
        if "saveEndpoint" in query:
            if errors:
                return {"errors": errors}
            return {"data": {"saveEndpoint": mutation_result or {}}}
        return {"data": {"myself": {"endpoints": endpoints}}}

    return graphql, calls


def test_endpoint_config_returns_matching_endpoint():
    graphql, calls = _fake_graphql([_FULL_ENDPOINT])
    cfg = endpoint_config("ep-1", "rk", graphql=graphql)
    assert cfg == _FULL_ENDPOINT
    assert calls[0][1] == "rk" and "myself" in calls[0][0]


def test_endpoint_config_raises_on_unknown_id():
    graphql, _ = _fake_graphql([_FULL_ENDPOINT])
    with pytest.raises(RuntimeError, match="ep-missing"):
        endpoint_config("ep-missing", "rk", graphql=graphql)


def test_endpoint_config_raises_on_graphql_errors():
    def graphql(query, api_key):
        return {"errors": [{"message": "not authorized"}]}
    with pytest.raises(RuntimeError, match="not authorized"):
        endpoint_config("ep-1", "rk", graphql=graphql)


def test_set_model_references_resends_full_config_plus_model_references():
    graphql, calls = _fake_graphql(
        [_FULL_ENDPOINT], mutation_result={"id": "ep-1", "modelReferences": ["org/x:main"]})
    result = set_model_references("ep-1", ["org/x:main"], "rk", graphql=graphql)
    assert result == ["org/x:main"]
    mutation_query = calls[1][0]
    assert "saveEndpoint" in mutation_query
    for field in RESEND_FIELDS:
        value = _FULL_ENDPOINT[field]
        if value is None:
            continue
        assert field in mutation_query
    assert 'modelReferences: ["org/x:main"]' in mutation_query
    assert 'id: "ep-1"' in mutation_query and 'name: "xet-dl-m0"' in mutation_query


def test_set_model_references_omits_null_fields_from_input():
    graphql, calls = _fake_graphql([_FULL_ENDPOINT], mutation_result={})
    set_model_references("ep-1", ["org/x:main"], "rk", graphql=graphql)
    mutation_query = calls[1][0]
    assert "locations:" not in mutation_query
    assert "networkVolumeId:" not in mutation_query


def test_set_model_references_raises_runtime_error_on_graphql_error():
    graphql, _ = _fake_graphql([_FULL_ENDPOINT], errors=[{"message": "invalid input"}])
    with pytest.raises(RuntimeError, match="invalid input"):
        set_model_references("ep-1", ["org/x:main"], "rk", graphql=graphql)
