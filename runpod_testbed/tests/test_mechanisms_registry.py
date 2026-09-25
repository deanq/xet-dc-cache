"""Protocol-conformance: every registered mechanism implements Mechanism and
its ProvisionState round-trips through JSON. Phases 2/3 add config texts here."""
import pytest

from runpod_testbed.config import load_str
from runpod_testbed.mechanisms import MECHANISMS, get_mechanism
from runpod_testbed.mechanisms.base import ProvisionState, WorkerSpec
from runpod_testbed.tests.test_config import VALID

_CONFIG_TEXT = {
    "shim": VALID,
}
_METHODS = ("provision", "worker_spec", "teardown", "report_sections", "has_metrics", "jobs")


@pytest.mark.parametrize("name", sorted(MECHANISMS))
def test_registered_mechanism_conforms_to_protocol(name):
    m = get_mechanism(name)
    assert m.name == name
    for meth in _METHODS:
        assert callable(getattr(m, meth)), meth
    assert isinstance(m.has_metrics(), bool)
    assert m.report_sections([], []) == [] or all(isinstance(s, str) for s in m.report_sections([], []))


@pytest.mark.parametrize("name", sorted(MECHANISMS))
def test_registered_mechanism_worker_spec_and_jobs(name):
    cfg = load_str(_CONFIG_TEXT[name])
    m = get_mechanism(name)
    assert isinstance(m.worker_spec(cfg), WorkerSpec)
    jobs = m.jobs(cfg)
    assert jobs, "a mechanism must plan at least one job"
    for j in jobs:
        assert {"mechanism", "endpoint", "model", "phase", "replica"} <= set(j)
        assert j["mechanism"] == name and j["phase"] in ("cold", "populate", "warm")


@pytest.mark.parametrize("name", sorted(MECHANISMS))
def test_provision_state_roundtrips_for_each_mechanism(name, tmp_path):
    st = ProvisionState(mechanism=name, runid="r", endpoints={"baseline": "e0", "x": "e1"},
                        pods={"A": "p"}, volumes={"vc": "v"}, metrics_urls={"A": "http://a"})
    p = tmp_path / "s.json"
    st.save(str(p))
    assert ProvisionState.load(str(p)) == st


def test_get_mechanism_rejects_unknown():
    with pytest.raises(ValueError, match="shim"):
        get_mechanism("turbo")
