from pathlib import Path
from runpod_testbed.harvest.scrape import parse_prometheus, write_metrics


def test_parse_plain_labeled_and_histogram():
    text = Path(__file__).parent.joinpath("fixtures/metrics_sample.txt").read_text()
    rows = parse_prometheus(text)
    by = {(r["name"], tuple(sorted(r["labels"].items()))): r["value"] for r in rows}
    assert by[("xet_hits_total", ())] == 12.0
    assert by[("xet_peer_bytes_total", ())] == 524288.0
    assert by[("xet_effective_hit_rate", ())] == 0.75
    assert by[("xet_peer_peer_throughput_bytes_per_ms",
               (("peer", "http://b:8000"),))] == 3.5
    assert by[("xet_xorb_latency_ms_bucket",
               (("le", "5"), ("source", "cdn")))] == 3.0
    assert all(not r["name"].startswith("#") for r in rows)


def test_write_metrics_roundtrip(tmp_path):
    import pyarrow.parquet as pq
    # Representative of a real scrape: a plain sample plus a labeled one (a
    # uniformly-empty labels column can't be inferred by pyarrow, but real
    # /metrics always carries labeled histogram buckets).
    rows = [
        {"name": "xet_hits_total", "labels": {}, "value": 12.0, "pod": "A", "ts": 1.0},
        {"name": "xet_xorb_latency_ms_bucket", "labels": {"le": "5", "source": "cdn"},
         "value": 3.0, "pod": "A", "ts": 1.0},
    ]
    out = str(tmp_path / "m.parquet")
    msg = write_metrics(rows, out)
    assert "wrote 2 rows" in msg
    back = pq.read_table(out).to_pylist()
    assert back[0]["name"] == "xet_hits_total" and back[0]["pod"] == "A"


def test_write_metrics_handles_all_empty_labels(tmp_path):
    # Regression: a cycle whose rows all have empty labels must still write
    # (inferring the schema from data would fail on an empty struct).
    import pyarrow.parquet as pq
    rows = [
        {"name": "xet_hits_total", "labels": {}, "value": 1.0, "pod": "A", "ts": 1.0},
        {"name": "xet_misses_total", "labels": {}, "value": 2.0, "pod": "A", "ts": 1.0},
    ]
    out = str(tmp_path / "m.parquet")
    write_metrics(rows, out)  # must not raise
    assert len(pq.read_table(out).to_pylist()) == 2
