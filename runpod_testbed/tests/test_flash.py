import sys
from pathlib import Path

import pytest

from runpod_testbed.mechanisms.base import WorkerSpec
from runpod_testbed.provision import flash
from runpod_testbed.provision.flash import (
    APP_NAME, RP_CLI, WORKER_DIR, deploy_env, endpoint_labels, flash_app_name, flash_deploy,
    flash_env_name,
)
from runpod_testbed.tests.test_provision import _cfg


def test_deploy_env_carries_mechanism_models_and_worker_knobs():
    cfg = _cfg()
    spec = WorkerSpec(handler="shim", env={"POD_ADDR_A": "http://1:41"})
    env = deploy_env(spec, cfg, hf_token="hf_x", runid="r1")
    assert env["MECHANISM"] == "shim" and env["MODELS"] == "x"
    assert env["POD_ADDR_A"] == "http://1:41" and env["HF_TOKEN"] == "hf_x"
    assert env["WORKER_CPU"] == "cpu5c-4-8" and env["WORKER_DEPS"] == "huggingface_hub"
    assert env["WORKER_MAX"] == "3" and env["FLASH_ENV"] == "xet-r1"
    assert env["FLASH_APP"] == "xet-testbed-r1"
    assert env["WORKER_DC"] == "EU-RO-1"
    assert "VOLUME_NAME" not in env
    assert env["WORKER_GPU"] == ""


def test_deploy_env_carries_worker_gpu_when_set():
    cfg = _cfg(worker_gpu="AMPERE_16")
    env = deploy_env(WorkerSpec(handler="shim"), cfg, hf_token="hf_x", runid="r1")
    assert env["WORKER_GPU"] == "AMPERE_16"


def test_deploy_env_adds_volume_knobs_when_spec_wants_a_volume():
    env = deploy_env(WorkerSpec(handler="volumecache", network_volume_gb=50), _cfg(), "t", "r2")
    assert env["VOLUME_NAME"] == "xet-vc-r2" and env["VOLUME_GB"] == "50"


