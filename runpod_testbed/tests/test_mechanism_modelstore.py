from dataclasses import replace

import pytest

from runpod_testbed.config import load_str
from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, WorkerSpec
from runpod_testbed.mechanisms.modelstore import (
    REUSED_PREFIX, ModelStoreMechanism, manual_step_lines, model_label,
)
from runpod_testbed.tests.test_config import VALID_VOLUMECACHE

_ENV = {"HF_TOKEN": "hf_t", "RUNPOD_API_KEY": "rk"}
_MS = VALID_VOLUMECACHE.replace('mechanism = "volumecache"', 'mechanism = "modelstore"') \
    .replace("[volumecache]\nvolume_gb = 50", "[modelstore]")
_MS_REUSE = _MS + '\n[modelstore.endpoints]\n"org/x@main" = "ep-x"\n"org/y@main" = "ep-y"\n'


def test_identity_spec_and_no_metrics():
    m = ModelStoreMechanism()
    assert m.name == "modelstore" and m.has_metrics() is False
    assert m.worker_spec(load_str(_MS)) == WorkerSpec(handler="modelstore")
    assert m.report_sections([], []) == []


def test_jobs_are_warm_only_per_model_endpoint():
    jobs = ModelStoreMechanism().jobs(replace(load_str(_MS), burst=2))
    assert [(j["endpoint"], j["model"], j["phase"], j["replica"]) for j in jobs] == [
        ("m0", "org/x@main", "warm", 0), ("m0", "org/x@main", "warm", 1),
        ("m1", "org/y@main", "warm", 0), ("m1", "org/y@main", "warm", 1),
    ]
    assert all(j["mechanism"] == "modelstore" for j in jobs)
    assert model_label(3) == "m3"


def test_jobs_use_reused_labels_when_endpoints_are_mapped():
    jobs = ModelStoreMechanism().jobs(replace(load_str(_MS_REUSE), burst=1))
    assert [j["endpoint"] for j in jobs] == [f"{REUSED_PREFIX}m0", f"{REUSED_PREFIX}m1"]


def test_provision_owned_path_deploys_per_model_endpoints_and_prints_manual_step(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    deploys = []
    st = ModelStoreMechanism().provision(
        load_str(_MS), "r1", deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {"m0": "e0", "m1": "e1", BASELINE_LABEL: "eb"}, environ=_ENV)
    assert deploys[0]["MECHANISM"] == "modelstore" and deploys[0]["MODELS"] == "org/x@main,org/y@main"
    assert st.endpoints == {"m0": "e0", "m1": "e1", BASELINE_LABEL: "eb"}
    out = capsys.readouterr().out
    assert "xet-dl-m0" in out and "org/x@main" in out and "e1" in out
    assert "/runpod-volume/huggingface-cache/hub" in out
    assert ProvisionState.load("data/state-r1.json") == st


def test_provision_reuse_path_validates_ids_and_deploys_only_baseline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deploys, queries = [], []

    def _graphql(q, key):
        queries.append((q, key))
        return {"data": {"myself": {"endpoints": [{"id": "ep-x", "name": "n1"}, {"id": "ep-y", "name": "n2"}]}}}

    st = ModelStoreMechanism().provision(
        load_str(_MS_REUSE), "r2", deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {BASELINE_LABEL: "eb"}, graphql=_graphql, environ=_ENV)
    assert deploys[0]["MODELS"] == "" and queries[0][1] == "rk"
    assert st.endpoints == {f"{REUSED_PREFIX}m0": "ep-x", f"{REUSED_PREFIX}m1": "ep-y", BASELINE_LABEL: "eb"}


def test_provision_reuse_path_fails_before_spend_on_unknown_or_partial_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deploys = []
    gone = lambda q, key: {"data": {"myself": {"endpoints": [{"id": "ep-x", "name": "n1"}]}}}
    with pytest.raises(ValueError, match="ep-y"):
        ModelStoreMechanism().provision(load_str(_MS_REUSE), "r3", deploy=lambda env: deploys.append(env),
                                        manifest_ids=lambda: {}, graphql=gone, environ=_ENV)
    partial = load_str(_MS_REUSE.replace('"org/y@main" = "ep-y"\n', ""))
    with pytest.raises(ValueError, match="org/y@main"):
        ModelStoreMechanism().provision(partial, "r4", deploy=lambda env: deploys.append(env),
                                        manifest_ids=lambda: {}, graphql=gone, environ=_ENV)
    assert deploys == []


def test_teardown_undeploys_owned_model_endpoints_only():
    undeployed = []
    st = ProvisionState(mechanism="modelstore", runid="r1",
                        endpoints={"m0": "e0", f"{REUSED_PREFIX}m1": "ep-y", BASELINE_LABEL: "eb"})
    ModelStoreMechanism().teardown(st, flash_undeploy=lambda env, names: undeployed.append(tuple(names)))
    assert undeployed == [("xet-dl-m0",)]


def test_teardown_on_pure_reuse_state_makes_zero_undeploy_calls():
    undeployed = []
    st = ProvisionState(mechanism="modelstore", runid="r1",
                        endpoints={f"{REUSED_PREFIX}m0": "ep-x", f"{REUSED_PREFIX}m1": "ep-y",
                                   BASELINE_LABEL: "eb"})
    ModelStoreMechanism().teardown(st, flash_undeploy=lambda env, names: undeployed.append(tuple(names)))
    assert undeployed == []


def test_manual_step_lines_list_each_owned_endpoint_with_its_model():
    st = ProvisionState(mechanism="modelstore", runid="r1", endpoints={"m0": "e0", "m1": "e1", BASELINE_LABEL: "eb"})
    text = "\n".join(manual_step_lines(load_str(_MS), st))
    assert "xet-dl-m0 (e0): cache org/x@main" in text and "xet-dl-m1 (e1): cache org/y@main" in text
