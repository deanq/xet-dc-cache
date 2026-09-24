from runpod_testbed.harvest.report import latency_by_phase, peering_payoff, headline, coldstart

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


COLD_JOBS = [
    {"result": {"cold_first_invocation": True, "dep_upgrade_ms": 15000,
                "results": [{"ok": True, "wall_seconds": 40.0}]}},
    {"result": {"cold_first_invocation": False, "dep_upgrade_ms": 0,
                "results": [{"ok": True, "wall_seconds": 5.0}]}},
    {"result": {"cold_first_invocation": False, "dep_upgrade_ms": 0,
                "results": [{"ok": True, "wall_seconds": 3.0}]}},
]

def test_coldstart_separates_cold_and_warm():
    c = coldstart(COLD_JOBS)
    assert c["n_cold"] == 1
    assert c["cold_mean_s"] == 40.0
    assert c["warm_mean_s"] == 4.0
    assert c["dep_upgrade_ms"] == 15000
