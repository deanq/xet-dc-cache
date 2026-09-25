import pytest

from runpod_testbed.provision.modelstore_api import (
    RESEND_FIELDS, endpoint_config, set_model_references,
)

_FULL_ENDPOINT = {
    "id": "ep-1", "name": "xet-dl-m0", "templateId": "tmpl-1",
    "computeType": "CPU", "instanceIds": ["cpu5c-4-8"], "gpuIds": None,
    "gpuCount": 1, "workersMin": 0, "workersMax": 3, "idleTimeout": 5,
    "locations": None, "networkVolumeId": None, "scalerType": "QUEUE_DELAY",
    "scalerValue": 4, "flashBootType": "FLASHBOOT", "executionTimeoutMs": 0,
    "type": "SERVERLESS", "allowedCudaVersions": None, "minCudaVersion": None,
    "flashEnvironmentId": "env-1",
}


def _fake_post(endpoints, mutation_result=None, error_on=None):
    """Fake for modelstore_api.post_graphql: post(query, variables, api_key).
    error_on ("read"|"save") makes that call raise, mimicking real post_graphql."""
    calls = []

    def post(query, variables, api_key):
        calls.append((query, variables, api_key))
        is_save = "saveEndpoint" in query
        if error_on == ("save" if is_save else "read"):
            raise RuntimeError("GraphQL error: boom")
        if is_save:
            return {"data": {"saveEndpoint": mutation_result or {}}}
        return {"data": {"myself": {"endpoints": endpoints}}}

    return post, calls


def test_endpoint_config_returns_matching_endpoint():
    post, calls = _fake_post([_FULL_ENDPOINT])
    cfg = endpoint_config("ep-1", "rk", post=post)
    assert cfg == _FULL_ENDPOINT
    query, variables, api_key = calls[0]
    assert api_key == "rk" and "myself" in query and variables is None


def test_endpoint_config_raises_on_unknown_id():
    post, _ = _fake_post([_FULL_ENDPOINT])
    with pytest.raises(RuntimeError, match="ep-missing"):
        endpoint_config("ep-missing", "rk", post=post)


def test_endpoint_config_raises_on_graphql_errors():
    post, _ = _fake_post([_FULL_ENDPOINT], error_on="read")
    with pytest.raises(RuntimeError, match="boom"):
        endpoint_config("ep-1", "rk", post=post)


def test_set_model_references_sends_input_as_variable_with_full_config():
    post, calls = _fake_post(
        [_FULL_ENDPOINT], mutation_result={"id": "ep-1", "modelReferences": ["org/x:main"]})
    result = set_model_references("ep-1", ["org/x:main"], "rk", post=post)
    assert result == ["org/x:main"]
    save_query, save_vars, _ = calls[1]
    # Parameterized mutation (enums coerce via variables, not an inline literal).
    assert "$input" in save_query and "saveEndpoint(input: $input)" in save_query
    sent = save_vars["input"]
    # every non-null resend field is carried through, plus the model references
    for field in RESEND_FIELDS:
        if _FULL_ENDPOINT[field] is not None:
            assert sent[field] == _FULL_ENDPOINT[field]
    assert sent["modelReferences"] == ["org/x:main"]
    # enum value stays a bare string in the JSON variable (server coerces it)
    assert sent["flashBootType"] == "FLASHBOOT"


def test_set_model_references_omits_null_fields_from_input():
    post, calls = _fake_post([_FULL_ENDPOINT], mutation_result={})
    set_model_references("ep-1", ["org/x:main"], "rk", post=post)
    sent = calls[1][1]["input"]
    assert "locations" not in sent
    assert "networkVolumeId" not in sent


def test_set_model_references_propagates_graphql_error():
    post, _ = _fake_post([_FULL_ENDPOINT], error_on="save")
    with pytest.raises(RuntimeError, match="boom"):
        set_model_references("ep-1", ["org/x:main"], "rk", post=post)
