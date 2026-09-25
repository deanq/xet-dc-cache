from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, WorkerSpec
from runpod_testbed.mechanisms.shim import ShimMechanism
from runpod_testbed.provision.down import ENDPOINTS
from runpod_testbed.tests.test_provision import _cfg

_PORTS = [{"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 8000, "publicPort": 41001, "type": "tcp"}]


class _FakeFleet:
    def __init__(self):
        self.created, self.terminated = [], []

    def create_cache_pod(self, name, image, instance_id, disk_gb, dc, env):
        self.created.append({"name": name, "image": image, "dc": dc, "env": env})
        return f"pod-{name[-1]}"

    def get_pod_ports(self, pid):
        return _PORTS

    def terminate_pod(self, pid):
        self.terminated.append(pid)


def _cfg3():
    from dataclasses import replace
    return replace(_cfg(), models=["x", "y", "z"],
                   overlap={"A": ["x", "y"], "B": ["y", "z"], "C": ["z", "x"]})


def test_shim_identity_and_worker_spec():
    m = ShimMechanism()
    assert m.name == "shim" and m.has_metrics() is True
    assert m.worker_spec(_cfg3()) == WorkerSpec(handler="shim")


def test_shim_jobs_are_todays_expand_jobs_with_labels():
    jobs = ShimMechanism().jobs(_cfg3())
    assert len(jobs) == 6 + 6 * 3                       # 6 cold + 6 (group,model) * burst 3
    assert all(j["mechanism"] == "shim" and j["endpoint"] == j["group"] for j in jobs)
    assert jobs[0]["phase"] == "cold" and jobs[-1]["phase"] == "warm"


def test_shim_provision_creates_pods_deploys_flash_and_records_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                        # state lands in ./data
    fleet, deploys = _FakeFleet(), []
    environ = {"HF_TOKEN": "hf_t", "SHIM_AUTH_TOKEN": "s3", "RUNPOD_API_KEY": "rk"}
    st = ShimMechanism().provision(
        _cfg3(), "r1", fleet=fleet, deploy=lambda env: deploys.append(env),
        manifest_ids=lambda: {"A": "ep-a", "B": "ep-b", "C": "ep-c", BASELINE_LABEL: "ep-base"},
        wait_healthz=lambda addr, timeout_s: None, environ=environ)
    assert [c["name"] for c in fleet.created] == ["xet-cache-r1-A", "xet-cache-r1-B", "xet-cache-r1-C"]
    assert fleet.created[0]["env"]["FLEET_PREFIX"] == "xet-cache-r1-"
    assert fleet.created[0]["env"]["FLEET_SIZE"] == "3" and fleet.created[0]["env"]["SHIM_AUTH_TOKEN"] == "s3"
    assert st.pods == {"A": "pod-A", "B": "pod-B", "C": "pod-C"}
    assert st.metrics_urls["B"] == "http://1.2.3.4:41001"
    assert st.endpoints[BASELINE_LABEL] == "ep-base" and st.endpoints["A"] == "ep-a"
    env = deploys[0]
    assert env["POD_ADDR_C"] == "http://1.2.3.4:41001" and env["MECHANISM"] == "shim"
    assert env["FLASH_ENV"] == "xet-r1" and env["HF_TOKEN"] == "hf_t"
    assert ProvisionState.load("data/state-r1.json") == st  # saved incrementally
    assert open("data/last-runid").read() == "r1"


def test_shim_teardown_undeploys_its_endpoints_then_terminates_pods():
    fleet, undeployed = _FakeFleet(), []
    st = ProvisionState(mechanism="shim", runid="r1", pods={"A": "pA", "B": "pB"})
    ShimMechanism().teardown(st, fleet=fleet,
                             flash_undeploy=lambda env, names=ENDPOINTS: undeployed.append(tuple(names)))
    assert undeployed == [ENDPOINTS]
    assert fleet.terminated == ["pA", "pB"]
