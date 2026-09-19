import os

import pytest

from runpod_testbed.provision import selfconfig
from runpod_testbed.provision.selfconfig import (
    NotReady,
    assemble_env,
    discover_ids,
    expected_fleet_size,
)


def test_assemble_builds_public_base_and_peers():
    addrs = {
        "self": "http://1.1.1.1:41001",
        "b": "http://2.2.2.2:41002",
        "c": "http://3.3.3.3:41003",
    }
    env = assemble_env(
        "self", ["b", "c"], addrs, token="secret", extra={"XORB_CACHE_MAX_GIB": "50"}
    )
    assert env["PUBLIC_BASE"] == "http://1.1.1.1:41001"
    assert env["SELF_URL"] == "http://1.1.1.1:41001"
    assert set(env["PEERS"].split(",")) == {
        "http://2.2.2.2:41002",
        "http://3.3.3.3:41003",
    }
    assert env["SHIM_AUTH_TOKEN"] == "secret"
    assert env["XORB_CACHE_MAX_GIB"] == "50"


def test_assemble_raises_when_peer_missing():
    with pytest.raises(NotReady):
        assemble_env("self", ["b"], {"self": "http://1.1.1.1:41001"}, token="s", extra={})


def test_discover_ids_excludes_own_id():
    pods = [{"id": "self", "name": "cache-0"}, {"id": "b", "name": "cache-1"}, {"id": "c", "name": "cache-2"}]
    assert set(discover_ids(pods, "self")) == {"b", "c"}


def test_expected_fleet_size_returns_int():
    assert expected_fleet_size({"FLEET_SIZE": "3"}) == 3


def test_expected_fleet_size_raises_when_unset():
    with pytest.raises(NotReady):
        expected_fleet_size({})


def test_expected_fleet_size_raises_when_zero():
    with pytest.raises(NotReady):
        expected_fleet_size({"FLEET_SIZE": "0"})


def test_expected_fleet_size_raises_when_not_an_integer():
    with pytest.raises(NotReady):
        expected_fleet_size({"FLEET_SIZE": "abc"})


class _FakeFleet:
    """Stand-in for provision.fleet.Fleet: single-pod fleet, always ready."""

    def __init__(self, *args, **kwargs):
        pass

    def list_pods_by_prefix(self, prefix):
        return [{"id": "self-id", "name": "cache-0"}]

    def get_pod_ports(self, pod_id):
        return [
            {"type": "tcp", "isIpPublic": True, "privatePort": 8000,
             "ip": "1.2.3.4", "publicPort": 41001}
        ]


def test_main_execs_xetcache_without_nameerror(monkeypatch):
    """Regression test for the `own_id` NameError in main(): a one-pod fleet
    should sail through the readiness loop and reach os.execvp("xetcache", ...)
    -- proving main() no longer references an undefined `own_id`.
    """
    monkeypatch.setenv("RUNPOD_POD_ID", "self-id")
    monkeypatch.setenv("FLEET_PREFIX", "cache-")
    monkeypatch.setenv("FLEET_SIZE", "1")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-key")
    monkeypatch.setattr(selfconfig, "Fleet", _FakeFleet)

    exec_calls = []

    def fake_execvp(file, args):
        exec_calls.append((file, args))

    monkeypatch.setattr(selfconfig.os, "execvp", fake_execvp)

    selfconfig.main()

    assert exec_calls == [("xetcache", ["xetcache"])]
    assert os.environ["PUBLIC_BASE"] == "http://1.2.3.4:41001"
    assert os.environ["PEERS"] == ""
