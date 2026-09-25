from dataclasses import replace

from runpod_testbed.config import load_str
from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, WorkerSpec
from runpod_testbed.mechanisms.volumecache import VOLUME_LABEL, VolumeCacheMechanism
from runpod_testbed.provision.flash import volume_name
from runpod_testbed.tests.test_config import VALID_VOLUMECACHE

_ENV = {"HF_TOKEN": "hf_t", "RUNPOD_API_KEY": "rk"}


def _cfg():
    return load_str(VALID_VOLUMECACHE)   # models x,y ; burst 4 ; volume_gb 50


def test_identity_spec_and_no_metrics():
    m = VolumeCacheMechanism()
    assert m.name == "volumecache" and m.has_metrics() is False
    assert m.worker_spec(_cfg()) == WorkerSpec(handler="volumecache", network_volume_gb=50)
    assert m.report_sections([], []) == []


def test_jobs_populate_then_warm_burst_per_model():
    jobs = VolumeCacheMechanism().jobs(replace(_cfg(), burst=2))
    assert [(j["model"], j["phase"], j["replica"]) for j in jobs] == [
        ("org/x@main", "populate", 0), ("org/x@main", "warm", 0), ("org/x@main", "warm", 1),
        ("org/y@main", "populate", 0), ("org/y@main", "warm", 0), ("org/y@main", "warm", 1),
    ]
    assert all(j["endpoint"] == VOLUME_LABEL and j["mechanism"] == "volumecache" for j in jobs)


def test_provision_deploys_with_volume_and_records_volume_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deploys, listed = [], []

    def _list(api_key):
        listed.append(api_key)
        return [{"id": "vol-77", "name": volume_name("r1"), "dataCenterId": "EU-RO-1"}]

    st = VolumeCacheMechanism().provision(
        _cfg(), "r1", deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {VOLUME_LABEL: "ep-vc", BASELINE_LABEL: "ep-base"},
        list_volumes=_list, environ=_ENV)
    env = deploys[0]
    assert env["MECHANISM"] == "volumecache" and env["VOLUME_GB"] == "50"
    assert env["VOLUME_NAME"] == "xet-vc-r1" and env["MODELS"] == "org/x@main,org/y@main"
    assert listed == ["rk"]
    assert st.endpoints == {VOLUME_LABEL: "ep-vc", BASELINE_LABEL: "ep-base"}
    assert st.volumes == {VOLUME_LABEL: "vol-77"} and st.pods == {}
    assert ProvisionState.load("data/state-r1.json") == st
    assert open("data/last-runid").read() == "r1"


def test_provision_warns_but_succeeds_when_volume_not_found(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    st = VolumeCacheMechanism().provision(
        _cfg(), "r2", deploy=lambda env: None,
        manifest_ids=lambda: {VOLUME_LABEL: "e", BASELINE_LABEL: "b"},
        list_volumes=lambda k: [], environ=_ENV)
    assert st.volumes == {}
    out = capsys.readouterr().out
    assert "xet-vc-r2" in out and "console" in out


def test_teardown_undeploys_only_its_endpoint():
    undeployed = []
    VolumeCacheMechanism().teardown(
        ProvisionState(mechanism="volumecache", runid="r1", volumes={VOLUME_LABEL: "vol-77"}),
        flash_undeploy=lambda env, names: undeployed.append((env, tuple(names))))
    assert undeployed == [("xet-r1", ("xet-dl-volumecache",))]
