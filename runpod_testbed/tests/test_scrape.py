from pathlib import Path
from runpod_testbed.harvest.scrape import parse_prometheus


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