def test_flash_deploy_runs_rp_cli_from_worker_dir_into_the_runs_env(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    calls = []
    flash_deploy({"FLASH_ENV": "xet-r1", "MECHANISM": "shim"},
                 run=lambda argv, **kw: calls.append((argv, kw)))
    argv, kw = calls[0]
    assert argv == [sys.executable, "-m", "runpod.rp_cli.main",
                    "flash", "deploy", ".", "--env", "xet-r1"]
    assert tuple(argv[:3]) == RP_CLI
    assert kw["cwd"] == WORKER_DIR and kw["check"] is True
    assert kw["env"]["MECHANISM"] == "shim" and kw["env"]["PATH"] == "/usr/bin"


def test_endpoint_labels_keys_by_trailing_label():
    eps = [{"name": "xet-dl-A", "id": "ep-a"}, {"name": "xet-dl-baseline", "id": "ep-base"},
           {"name": "xet-dl-m0", "id": "ep-m0"}]
    assert endpoint_labels(eps) == {"A": "ep-a", "baseline": "ep-base", "m0": "ep-m0"}


# --- registry-backed helpers (runpod.apps.manage / AppsApiClient faked) -----

class _FakeClient:
    def __init__(self, envs: dict, apps_envs=None):
        self.envs, self.deleted_endpoints, self.deleted_apps = envs, [], []
        self.deleted_templates = []
        self.apps_envs = apps_envs if apps_envs is not None else []

    async def delete_endpoint(self, eid):
        self.deleted_endpoints.append(eid)
        return True

    async def delete_app(self, app_id):
        self.deleted_apps.append(app_id)
        return True


@pytest.fixture
def fake_registry(monkeypatch):
    import runpod.apps.api as api
    import runpod.apps.manage as manage

    client = _FakeClient({})

    async def get_environment(app, env, api=None):
        assert app == flash_app_name(env)
        if env not in client.envs:
            raise manage.EnvironmentNotFound(app, env)
        return client.envs[env]

    async def undeploy_environment(app, env, api=None):
        for ep in client.envs.pop(env)["endpoints"]:
            client.deleted_endpoints.append(ep["id"])
        return manage.UndeployResult(endpoints_deleted=1, environment_deleted=True)

    async def get_app(app, api=None):
        return {"id": "app-1", "flashEnvironments": client.apps_envs}

    def delete_templates(app, resources):
        client.deleted_templates.append((app, resources))
        return []

    monkeypatch.setattr(api, "AppsApiClient", lambda: client)
    monkeypatch.setattr(manage, "get_environment", get_environment)
    monkeypatch.setattr(manage, "undeploy_environment", undeploy_environment)
    monkeypatch.setattr(manage, "get_app", get_app)
    monkeypatch.setattr(flash, "delete_flash_templates", delete_templates)
    return client


def test_environment_endpoint_ids_reads_the_runs_environment(fake_registry):
    fake_registry.envs["xet-r1"] = {"id": "env-1", "endpoints": [
        {"name": "xet-dl-A", "id": "ep-a"}, {"name": "xet-dl-baseline", "id": "ep-b"}]}
    assert flash.environment_endpoint_ids("xet-r1") == {"A": "ep-a", "baseline": "ep-b"}


def test_environment_endpoint_ids_fails_loudly_when_env_missing(fake_registry):
    with pytest.raises(RuntimeError, match="xet-r1"):
        flash.environment_endpoint_ids("xet-r1")


def test_undeploy_endpoints_deletes_only_named_endpoints(fake_registry):
    fake_registry.envs["xet-r1"] = {"id": "env-1", "endpoints": [
        {"name": "xet-dl-A", "id": "ep-a"}, {"name": "xet-dl-baseline", "id": "ep-b"}]}
    flash.undeploy_endpoints("xet-r1", ("xet-dl-A", "xet-dl-B"))
    assert fake_registry.deleted_endpoints == ["ep-a"]


def test_undeploy_endpoints_tolerates_missing_env(fake_registry):
    flash.undeploy_endpoints("xet-never", ("xet-dl-A",))
    assert fake_registry.deleted_endpoints == []


def test_delete_environment_removes_env_app_and_leaked_templates(fake_registry):
    fake_registry.envs["xet-r1"] = {"id": "env-1", "endpoints": [{"name": "xet-dl-B", "id": "ep-b"}]}
    flash.delete_environment("xet-r1", ("xet-dl-A",))   # A was already undeployed by name
    assert fake_registry.deleted_endpoints == ["ep-b"] and fake_registry.deleted_apps == ["app-1"]
    assert fake_registry.deleted_templates == [("xet-testbed-r1", ["xet-dl-A", "xet-dl-B"])]


def test_delete_environment_keeps_app_that_still_has_an_env(fake_registry):
    fake_registry.apps_envs = [{"name": "xet-other"}]
    flash.delete_environment("xet-r1")   # env already gone: no-op, app kept
    assert fake_registry.deleted_apps == []


def test_delete_environment_raises_template_failures(fake_registry, monkeypatch):
    monkeypatch.setattr(flash, "delete_flash_templates", lambda app, res: ["template t: held"])
    with pytest.raises(RuntimeError, match="held"):
        flash.delete_environment("xet-r1", ("xet-dl-A",))


def test_env_and_app_names_are_per_run():
    assert flash_env_name("r1") == "xet-r1"
    assert flash_app_name("xet-r1") == f"{APP_NAME}-r1" == "xet-testbed-r1"


# --- the worker app, discovered exactly as `rp flash deploy` does ------------

_BASE_ENV = {"MODELS": "org/a@main,org/b@main", "HF_TOKEN": "hf_x", "WORKER_MAX": "4",
             "WORKER_DEPS": "huggingface_hub>=1.32.0,hf_xet>=1.6.0", "WORKER_GPU": "",
             "WORKER_CPU": "cpu5c-4-8", "WORKER_DC": "EU-RO-1"}


@pytest.fixture
def isolated_modules():
    # discover_apps evicts every already-imported module under the project root
    # (here: all of runpod_testbed.*) so it can re-import fresh; restore them so
    # later tests keep the module objects they imported at collection time.
    saved = dict(sys.modules)
    yield
    sys.modules.clear()
    sys.modules.update(saved)
    for name, module in saved.items():   # discovery also delattr's submodules off parents
        parent, _, child = name.rpartition(".")
        if parent in saved and module is not None:
            setattr(saved[parent], child, module)


def _discover_apps(monkeypatch, env):
    from runpod.apps.discovery import discover_apps
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return discover_apps(Path(WORKER_DIR).resolve())


def _discover(monkeypatch, **env):
    env = {**_BASE_ENV, **env}
    apps = _discover_apps(monkeypatch, env)
    assert [a.name for a in apps] == [env.get("FLASH_APP", APP_NAME)]
    return {name: h.spec for name, h in apps[0].resources.items()}


def test_worker_app_shim_resources(monkeypatch, isolated_modules):
    specs = _discover(monkeypatch, MECHANISM="shim", POD_ADDR_A="http://1:41",
                      FLASH_APP="xet-testbed-r1")
    assert set(specs) == {"xet-dl-A", "xet-dl-B", "xet-dl-C", "xet-dl-baseline"}
    a = specs["xet-dl-A"]
    a.validate()
    assert a.kind.value == "queue" and a.cpu == ["cpu5c-4-8"] and a.gpu is None
    assert a.workers == (0, 4) and a.idle_timeout == 30 and a.datacenter == ["EU-RO-1"]
    assert a.env["HF_ENDPOINT"] == "http://1:41" and a.env["HF_TOKEN"] == "hf_x"
    assert a.dependencies == ["huggingface_hub>=1.32.0", "hf_xet>=1.6.0"]
    assert not a.mounts


def test_worker_app_volumecache_mounts_network_volume(monkeypatch, isolated_modules):
    specs = _discover(monkeypatch, MECHANISM="volumecache", VOLUME_NAME="xet-vc-r1", VOLUME_GB="10")
    vc = specs["xet-dl-volumecache"]
    vc.validate()
    vol = vc.mounts["/runpod-volume"]
    assert vol.name == "xet-vc-r1" and vol.size == 10 and vol.datacenter == "EU-RO-1"


def test_worker_app_gpu_switch(monkeypatch, isolated_modules):
    specs = _discover(monkeypatch, MECHANISM="modelstore", WORKER_GPU="AMPERE_16,ADA_24")
    m0 = specs["xet-dl-m0"]
    m0.validate()
    assert m0.cpu is None and m0.gpu and m0.gpu_count == 1


def test_worker_app_manifest_resolves_to_flash_app_module(monkeypatch, isolated_modules):
    # The deployed runtime imports entry["module"] from the artifact root (worker/).
    from runpod.apps.deploy import build_manifest
    app = _discover_apps(monkeypatch, {**_BASE_ENV, "MECHANISM": "shim"})[0]
    manifest = build_manifest(app, Path(WORKER_DIR).resolve())
    assert {r["module"] for r in manifest["resources"]} == {"flash_app"}
