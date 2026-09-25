import json
from pathlib import Path

from runpod_testbed.mechanisms.base import WorkerSpec
from runpod_testbed.provision.flash import (
    MANIFEST_PATH, WORKER_DIR, deploy_env, flash_deploy, manifest_endpoint_ids,
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
    assert "VOLUME_NAME" not in env


def test_deploy_env_adds_volume_knobs_when_spec_wants_a_volume():
    env = deploy_env(WorkerSpec(handler="volumecache", network_volume_gb=50), _cfg(), "t", "r2")
    assert env["VOLUME_NAME"] == "xet-vc-r2" and env["VOLUME_GB"] == "50"


def test_flash_deploy_runs_cli_from_worker_dir(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    calls = []
    flash_deploy({"FLASH_ENV": "xet-r1", "MECHANISM": "shim"},
                 run=lambda argv, **kw: calls.append((argv, kw)))
    argv, kw = calls[0]
    assert argv == ["flash", "deploy", "--env", "xet-r1"]
    assert kw["cwd"] == WORKER_DIR and kw["check"] is True
    assert kw["env"]["MECHANISM"] == "shim" and kw["env"]["PATH"] == "/usr/bin"


def test_manifest_endpoint_ids_reads_flash_manifest(tmp_path):
    p = tmp_path / "flash_manifest.json"
    p.write_text(json.dumps({"resources": {
        "xet-dl-A": {"endpoint_id": "ep-a"},
        "xet-dl-baseline": {"endpoint_id": "ep-base"},
        "xet-dl-m0": {"endpoint_id": "ep-m0"},
    }}))
    assert manifest_endpoint_ids(str(p)) == {"A": "ep-a", "baseline": "ep-base", "m0": "ep-m0"}
    assert MANIFEST_PATH == "runpod_testbed/worker/.flash/flash_manifest.json"


def test_flash_app_downloaders_registry_wires_volumecache():
    # Verify the _DOWNLOADERS dict is correctly wired by checking the source.
    worker_dir = Path(WORKER_DIR).absolute()
    flash_app_source = (worker_dir / "flash_app.py").read_text()
    # Verify volumecache_download is imported
    assert "from timing import run_download, hf_download, volumecache_download" in flash_app_source
    # Verify volumecache is wired to volumecache_download in _DOWNLOADERS dict
    assert '"volumecache": volumecache_download' in flash_app_source


def test_flash_app_gates_hf_upgrade_per_endpoint_via_needs_hf_upgrade():
    # flash_app.py can't be imported here (it pulls in runpod_flash, unavailable
    # no-spend), so this pins the wiring by source: the handler must consult
    # plan.needs_hf_upgrade(plan.downloader) before running the pip upgrade,
    # rather than upgrading unconditionally for every endpoint (see
    # runpod_testbed.worker.plan.needs_hf_upgrade for the actual gate, which
    # is exercised directly in test_worker_plan.py).
    worker_dir = Path(WORKER_DIR).absolute()
    flash_app_source = (worker_dir / "flash_app.py").read_text()
    assert "from plan import needs_hf_upgrade, plan_endpoints" in flash_app_source
    assert "hf_upgrade_needed = needs_hf_upgrade(plan.downloader)" in flash_app_source
    assert "_upgrade_hf_once() if hf_upgrade_needed else (False, 0)" in flash_app_source
