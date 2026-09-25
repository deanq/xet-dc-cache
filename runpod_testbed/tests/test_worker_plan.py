import pytest

from runpod_testbed.worker.plan import HF_HOME_DEFAULT, EndpointPlan, plan_endpoints

MODELS = ["org/a@main", "org/b@main"]


def _by_name(plans):
    return {p.name: p for p in plans}


def test_every_mechanism_gets_the_baseline_endpoint():
    for mech in ("shim", "volumecache", "modelstore"):
        base = _by_name(plan_endpoints(mech, MODELS, {}))["xet-dl-baseline"]
        assert base.binding == "xet_dl_baseline" and base.downloader == "baseline"
        assert "HF_ENDPOINT" not in base.env and base.volume_gb is None
        assert base.env["MECHANISM"] == mech


def test_shim_plan_matches_todays_endpoints():
    env = {"POD_ADDR_A": "http://1:41", "POD_ADDR_B": "http://2:42", "POD_ADDR_C": "http://3:43"}
    plans = _by_name(plan_endpoints("shim", MODELS, env))
    assert set(plans) == {"xet-dl-A", "xet-dl-B", "xet-dl-C", "xet-dl-baseline"}
    assert plans["xet-dl-A"] == EndpointPlan(
        name="xet-dl-A", binding="xet_dl_A", downloader="shim",
        env={"HF_ENDPOINT": "http://1:41", "MECHANISM": "shim", "MODELS": "org/a@main,org/b@main"},
        volume_gb=None)


def test_shim_plan_tolerates_missing_pod_addrs_at_worker_runtime():
    # Worker re-import: POD_ADDR_* absent -> HF_ENDPOINT "" (never a KeyError).
    plans = _by_name(plan_endpoints("shim", MODELS, {}))
    assert plans["xet-dl-B"].env["HF_ENDPOINT"] == ""


def test_volumecache_plan_single_endpoint_with_volume():
    plans = _by_name(plan_endpoints("volumecache", MODELS, {"VOLUME_GB": "50", "VOLUME_NAME": "xet-vc-r"}))
    vc = plans["xet-dl-volumecache"]
    assert vc.binding == "xet_dl_volumecache" and vc.downloader == "volumecache"
    assert vc.volume_gb == 50 and vc.env["HF_HOME"] == HF_HOME_DEFAULT
    assert vc.env["VOLUME_NAME"] == "xet-vc-r"


def test_modelstore_plan_one_endpoint_per_model():
    plans = _by_name(plan_endpoints("modelstore", MODELS, {}))
    assert plans["xet-dl-m0"].env["MODEL"] == "org/a@main"
    assert plans["xet-dl-m1"].env["MODEL"] == "org/b@main"
    assert plans["xet-dl-m1"].binding == "xet_dl_m1"
    assert plans["xet-dl-m0"].downloader == "modelstore" and plans["xet-dl-m0"].volume_gb is None


def test_modelstore_plan_with_no_models_is_baseline_only():
    assert [p.name for p in plan_endpoints("modelstore", [], {})] == ["xet-dl-baseline"]


def test_unknown_mechanism_is_an_error():
    with pytest.raises(ValueError, match="mechanism"):
        plan_endpoints("turbo", MODELS, {})
