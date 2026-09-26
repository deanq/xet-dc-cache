import json

import pytest

from runpod_testbed.harvest.compare import (
    _compute_shared_baseline,
    _parse_args,
    _load_mechanism_jobs,
    build_report,
    mechanism_stats,
)


def _job(mechanism, phase, wall, nbytes=100, ok=True,
         delay_seconds=None, exec_seconds=None, **bd):
    return {"phase": phase, "result": {},
            "timing": {"mechanism": mechanism, "phase": phase, "model": "org/x@main",
                       "wall_seconds": wall, "bytes": nbytes,
                       "breakdown": {"download_s": None, "hydrate_s": None, "local_read_s": None, **bd},
                       "worker_cold": False, "ok": ok,
                       "delay_seconds": delay_seconds, "exec_seconds": exec_seconds}}


def _write_jobs(tmp_path, runid, jobs):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    path = data_dir / f"jobs-{runid}.jsonl"
    with open(path, "w") as fh:
        for j in jobs:
            fh.write(json.dumps(j) + "\n")
    return path


SHIM_JOBS = [
    _job("baseline", "baseline", 12.0, download_s=11.5),
    _job("baseline", "baseline", 14.0, download_s=13.5),
    _job("shim", "populate", 28.0, download_s=27.0),
    _job("shim", "warm", 1.5, download_s=1.4),
    _job("shim", "warm", 1.6, download_s=1.5),
]

VOLUMECACHE_JOBS = [
    _job("baseline", "baseline", 13.0, download_s=12.5),
    _job("volumecache", "populate", 30.0, download_s=29.0),
    _job("volumecache", "warm", 5.0, hydrate_s=4.8),
    _job("volumecache", "warm", 5.2, hydrate_s=5.0),
]

MODELSTORE_JOBS = [
    _job("baseline", "baseline", 12.0, download_s=11.5, exec_seconds=11.6),
    _job("baseline", "baseline", 14.0, download_s=13.5, exec_seconds=13.6),
    _job("modelstore", "warm", 119.0, local_read_s=0.22, delay_seconds=118.5, exec_seconds=0.22),
    _job("modelstore", "warm", 120.0, local_read_s=0.24, delay_seconds=119.5, exec_seconds=0.24),
    _job("modelstore", "warm", 0.0, ok=False, nbytes=0),
]


def test_parse_args_accepts_mechanism_equals_runid():
    assert _parse_args(["shim=r1", "volumecache=r2"]) == {"shim": "r1", "volumecache": "r2"}


def test_parse_args_rejects_malformed_arg():
    with pytest.raises(ValueError):
        _parse_args(["shim-r1"])


def test_missing_jobs_file_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="no jobs file"):
        _load_mechanism_jobs("shim", "doesnotexist")


def test_empty_jobs_file_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "jobs-empty.jsonl").write_text("")
    with pytest.raises(ValueError, match="is empty"):
        _load_mechanism_jobs("shim", "empty")


def test_shim_coldstart_uses_end_to_end_wall():
    s = mechanism_stats("shim", SHIM_JOBS)
    assert s["baseline_wall"] == 13.0
    assert s["warm_wall"] == 1.55
    assert s["coldstart_speedup"] == pytest.approx(13.0 / 1.55)


def test_modelstore_steadystate_uses_exec_not_delay_inflated_wall():
    s = mechanism_stats("modelstore", MODELSTORE_JOBS)
    # warm end-to-end wall is dominated by delay_seconds (~119s)
    assert s["warm_wall"] == pytest.approx(119.5)
    # steady-state must use exec_seconds, not the delay-inflated wall
    assert s["warm_exec"] == pytest.approx(0.23)
    assert s["delay_median"] == pytest.approx(119.0)


def test_modelstore_coldstart_speedup_below_one_but_steadystate_above_one():
    s = mechanism_stats("modelstore", MODELSTORE_JOBS)
    assert s["coldstart_speedup"] < 1
    assert s["steadystate_speedup"] > 1


def test_modelstore_ignores_failed_warm_row():
    s = mechanism_stats("modelstore", MODELSTORE_JOBS)
    # median over the 2 ok warm rows only (118.5, 119.5), not the failed 0.0 row
    assert s["delay_median"] == pytest.approx(119.0)


def test_build_report_renders_footnote_only_for_significant_delay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "shim-run", SHIM_JOBS)
    _write_jobs(tmp_path, "ms-run", MODELSTORE_JOBS)
    _, text = build_report({"shim": "shim-run", "modelstore": "ms-run"})
    assert "\\* **modelstore**:" in text
    assert "\\* **shim**:" not in text


