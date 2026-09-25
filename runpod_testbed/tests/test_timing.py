from runpod_testbed.worker.timing import time_one, run_download
from runpod_testbed.worker.timing import EMPTY_BREAKDOWN, make_timing_row, schema_phase
from runpod_testbed.worker.timing import volumecache_download

def test_time_one_records_bytes_and_timing():
    out = time_one(lambda m: (1_000_000, 0.5), "org/x@main")
    assert out["ok"] is True
    assert out["bytes"] == 1_000_000
    assert out["first_byte_ms"] == 500
    assert out["wall_seconds"] >= 0

def test_time_one_captures_error():
    def boom(m):
        raise RuntimeError("dns fail")
    out = time_one(boom, "org/x@main")
    assert out["ok"] is False
    assert "dns fail" in out["error"]

def test_run_download_iterates_models():
    out = run_download({"models": ["a", "b"]}, lambda m: (10, 0.01))
    assert [r["model"] for r in out["results"]] == ["a", "b"]

def test_run_download_carries_coldstart_fields():
    out = run_download({"models": ["a"]}, lambda m: (10, 0.01),
                       cold_first_invocation=True, dep_upgrade_ms=1500)
    assert out["cold_first_invocation"] is True
    assert out["dep_upgrade_ms"] == 1500

def test_run_download_defaults_warm():
    out = run_download({"models": ["a"]}, lambda m: (10, 0.01))
    assert out["cold_first_invocation"] is False
    assert out["dep_upgrade_ms"] == 0


def test_time_one_defaults_breakdown_for_two_tuple_downloaders():
    out = time_one(lambda m: (10, 0.01), "org/x@main")
    assert out["breakdown"] == EMPTY_BREAKDOWN


def test_time_one_merges_reported_breakdown():
    out = time_one(lambda m: (10, 0.01, {"hydrate_s": 0.4, "download_s": 0.1}), "org/x@main")
    assert out["breakdown"] == {"download_s": 0.1, "hydrate_s": 0.4, "local_read_s": None}


def test_time_one_error_still_has_breakdown():
    def boom(m):
        raise RuntimeError("x")
    assert time_one(boom, "m")["breakdown"] == EMPTY_BREAKDOWN


def test_schema_phase_maps_legacy_cold_to_populate():
    assert schema_phase("cold") == "populate"
    assert schema_phase("warm") == "warm" and schema_phase("baseline") == "baseline"


def test_make_timing_row_matches_shared_schema():
    job = {"mechanism": "volumecache", "endpoint": "volumecache", "model": "org/x@main",
           "phase": "warm", "replica": 2}
    result = {"worker_id": "w", "cold_first_invocation": True, "dep_upgrade_ms": 900,
              "results": [{"model": "org/x@main", "bytes": 1234, "first_byte_ms": 5,
                           "wall_seconds": 1.2, "ok": True, "error": None,
                           "breakdown": {"download_s": 0.1, "hydrate_s": 1.0, "local_read_s": None}}]}
    row = make_timing_row("volumecache", job, result, wall_seconds=7.5)
    assert row == {
        "mechanism": "volumecache", "phase": "warm", "model": "org/x@main",
        "wall_seconds": 7.5, "bytes": 1234,
        "breakdown": {"download_s": 0.1, "hydrate_s": 1.0, "local_read_s": None},
        "worker_cold": True, "ok": True,
    }


def test_make_timing_row_for_driver_side_failure():
    job = {"mechanism": "shim", "endpoint": "A", "model": "org/x@main", "phase": "cold", "replica": 0}
    row = make_timing_row("shim", job, {"ok": False, "error": "TimeoutError: Job timed out."}, 600.0)
    assert row["phase"] == "populate" and row["ok"] is False
    assert row["bytes"] == 0 and row["worker_cold"] is False
    assert row["breakdown"] == EMPTY_BREAKDOWN


class _FakeVolumeCache:
    instances: list = []

    def __init__(self, dirs, *, namespace=None, volume_path="/runpod-volume", best_effort=True, max_workers=None):
        self.dirs, self.best_effort, self.calls = dirs, best_effort, []
        _FakeVolumeCache.instances.append(self)

    def hydrate(self):
        self.calls.append("hydrate")

    def sync(self, *, background=True):
        self.calls.append(f"sync(background={background})")


def test_volumecache_download_hydrates_downloads_then_syncs_synchronously():
    _FakeVolumeCache.instances.clear()
    order = []

    def fake_download(model):
        order.append("download")
        return (2048, 0.2, {"download_s": 0.2})

    nbytes, first_byte_s, bd = volumecache_download(
        "org/x@main", download_fn=fake_download, cache_factory=_FakeVolumeCache, hf_home="/tmp/hf")
    vc = _FakeVolumeCache.instances[0]
    assert vc.dirs == ["/tmp/hf"] and vc.best_effort is True
    assert vc.calls == ["hydrate", "sync(background=False)"] and order == ["download"]
    assert nbytes == 2048 and first_byte_s == 0.2
    assert set(bd) == {"hydrate_s", "download_s"} and bd["hydrate_s"] >= 0 and bd["download_s"] >= 0


def test_volumecache_download_defaults_hf_home_from_env(monkeypatch):
    _FakeVolumeCache.instances.clear()
    monkeypatch.setenv("HF_HOME", "/root/.cache/huggingface")
    volumecache_download("m", download_fn=lambda m: (1, 0.0), cache_factory=_FakeVolumeCache)
    assert _FakeVolumeCache.instances[0].dirs == ["/root/.cache/huggingface"]
