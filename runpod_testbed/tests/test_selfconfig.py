import pytest

from runpod_testbed.provision.selfconfig import NotReady, assemble_env, discover_ids


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
