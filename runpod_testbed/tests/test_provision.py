from runpod_testbed.provision.up import flash_deploy_env, State
from runpod_testbed.provision.down import teardown, cli_undeploy, ENDPOINTS

def _cfg(**overrides):
    from runpod_testbed.config import Config
    return Config(dc="EU-RO-1", registry="r", cache_image="c",
                  worker_cpu="cpu5c-4-8", worker_deps=["huggingface_hub"],
                  models=["x"], overlap={"A": ["x"]}, burst=3, max_pods=3,
                  max_burst=8, pod_instance_id="cpu3c-2-4",
                  container_disk_gb=60, scrape_interval_s=5, job_timeout_s=600,
                  **overrides)

def test_flash_deploy_env_maps_addrs_and_worker_knobs():
    env = flash_deploy_env({"A": "http://1:41", "B": "http://2:42", "C": "http://3:43"},
                           _cfg(), "hf_throwaway")
    assert env["POD_ADDR_A"] == "http://1:41"
    assert env["POD_ADDR_C"] == "http://3:43"
    assert env["HF_TOKEN"] == "hf_throwaway"
    assert env["WORKER_CPU"] == "cpu5c-4-8"
    assert env["WORKER_DEPS"] == "huggingface_hub"
    assert env["WORKER_MAX"] == "3"
    assert env["WORKER_GPU"] == ""

def test_flash_deploy_env_carries_worker_gpu_when_set():
    env = flash_deploy_env({"A": "http://1:41"}, _cfg(worker_gpu="AMPERE_16"), "hf_throwaway")
    assert env["WORKER_GPU"] == "AMPERE_16"

def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    State("run1", ["pA", "pB"], "run1-env").save(str(p))
    got = State.load(str(p))
    assert got.pods == ["pA", "pB"] and got.flash_env == "run1-env"

class _FakeFleet:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)
    def terminate_pod(self, i):
        self.calls.append(("pod", i))
        if i in self.fail: raise RuntimeError("already gone")

def test_teardown_undeploys_then_terminates_and_tolerates_errors():
    f = _FakeFleet(fail={"pB"})
    seen = []
    errored = teardown(f, State("r", ["pA", "pB"], "r-env"),
                       flash_undeploy=lambda env: seen.append(env))
    assert seen == ["r-env"]                        # endpoints (Flash) torn down first
    assert f.calls == [("pod", "pA"), ("pod", "pB")]
    assert errored == ["pB"]

def test_teardown_tolerates_flash_undeploy_error():
    f = _FakeFleet()
    def boom(env): raise RuntimeError("flash cli missing")
    errored = teardown(f, State("r", ["pA"], "r-env"), flash_undeploy=boom)
    assert "r-env" in errored and f.calls == [("pod", "pA")]  # pods still torn down

def test_cli_undeploy_removes_each_endpoint_by_name_without_env(monkeypatch):
    import runpod_testbed.provision.down as down
    calls = []
    monkeypatch.setattr(down.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)))
    cli_undeploy("ignored-env")
    assert [c[0] for c in calls] == [
        ["flash", "undeploy", name, "--force"] for name in ENDPOINTS
    ]
    # flash undeploy has no --env, and --all is unsafe (nukes unrelated endpoints)
    for argv, kw in calls:
        assert "--env" not in argv and "--all" not in argv
        assert kw.get("check") is False  # tolerate not-found
        # must run from the worker dir or flash reports "no endpoints found"
        assert kw.get("cwd") == "runpod_testbed/worker"
