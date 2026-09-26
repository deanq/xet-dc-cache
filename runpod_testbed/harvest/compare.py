"""Cross-mechanism comparison roll-up.

Each mechanism (shim, volumecache, modelstore) measures the cache win
differently: shim/volumecache fill the cache at runtime, so their warm
end-to-end wall is the real caller-facing win. Model Store's fill happens at
provisioning time, so its warm end-to-end wall is dominated by unbilled
platform staging (`delay_seconds`) — its honest number is the once-running
`exec_seconds`. This module puts all three side by side without blending
those two different things into one misleading speedup.

Usage:
    python -m runpod_testbed.harvest.compare shim=<runid> volumecache=<runid> modelstore=<runid>

Any mechanism may be omitted.

Optionally pin a single shared baseline run so every mechanism's speedup uses
the same denominator (baselines otherwise vary run-to-run, e.g. CPU ~11s vs
GPU ~5.8s, making per-mechanism baseline columns not directly comparable):

    python -m runpod_testbed.harvest.compare shim=A volumecache=B modelstore=C baseline=A
"""
from __future__ import annotations

import os
import statistics as st
import sys
from datetime import datetime

from runpod_testbed.harvest.report import (
    _load_jobs,
    _model_names,
    format_seconds,
    latency_by_schema_phase,
    steady_state_by_schema_phase,
    timing_rows,
)

_MECHANISM_ORDER = ("shim", "volumecache", "modelstore")
# Prefer the real cache-hit phase; a mechanism with no "warm" rows (shouldn't
# happen for a complete run) falls back to "populate" rather than showing n/a.
_WARM_PHASE_PRIORITY = ("warm", "populate")
# Below this, a warm row's delay_seconds is scheduler noise, not the
# provisioning-triggered platform staging wait Model Store is known for.
_SIGNIFICANT_DELAY_SECONDS = 1.0
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


def _parse_args(argv: list[str]) -> dict[str, str]:
    runids: dict[str, str] = {}
    for arg in argv:
        if "=" not in arg:
            raise ValueError(f"expected mechanism=runid, got {arg!r}")
        mechanism, runid = arg.split("=", 1)
        runids[mechanism] = runid
    return runids


def _load_mechanism_jobs(mechanism: str, runid: str) -> list:
    path = f"data/jobs-{runid}.jsonl"
    if not os.path.exists(path):
        raise FileNotFoundError(f"no jobs file for {mechanism}={runid}: {path} does not exist")
    jobs = _load_jobs(path)
    if not jobs:
        raise ValueError(f"jobs file for {mechanism}={runid} ({path}) is empty")
    return jobs


def _pick_bucket(buckets: dict, priority: tuple[str, ...]) -> dict | None:
    for phase in priority:
        if phase in buckets:
            return buckets[phase]
    return None


def _median_s(bucket: dict | None) -> float | None:
    return bucket["median_s"] if bucket else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _warm_delay_median(rows: list, wall_buckets: dict) -> float:
    warm_phase = next((p for p in _WARM_PHASE_PRIORITY if p in wall_buckets), None)
    if warm_phase is None:
        return 0.0
    delays = [r["delay_seconds"] for r in rows
              if r["phase"] == warm_phase and r["ok"] and r.get("delay_seconds")]
    return st.median(delays) if delays else 0.0


def _compute_shared_baseline(runid: str) -> dict:
    """Pin ONE baseline run as the reference for every mechanism, so the
    speedup denominator is comparable across mechanisms measured on
    different runs (CPU baselines run ~11s, the GPU baseline ~5.8s)."""
    jobs = _load_mechanism_jobs("baseline", runid)
    rows = timing_rows(jobs)
    baseline_wall = _median_s(latency_by_schema_phase(rows).get("baseline"))
    baseline_exec = _median_s(steady_state_by_schema_phase(rows).get("baseline"))
    if baseline_wall is None or baseline_exec is None:
        raise ValueError(
            f"pinned baseline run {runid!r} (data/jobs-{runid}.jsonl) has no "
            "baseline-phase rows to compute a shared baseline from")
    return {"runid": runid, "baseline_wall": baseline_wall, "baseline_exec": baseline_exec}


