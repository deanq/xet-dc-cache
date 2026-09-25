import json
import sys
import types

import pytest

from runpod_testbed.drive.run import (
    expand_jobs, endpoint_ids, _submit_and_wait, start_jobs_file, record,
    SEQUENTIAL_PHASES, resolve_endpoints, split_jobs,
)

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


def test_submit_and_wait_records_transient_api_error_without_crashing(tmp_path, monkeypatch):
    # A transient Runpod-API error during polling (not a TimeoutError) must be
    # recorded, not raised — otherwise one flaky poll aborts the whole run.
    class _BoomHandle:
        job_id = "j"
        def output(self, timeout=0):
            raise RuntimeError("Read timed out")
        def status(self):
            raise RuntimeError("api unreachable")
    _install_fake_runpod(monkeypatch, _BoomHandle())
    jobs_path = tmp_path / "jobs.jsonl"
    job = {"group": "A", "model": "org/m@main", "phase": "warm", "replica": 0}

    _submit_and_wait("ep-a", job, str(jobs_path), timeout_s=5)

    row = json.loads(jobs_path.read_text().strip())
    assert row["result"]["ok"] is False
    assert "RuntimeError" in row["result"]["error"]
    assert row["result"]["status"] is None   # status() also failed, guarded


def test_start_jobs_file_truncates_so_reruns_dont_double_count(tmp_path):
    # Regression: record() appends, so a second drive pass on the same RUNID
    # must not accumulate on top of the first pass's rows.
    jobs_path = str(tmp_path / "jobs.jsonl")
    job = {"group": "A", "model": "org/m@main", "phase": "cold", "replica": 0}
    start_jobs_file(jobs_path)
    record(job, {"ok": True}, 1.0, jobs_path)
    record(job, {"ok": True}, 2.0, jobs_path)
    assert len(open(jobs_path).read().splitlines()) == 2

    # A fresh run resets the file rather than appending a third+fourth row.
    start_jobs_file(jobs_path)
    record(job, {"ok": True}, 3.0, jobs_path)
    assert len(open(jobs_path).read().splitlines()) == 1


def test_start_jobs_file_creates_missing_parent_dir(tmp_path):
    jobs_path = str(tmp_path / "nested" / "jobs.jsonl")
    start_jobs_file(jobs_path)
    assert open(jobs_path).read() == ""


def test_record_adds_shared_timing_row_when_job_carries_mechanism(tmp_path):
    jobs_path = str(tmp_path / "jobs.jsonl")
    job = {"mechanism": "baseline", "endpoint": "baseline", "model": "org/m@main",
           "phase": "baseline", "replica": 0}
    result = {"cold_first_invocation": False, "dep_upgrade_ms": 0,
              "results": [{"ok": True, "bytes": 42, "wall_seconds": 1.0, "model": "org/m@main",
                           "first_byte_ms": 1, "error": None,
                           "breakdown": {"download_s": 0.9, "hydrate_s": None, "local_read_s": None}}]}
    record(job, result, submit_ts=100.0, path=jobs_path)
    row = json.loads(open(jobs_path).read())
    t = row["timing"]
    assert set(t) == {"mechanism", "phase", "model", "wall_seconds", "bytes", "breakdown", "worker_cold", "ok"}
    assert t["mechanism"] == "baseline" and t["phase"] == "baseline" and t["bytes"] == 42
    assert t["wall_seconds"] == row["return_ts"] - 100.0      # driver-observed, not handler wall
    assert t["breakdown"]["download_s"] == 0.9 and t["ok"] is True


def test_record_without_mechanism_stays_legacy(tmp_path):
    jobs_path = str(tmp_path / "jobs.jsonl")
    record({"group": "A", "model": "m", "phase": "cold", "replica": 0}, {"ok": True}, 1.0, jobs_path)
    assert "timing" not in json.loads(open(jobs_path).read())


def test_split_jobs_sequential_first_then_burst():
    jobs = [{"phase": "warm"}, {"phase": "baseline"}, {"phase": "cold"}, {"phase": "populate"}]
    seq, burst = split_jobs(jobs)
    assert [j["phase"] for j in seq] == ["baseline", "cold", "populate"]
    assert burst == [{"phase": "warm"}] and SEQUENTIAL_PHASES == ("baseline", "cold", "populate")


def test_resolve_endpoints_fails_before_spend_on_missing_label():
    jobs = [{"endpoint": "baseline"}, {"endpoint": "volumecache"}]
    assert resolve_endpoints(jobs, {"baseline": "e0", "volumecache": "e1", "extra": "e2"}) == \
        {"baseline": "e0", "volumecache": "e1"}
    with pytest.raises(ValueError, match="volumecache"):
        resolve_endpoints(jobs, {"baseline": "e0"})
