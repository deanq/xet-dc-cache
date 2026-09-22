from runpod_testbed.harvest.report import latency_by_phase, peering_payoff, headline

JOBS = [
    {"phase": "cold", "result": {"results": [{"ok": True, "wall_seconds": 10.0, "bytes": 100}]}},
    {"phase": "warm", "result": {"results": [{"ok": True, "wall_seconds": 1.0, "bytes": 100}]}},
    {"phase": "warm", "result": {"results": [{"ok": True, "wall_seconds": 2.0, "bytes": 100}]}},
    {"phase": "warm", "result": {"results": [{"ok": False, "wall_seconds": 0.0, "bytes": 0}]}},
]


def test_latency_by_phase_ignores_failures():
    out = latency_by_phase(JOBS)
    assert out["cold"]["n"] == 1 and out["cold"]["median_s"] == 10.0
    assert out["warm"]["n"] == 2 and out["warm"]["median_s"] == 1.5


def test_headline_computes_cold_warm_speedup():
    h = headline(JOBS)
    assert h["n_ok"] == 3                       # 1 cold + 2 warm ok (failure excluded)
    assert h["cold_median_s"] == 10.0
    assert h["warm_median_s"] == 1.5
    assert h["speedup"] == 10.0 / 1.5           # ~6.67x
    assert h["total_bytes"] == 300


def test_headline_speedup_none_when_phase_missing():
    only_cold = [{"phase": "cold",
                  "result": {"results": [{"ok": True, "wall_seconds": 5.0, "bytes": 10}]}}]
    assert headline(only_cold)["speedup"] is None


def test_peering_payoff_uses_final_sample():
    rows = [
        {"pod": "A", "ts": 1, "name": "xet_peer_bytes_total", "labels": {}, "value": 5},
        {"pod": "A", "ts": 2, "name": "xet_peer_bytes_total", "labels": {}, "value": 30},
        {"pod": "A", "ts": 2, "name": "xet_wan_bytes_total", "labels": {}, "value": 70},
    ]
    out = peering_payoff(rows)
    assert out["peer_bytes"] == 30 and out["wan_bytes"] == 70
    assert abs(out["peer_fraction"] - 0.3) < 1e-9