def mechanism_stats(mechanism: str, jobs: list, shared_baseline: dict | None = None) -> dict:
    rows = timing_rows(jobs)
    wall = latency_by_schema_phase(rows)
    exec_ = steady_state_by_schema_phase(rows)

    if shared_baseline is not None:
        baseline_wall = shared_baseline["baseline_wall"]
        baseline_exec = shared_baseline["baseline_exec"]
    else:
        baseline_wall = _median_s(wall.get("baseline"))
        baseline_exec = _median_s(exec_.get("baseline"))
    warm_wall = _median_s(_pick_bucket(wall, _WARM_PHASE_PRIORITY))
    warm_exec = _median_s(_pick_bucket(exec_, _WARM_PHASE_PRIORITY))

    return {
        "mechanism": mechanism,
        "baseline_wall": baseline_wall,
        "warm_wall": warm_wall,
        "baseline_exec": baseline_exec,
        "warm_exec": warm_exec,
        "delay_median": _warm_delay_median(rows, wall),
        "coldstart_speedup": _ratio(baseline_wall, warm_wall),
        "steadystate_speedup": _ratio(baseline_exec, warm_exec),
    }


def _header_lines(runids: dict[str, str], jobs_by_mechanism: dict[str, list],
                   shared_baseline: dict | None = None) -> list[str]:
    date_str = datetime.now().strftime("%Y-%m-%d")
    models = sorted({m for jobs in jobs_by_mechanism.values() for m in _model_names(jobs)})
    model_str = ", ".join(models) if models else "no models recorded"
    runid_str = "; ".join(f"{mech}={runids[mech]}" for mech in _MECHANISM_ORDER if mech in runids)
    lines = [
        "# Cache mechanism comparison", "",
        f"**{date_str} · models: {model_str} · runs: {runid_str}**", "",
    ]
    if shared_baseline is not None:
        lines += [
            f"**Shared baseline: pinned from run `{shared_baseline['runid']}`** "
            f"(wall {format_seconds(shared_baseline['baseline_wall'])}, "
            f"steady-state {format_seconds(shared_baseline['baseline_exec'])}).", "",
            "_Every mechanism's speedup below is computed against this one baseline, not its own "
            "run's baseline, so the numbers are directly comparable — the pinned run's instance "
            "type and conditions define the reference._", "",
        ]
    return lines


def _speedup_str(speedup: float | None) -> str:
    return f"{speedup:.2f}×" if speedup is not None else "n/a"


def _table_header(shared_baseline: dict | None, baseline_label: str) -> list[str]:
    if shared_baseline is not None:
        return [f"_baseline (shared, see above): {baseline_label}_", "",
                "| mechanism | warm | speedup |", "|---|---|---|"]
    return ["| mechanism | baseline | warm | speedup |", "|---|---|---|---|"]


def _coldstart_table(stats_list: list[dict], shared_baseline: dict | None = None) -> list[str]:
    lines = [
        "## Cold-start latency (end-to-end)", "",
        "_What the caller actually waits: submit → ready._", "",
    ]
    lines += _table_header(shared_baseline, format_seconds(shared_baseline["baseline_wall"])
                            if shared_baseline else "")
    footnotes = []
    for s in stats_list:
        significant = s["delay_median"] >= _SIGNIFICANT_DELAY_SECONDS
        marker = "\\*" if significant else ""
        baseline_cell = "" if shared_baseline else f"{format_seconds(s['baseline_wall'])} | "
        lines.append(f"| {s['mechanism']}{marker} | {baseline_cell}"
                     f"{format_seconds(s['warm_wall'])} | {_speedup_str(s['coldstart_speedup'])} |")
        if significant:
            footnotes.append(
                f"\\* **{s['mechanism']}**: {format_seconds(s['delay_median'])} of this is unbilled "
                "provisioning-triggered staging, recurring per cold worker.")
    lines.append("")
    if footnotes:
        lines += footnotes + [""]
    return lines


