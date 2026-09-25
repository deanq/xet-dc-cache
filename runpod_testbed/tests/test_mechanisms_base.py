import json

from runpod_testbed.mechanisms.base import (
    BASELINE_LABEL, ProvisionState, WorkerSpec, endpoint_name, state_path,
)


def test_worker_spec_defaults_to_no_volume():
    spec = WorkerSpec(handler="shim")
    assert spec.env == {} and spec.deps == [] and spec.network_volume_gb is None


def test_provision_state_roundtrips_through_json(tmp_path):
    st = ProvisionState(mechanism="volumecache", runid="r1",
                        endpoints={"volumecache": "ep-1", BASELINE_LABEL: "ep-b"},
                        volumes={"vc": "vol-9"})
    p = tmp_path / "state.json"
    st.save(str(p))
    back = ProvisionState.load(str(p))
    assert back == st
    assert json.loads(p.read_text())["pods"] == {}          # defaults serialize


def test_provision_state_from_json_text():
    text = json.dumps({"mechanism": "shim", "runid": "r", "endpoints": {"A": "e"},
                       "pods": {"A": "p"}, "volumes": {}, "metrics_urls": {"A": "http://x"}})
    st = ProvisionState.from_json(text)
    assert st.pods == {"A": "p"} and st.metrics_urls["A"] == "http://x"


def test_endpoint_name_and_state_path():
    assert endpoint_name("A") == "xet-dl-A"
    assert endpoint_name(BASELINE_LABEL) == "xet-dl-baseline"
    assert state_path("20260924-101500") == "data/state-20260924-101500.json"