def test_build_report_includes_both_tables_and_verdict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "shim-run", SHIM_JOBS)
    _write_jobs(tmp_path, "vc-run", VOLUMECACHE_JOBS)
    _write_jobs(tmp_path, "ms-run", MODELSTORE_JOBS)
    _, text = build_report({"shim": "shim-run", "volumecache": "vc-run", "modelstore": "ms-run"})
    assert "## Cold-start latency (end-to-end)" in text
    assert "## Steady-state (once running)" in text
    assert "## Verdict" in text
    assert "two separate claims" in text
    assert "clean end-to-end wins" in text


def test_build_report_handles_missing_mechanism(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "shim-run", SHIM_JOBS)
    _, text = build_report({"shim": "shim-run"})
    assert "| shim |" in text
    assert "modelstore" not in text


# --- shared baseline (baseline=<runid>) ---

# A pinned baseline whose wall/exec medians differ sharply from both
# SHIM_JOBS' own baseline (13.0s wall / 12.5s exec) and VOLUMECACHE_JOBS'
# own baseline (13.0s wall / 12.5s exec) — lets tests distinguish "shared"
# from "own-run" baseline values.
PINNED_BASELINE_JOBS = [
    _job("baseline", "baseline", 100.0, download_s=95.0),
    _job("baseline", "baseline", 110.0, download_s=105.0),
]

NO_BASELINE_ROWS_JOBS = [
    _job("shim", "warm", 1.5, download_s=1.4),
]


def test_compute_shared_baseline_reads_pinned_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "pinned-run", PINNED_BASELINE_JOBS)
    shared = _compute_shared_baseline("pinned-run")
    assert shared["runid"] == "pinned-run"
    assert shared["baseline_wall"] == pytest.approx(105.0)
    assert shared["baseline_exec"] == pytest.approx(100.0)


def test_compute_shared_baseline_raises_on_missing_baseline_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "no-baseline-run", NO_BASELINE_ROWS_JOBS)
    with pytest.raises(ValueError, match="no baseline-phase rows"):
        _compute_shared_baseline("no-baseline-run")


def test_compute_shared_baseline_missing_jobs_file_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="no jobs file"):
        _compute_shared_baseline("doesnotexist")


def test_mechanism_stats_shared_baseline_overrides_own_run_baseline():
    shared = {"runid": "pinned-run", "baseline_wall": 105.0, "baseline_exec": 100.0}
    s = mechanism_stats("shim", SHIM_JOBS, shared_baseline=shared)
    assert s["baseline_wall"] == 105.0
    assert s["baseline_exec"] == 100.0
    # differs from shim's own-run baseline (13.0s wall / 12.5s exec)
    own = mechanism_stats("shim", SHIM_JOBS)
    assert own["baseline_wall"] != s["baseline_wall"]
    assert own["baseline_exec"] != s["baseline_exec"]


def test_build_report_with_pinned_baseline_uses_same_denominator_for_all_mechanisms(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "shim-run", SHIM_JOBS)
    _write_jobs(tmp_path, "vc-run", VOLUMECACHE_JOBS)
    _write_jobs(tmp_path, "pinned-run", PINNED_BASELINE_JOBS)
    _, text = build_report({"shim": "shim-run", "volumecache": "vc-run", "baseline": "pinned-run"})
    assert "pinned from run `pinned-run`" in text
    assert "directly comparable" in text
    # shared baseline value appears once (in the header note); the per-mechanism
    # baseline column is gone from both tables.
    assert "| baseline |" not in text
    assert text.count("105.0s") >= 1


def test_build_report_pinned_baseline_speedups_share_denominator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "shim-run", SHIM_JOBS)
    _write_jobs(tmp_path, "vc-run", VOLUMECACHE_JOBS)
    _write_jobs(tmp_path, "pinned-run", PINNED_BASELINE_JOBS)
    shared = _compute_shared_baseline("pinned-run")
    shim_jobs = _load_mechanism_jobs("shim", "shim-run")
    vc_jobs = _load_mechanism_jobs("volumecache", "vc-run")
    shim_stats = mechanism_stats("shim", shim_jobs, shared)
    vc_stats = mechanism_stats("volumecache", vc_jobs, shared)
    assert shim_stats["baseline_wall"] == vc_stats["baseline_wall"] == shared["baseline_wall"]
    assert shim_stats["baseline_exec"] == vc_stats["baseline_exec"] == shared["baseline_exec"]


def test_build_report_without_baseline_arg_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, "shim-run", SHIM_JOBS)
    _write_jobs(tmp_path, "vc-run", VOLUMECACHE_JOBS)
    _, text = build_report({"shim": "shim-run", "volumecache": "vc-run"})
    assert "| mechanism | baseline | warm | speedup |" in text
    assert "pinned from run" not in text
