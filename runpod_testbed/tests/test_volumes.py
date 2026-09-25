import json
import urllib.error

import pytest

from runpod_testbed.provision.volumes import (
    REST_BASE, delete_network_volume, find_volume_id, list_network_volumes,
)


class _Resp:
    def __init__(self, status, body=b""):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(status=200, body=b"[]", seen=None):
    def open_(req, timeout=None):
        if seen is not None:
            seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, None)
        return _Resp(status, body)
    return open_


def test_list_handles_bare_list_and_wrapped_shapes():
    vols = [{"id": "v1", "name": "xet-vc-r", "dataCenterId": "EU-RO-1"}]
    assert list_network_volumes("k", opener=_opener(body=json.dumps(vols).encode())) == vols
    assert list_network_volumes("k", opener=_opener(body=json.dumps({"networkVolumes": vols}).encode())) == vols


def test_list_sends_bearer_auth_to_rest_endpoint():
    seen = []
    list_network_volumes("secret", opener=_opener(seen=seen))
    assert seen == [("GET", f"{REST_BASE}/networkvolumes", "Bearer secret")]


def test_find_volume_id_by_name():
    vols = [{"id": "v1", "name": "a"}, {"id": "v2", "name": "xet-vc-r"}]
    assert find_volume_id("xet-vc-r", vols) == "v2"
    assert find_volume_id("nope", vols) is None


def test_delete_issues_delete_and_accepts_204():
    seen = []
    delete_network_volume("v2", api_key="k", opener=_opener(status=204, seen=seen))
    assert seen == [("DELETE", f"{REST_BASE}/networkvolumes/v2", "Bearer k")]


def test_delete_tolerates_404_already_gone():
    delete_network_volume("v2", api_key="k", opener=_opener(status=404))   # must not raise


def test_delete_raises_on_other_errors():
    with pytest.raises(RuntimeError, match="500"):
        delete_network_volume("v2", api_key="k", opener=_opener(status=500))