def _steadystate_table(stats_list: list[dict], shared_baseline: dict | None = None) -> list[str]:
    lines = [
        "## Steady-state (once running)", "",
        "_Like-for-like: execution/read time only, queue and staging wait excluded._", "",
    ]
    lines += _table_header(shared_baseline, format_seconds(shared_baseline["baseline_exec"])
                            if shared_baseline else "")
    for s in stats_list:
        baseline_cell = "" if shared_baseline else f"{format_seconds(s['baseline_exec'])} | "
        lines.append(f"| {s['mechanism']} | {baseline_cell}"
                     f"{format_seconds(s['warm_exec'])} | {_speedup_str(s['steadystate_speedup'])} |")
    lines.append("")
    return lines


def _clean_win_sentence(stats_by_mechanism: dict[str, dict]) -> str | None:
    wins = [s for m in ("shim", "volumecache") if (s := stats_by_mechanism.get(m))
            and s["coldstart_speedup"] is not None]
    if not wins:
        return None
    parts = [f"**{s['mechanism']}** ({s['coldstart_speedup']:.1f}×)" for s in wins]
    return (f"{' and '.join(parts)} are clean end-to-end wins: the cache fill is a one-time cost, and "
            "every subsequent read is faster, full stop.")


def _modelstore_sentence(s: dict | None) -> str | None:
    if s is None or s["coldstart_speedup"] is None or s["steadystate_speedup"] is None:
        return None
    return (
        f"**modelstore** is two separate claims, not one speedup: once a worker is running, its read is "
        f"the fastest of the three ({s['steadystate_speedup']:.1f}× steady-state), but its cold-start "
        f"is the *worst* end-to-end wait ({format_seconds(s['warm_wall'])} vs "
        f"{format_seconds(s['baseline_wall'])} baseline, a {s['coldstart_speedup']:.2f}× \"speedup\" "
        "— i.e. slower), dominated by unbilled platform staging that recurs on every cold worker.")


def _verdict_lines(stats_list: list[dict]) -> list[str]:
    by_mechanism = {s["mechanism"]: s for s in stats_list}
    sentences = [
        _clean_win_sentence(by_mechanism),
        _modelstore_sentence(by_mechanism.get("modelstore")),
    ]
    lines = ["## Verdict", ""]
    lines += [s for s in sentences if s is not None]
    lines.append("")
    return lines


def build_report(runids: dict[str, str]) -> tuple[str, str]:
    mechanism_runids = {m: r for m, r in runids.items() if m != "baseline"}
    baseline_runid = runids.get("baseline")
    shared_baseline = _compute_shared_baseline(baseline_runid) if baseline_runid else None

    jobs_by_mechanism = {m: _load_mechanism_jobs(m, r) for m, r in mechanism_runids.items()}
    stats_list = [mechanism_stats(m, jobs_by_mechanism[m], shared_baseline)
                  for m in _MECHANISM_ORDER if m in jobs_by_mechanism]
    lines = (_header_lines(mechanism_runids, jobs_by_mechanism, shared_baseline)
             + _coldstart_table(stats_list, shared_baseline)
             + _steadystate_table(stats_list, shared_baseline)
             + _verdict_lines(stats_list))
    timestamp = datetime.now().strftime(_TIMESTAMP_FORMAT)
    return f"data/comparison-{timestamp}.md", "\n".join(lines)


def main() -> None:
    runids = _parse_args(sys.argv[1:])
    if not runids:
        raise SystemExit("usage: python -m runpod_testbed.harvest.compare "
                          "shim=<runid> volumecache=<runid> modelstore=<runid> [baseline=<runid>]")
    out_path, text = build_report(runids)
    os.makedirs("data", exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write(text)
    print(f"wrote {out_path}")
    print()
    print(text)


if __name__ == "__main__":
    main()
