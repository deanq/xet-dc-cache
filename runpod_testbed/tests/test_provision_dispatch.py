import types

import pytest

from runpod_testbed.mechanisms.base import BASELINE_LABEL, ProvisionState, state_path
from runpod_testbed.provision import up
from runpod_testbed.provision.down import teardown_all


class _FakeMech:
    name = "fake"

    def __init__(self, fail_provision_after_save=False, fail_teardown=False):
        self.fail_provision_after_save = fail_provision_after_save
        self.fail_teardown = fail_teardown
        self.torn_down = []

    def provision(self, cfg, runid):
        st = ProvisionState(mechanism=self.name, runid=runid, endpoints={"x": "e1"})
        st.save(state_path(runid))
        if self.fail_provision_after_save:
            raise RuntimeError("flash deploy exploded")
        return st

    def teardown(self, state):
        self.torn_down.append(state.runid)
        if self.fail_teardown:
            raise RuntimeError("pods gone already")

    def has_metrics(self):
        return False


def test_teardown_all_runs_mechanism_then_baseline_then_env_then_volumes_and_tolerates_errors():
    mech, undeployed, deleted, order = _FakeMech(fail_teardown=True), [], [], []
    st = ProvisionState(mechanism="fake", runid="r", volumes={"vc": "vol-1", "vc2": "vol-2"})

    def _delete(vid):
        deleted.append(vid)
        order.append("volume")
        if vid == "vol-2":
            raise RuntimeError("409 in use")

    def _undeploy(env, names):
        undeployed.append(tuple(names))
        order.append("baseline")

    errored = teardown_all(mech, st, flash_undeploy=_undeploy, delete_volume=_delete,
                           delete_env=lambda env: order.append(f"env:{env}"))
    assert mech.torn_down == ["r"]
    assert order == ["baseline", "env:xet-r", "volume", "volume"]  # volumes after every endpoint
    assert undeployed == [("xet-dl-baseline",)]
    assert deleted == ["vol-1", "vol-2"]
    assert any("pods gone already" in e for e in errored) and any("vol-2" in e for e in errored)


def test_teardown_all_deletes_volumes_via_rest_helper_by_default():
    from runpod_testbed.provision import down
    from runpod_testbed.provision.volumes import delete_network_volume
    assert down._default_volume_delete is delete_network_volume


def test_teardown_all_uses_module_default_when_delete_volume_not_given(monkeypatch):
    from runpod_testbed.provision import down
    deleted = []
    monkeypatch.setattr(down, "_default_volume_delete", lambda vid: deleted.append(vid))
    st = ProvisionState(mechanism="fake", runid="r", volumes={"vc": "vol-1"})
    monkeypatch.setattr(down, "_default_env_delete", lambda env: None)
    assert teardown_all(_FakeMech(), st, flash_undeploy=lambda env, names: None) == []
    assert deleted == ["vol-1"]


def _fake_cfg(path, mechanism_override=None):
    return types.SimpleNamespace(mechanism=mechanism_override or "fake")


def test_up_main_provisions_and_prints_runid(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    mech = _FakeMech()
    up.main(["cfg.toml"], environ={"MECHANISM": "fake"},
            get_mech=lambda name: mech, load_config=_fake_cfg,
            now=lambda fmt: "20260924-000000")
    assert ProvisionState.load("data/state-20260924-000000.json").endpoints == {"x": "e1"}
    assert "UP runid=20260924-000000 mechanism=fake" in capsys.readouterr().out


def test_up_main_tears_down_partial_state_on_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mech = _FakeMech(fail_provision_after_save=True)
    undeployed = []
    monkeypatch.setattr("runpod_testbed.provision.down.cli_undeploy",
                        lambda env, names: undeployed.append(tuple(names)))
    monkeypatch.setattr("runpod_testbed.provision.down._default_env_delete",
                        lambda env: undeployed.append(env))
    with pytest.raises(RuntimeError, match="exploded"):
        up.main(["cfg.toml"], environ={}, get_mech=lambda name: mech,
                load_config=_fake_cfg, now=lambda fmt: "r9")
    assert mech.torn_down == ["r9"]                 # partial state was torn down
    assert undeployed == [("xet-dl-baseline",), "xet-r9"]


def test_teardown_all_reports_env_delete_failure_but_still_deletes_volumes():
    deleted = []
    st = ProvisionState(mechanism="fake", runid="r", volumes={"vc": "vol-1"})

    def _boom(env):
        raise RuntimeError("endpoint has running workers")

    errored = teardown_all(_FakeMech(), st, flash_undeploy=lambda env, names: None,
                           delete_volume=deleted.append, delete_env=_boom)
    assert deleted == ["vol-1"]
    assert any("xet-r" in e and "running workers" in e for e in errored)
