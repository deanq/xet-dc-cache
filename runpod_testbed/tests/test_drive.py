import json
import sys
import types

from runpod_testbed.drive.run import expand_jobs, endpoint_ids, _submit_and_wait

def test_expand_cold_then_warm():
    overlap = {"A": ["x", "y"], "B": ["y", "z"]}
    jobs = expand_jobs(overlap, burst=3)
    cold = [j for j in jobs if j["phase"] == "cold"]
    warm = [j for j in jobs if j["phase"] == "warm"]
    assert len(cold) == 4                      # 2 groups * 2 models
    assert len(warm) == 4 * 3                   # each (group,model) * burst
    # all cold jobs precede all warm jobs
    assert jobs.index(cold[-1]) < jobs.index(warm[0])
    assert {j["group"] for j in jobs} == {"A", "B"}

def test_endpoint_ids_maps_group_to_id():
    # Real flash_manifest.json shape (verified live 2026-09-22): a "resources"
    # dict keyed by endpoint name, each carrying its endpoint_id.
    manifest = {"resources": {
        "xet-dl-A": {"functions": [{"name": "xet_dl_A"}], "endpoint_id": "ep-a"},
        "xet-dl-B": {"functions": [{"name": "xet_dl_B"}], "endpoint_id": "ep-b"},
    }}
    assert endpoint_ids(manifest) == {"A": "ep-a", "B": "ep-b"}


class _FakeHandle:
    def __init__(self, output=None, raise_timeout=False):
        self._output, self._raise = output, raise_timeout
        self.output_calls = []

    def output(self, timeout=0):
        self.output_calls.append(timeout)
        if self._raise:
            raise TimeoutError("Job timed out.")
        return self._output

    def status(self):
        return "IN_QUEUE"


def _install_fake_runpod(monkeypatch, handle):
    fake = types.ModuleType("runpod")
    fake.Endpoint = lambda eid: types.SimpleNamespace(run=lambda payload: handle)
    monkeypatch.setitem(sys.modules, "runpod", fake)


def test_submit_and_wait_passes_real_timeout_not_zero(tmp_path, monkeypatch):
    # The bug: Job.output(timeout=0) returns None immediately without polling.
    # drive must pass the configured ceiling so it actually waits for a result.
    handle = _FakeHandle(output={"ok": True, "results": []})
    _install_fake_runpod(monkeypatch, handle)
    jobs_path = tmp_path / "jobs.jsonl"
    job = {"group": "A", "model": "org/m@main", "phase": "cold", "replica": 0}

    _submit_and_wait("ep-a", job, str(jobs_path), timeout_s=600)

    assert handle.output_calls == [600]
    row = json.loads(jobs_path.read_text().strip())
    assert row["result"] == {"ok": True, "results": []}


def test_submit_and_wait_records_timeout_instead_of_crashing(tmp_path, monkeypatch):
    handle = _FakeHandle(raise_timeout=True)
    _install_fake_runpod(monkeypatch, handle)
    jobs_path = tmp_path / "jobs.jsonl"
    job = {"group": "A", "model": "org/m@main", "phase": "warm", "replica": 1}

    _submit_and_wait("ep-a", job, str(jobs_path), timeout_s=5)

    row = json.loads(jobs_path.read_text().strip())
    assert row["result"]["ok"] is False
    assert "timed out" in row["result"]["error"]
    assert row["result"]["status"] == "IN_QUEUE"
