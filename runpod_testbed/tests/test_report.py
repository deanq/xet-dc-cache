from runpod_testbed.harvest.report import latency_by_phase, peering_payoff, headline, coldstart
from runpod_testbed.harvest.report import (
    SCHEMA_KEYS, format_bytes, format_percent, format_seconds, headline_vs_baseline,
    latency_by_schema_phase, pod_label, render_timing_core, report_path, timing_rows,
)
from runpod_testbed.mechanisms.base import ProvisionState

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


def _t(mechanism, phase, wall, nbytes=100, ok=True, cold=False, **bd):
    return {"phase": phase, "result": {},
            "timing": {"mechanism": mechanism, "phase": phase, "model": "org/x@main",
                       "wall_seconds": wall, "bytes": nbytes,
                       "breakdown": {"download_s": None, "hydrate_s": None, "local_read_s": None, **bd},
                       "worker_cold": cold, "ok": ok}}

# Mixed-mechanism fixture: locks the schema shared by shim, volumecache, modelstore + baseline.
MIXED = [
    _t("baseline", "baseline", 30.0, download_s=29.0, cold=True),
    _t("baseline", "baseline", 32.0, download_s=31.0),
    _t("shim", "populate", 28.0, download_s=27.0, cold=True),
    _t("shim", "warm", 4.0, download_s=3.5),
    _t("volumecache", "populate", 31.0, download_s=30.0, hydrate_s=0.1),
    _t("volumecache", "warm", 2.0, hydrate_s=1.5, download_s=0.1),
    _t("modelstore", "warm", 6.0, local_read_s=0.2, cold=True),
    _t("modelstore", "warm", 0.0, ok=False, nbytes=0),
    {"phase": "cold", "result": {"results": [{"ok": True, "wall_seconds": 9.0, "bytes": 1}]}},  # legacy row, no timing
]


def test_every_timing_row_has_exactly_the_schema_keys():
    rows = timing_rows(MIXED)
    assert len(rows) == 8
    for r in rows:
        assert set(r) == SCHEMA_KEYS
        assert set(r["breakdown"]) == {"download_s", "hydrate_s", "local_read_s"}
        assert r["mechanism"] in ("shim", "volumecache", "modelstore", "baseline")
        assert r["phase"] in ("baseline", "populate", "warm")


def test_latency_by_schema_phase_ignores_failures_and_buckets_all_three_phases():
    lat = latency_by_schema_phase(timing_rows(MIXED))
    assert set(lat) == {"baseline", "populate", "warm"}
    assert lat["baseline"]["median_s"] == 31.0 and lat["baseline"]["n"] == 2
    assert lat["warm"]["n"] == 3                       # the failed modelstore row excluded
    assert lat["warm"]["total_bytes"] == 300


def test_headline_speedup_is_baseline_over_warm():
    h = headline_vs_baseline(timing_rows(MIXED))
    assert h["baseline_median_s"] == 31.0 and h["warm_median_s"] == 4.0
    assert h["speedup"] == 31.0 / 4.0
    assert h["n_ok"] == 7


def test_headline_speedup_none_without_baseline():
    assert headline_vs_baseline(timing_rows([_t("shim", "warm", 1.0)]))["speedup"] is None


def test_render_timing_core_mentions_mechanism_and_phases():
    text = "\n".join(render_timing_core("r1", "volumecache", MIXED, [], None))
    assert text.startswith("# Cache testbed report — volumecache")
    assert "7.8× faster" in text
    assert "| baseline |" in text and "| populate |" in text and "| warm |" in text
    assert "## Cold start" in text


def test_report_path_is_per_mechanism():
    assert report_path("modelstore", "r1") == "data/report-modelstore-r1.md"


PEERING_ROWS = [
    {"pod": "A", "ts": 1, "name": "xet_peer_bytes_total", "labels": {}, "value": 90},
    {"pod": "A", "ts": 1, "name": "xet_wan_bytes_total", "labels": {}, "value": 10},
]


def test_one_line_summary_includes_speedup_and_peer_percent_when_peering_data_present():
    text = "\n".join(render_timing_core("r1", "shim", MIXED, PEERING_ROWS, None))
    assert "## In one line" in text
    assert "7.8× faster" in text
    assert "90% of cache-miss bytes came from a neighboring cache pod" in text


def test_one_line_summary_omits_peer_sentence_without_peering_data():
    text = "\n".join(render_timing_core("r1", "volumecache", MIXED, [], None))
    assert "neighboring cache pod" not in text


def test_format_bytes_renders_kb_mb_gb_human_units():
    assert format_bytes(8_801_316_242) == "8.8 GB"
    assert format_bytes(4096) == "4 KB"
    assert format_bytes(None) == "n/a"


def test_format_percent_renders_whole_percent():
    assert format_percent(0.9) == "90%"
    assert format_percent(0.0) == "0%"
    assert format_percent(None) == "n/a"


def test_format_seconds_uses_more_precision_below_one_second():
    assert format_seconds(1.510) == "1.5s"
    assert format_seconds(0.163) == "0.16s"


def test_pod_label_maps_known_id_and_falls_back_to_short_id():
    st = ProvisionState(mechanism="shim", runid="r1", pods={"A": "xb3exuk3uxcqrn"})
    assert pod_label("xb3exuk3uxcqrn", st) == "pod A"
    assert pod_label("unknownid", None) == "pod unknow"
