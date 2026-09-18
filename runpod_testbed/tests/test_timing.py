from runpod_testbed.worker.timing import time_one, run_download

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
