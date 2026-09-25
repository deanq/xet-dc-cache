from __future__ import annotations
import statistics as st


def _ok_results(jobs):
    for j in jobs:
        for r in j.get("result", {}).get("results", []):
            if r.get("ok"):
                yield j["phase"], r


def latency_by_phase(jobs: list) -> dict:
    buckets: dict = {}
    for phase, r in _ok_results(jobs):
        buckets.setdefault(phase, []).append(r)
    out = {}
    for phase, rs in buckets.items():
        secs = sorted(x["wall_seconds"] for x in rs)
        out[phase] = {"n": len(secs), "median_s": st.median(secs),
                      "p95_s": secs[max(0, round(0.95 * len(secs)) - 1)],
                      "total_bytes": sum(x["bytes"] for x in rs)}
    return out


def headline(jobs: list) -> dict:
    """The demo money-stat: cold→warm speedup, job count, bytes served.

    speedup is the ratio of cold to warm median wall time (how many times
    faster a warm cache hit is); None if either phase is absent or warm is 0.
    """
    lat = latency_by_phase(jobs)
    cold, warm = lat.get("cold"), lat.get("warm")
    n_ok = sum(1 for _ in _ok_results(jobs))
    total_bytes = sum(d["total_bytes"] for d in lat.values())
    speedup = None
    if cold and warm and warm["median_s"] > 0:
        speedup = cold["median_s"] / warm["median_s"]
    return {"n_ok": n_ok, "total_bytes": total_bytes,
            "cold_median_s": cold["median_s"] if cold else None,
            "warm_median_s": warm["median_s"] if warm else None,
            "speedup": speedup}


def coldstart(jobs: list) -> dict:
    """Cold (first-invocation, dep-upgrade-paying) vs warm job wall times.

    `drive/run.py` already persists the whole worker `result` dict via
    `record()`, so `cold_first_invocation`/`dep_upgrade_ms` flow into the jobs
    JSONL with no drive change needed.
    """
    cold, warm, dep_ms = [], [], []
    for j in jobs:
        res = j.get("result", {})
        first = bool(res.get("cold_first_invocation"))
        if first:
            dep_ms.append(int(res.get("dep_upgrade_ms", 0)))
        walls = [r["wall_seconds"] for r in res.get("results", []) if r.get("ok")]
        (cold if first else warm).extend(walls)
    return {"n_cold": len(cold),
            "cold_mean_s": (sum(cold) / len(cold)) if cold else None,
            "warm_mean_s": (sum(warm) / len(warm)) if warm else None,
            "dep_upgrade_ms": (sum(dep_ms) / len(dep_ms)) if dep_ms else None}


def _final_by_pod(rows, name):
    latest = {}
    for r in rows:
        if r["name"] != name or r["labels"]:
            continue
        cur = latest.get(r["pod"])
        if cur is None or r["ts"] > cur[0]:
            latest[r["pod"]] = (r["ts"], r["value"])
    return sum(v for _, v in latest.values())


def peering_payoff(metric_rows: list) -> dict:
    peer = _final_by_pod(metric_rows, "xet_peer_bytes_total")
    wan = _final_by_pod(metric_rows, "xet_wan_bytes_total")
    # Metric names confirmed against shim-go/prometheus.go (lines 33/35) -- the
    # graceful 0.0 fallback below is for "no hedges fired yet", not an unverified
    # metric-name guess.
    fired = _final_by_pod(metric_rows, "xet_peer_hedge_fired_total")
    won = _final_by_pod(metric_rows, "xet_peer_hedge_peer_won_total")
    total = peer + wan
    return {"peer_bytes": peer, "wan_bytes": wan,
            "peer_fraction": (peer / total) if total else 0.0,
            "hedge_win_ratio": (won / fired) if fired else 0.0}


SCHEMA_KEYS = frozenset({"mechanism", "phase", "model", "wall_seconds", "bytes",
                         "breakdown", "worker_cold", "ok"})
SCHEMA_PHASES = ("baseline", "populate", "warm")


def report_path(mechanism: str, runid: str) -> str:
    return f"data/report-{mechanism}-{runid}.md"


def plot_path(mechanism: str, runid: str, kind: str) -> str:
    return f"data/report-{mechanism}-{runid}-{kind}.png"


def timing_rows(jobs: list) -> list:
    return [j["timing"] for j in jobs if "timing" in j]


def _stats(secs: list, total_bytes: int) -> dict:
    secs = sorted(secs)
    return {"n": len(secs), "median_s": st.median(secs),
            "p95_s": secs[max(0, round(0.95 * len(secs)) - 1)], "total_bytes": total_bytes}


def latency_by_schema_phase(rows: list) -> dict:
    buckets: dict = {}
    for r in rows:
        if r["ok"]:
            buckets.setdefault(r["phase"], []).append(r)
    return {p: _stats([r["wall_seconds"] for r in rs], sum(r["bytes"] for r in rs))
            for p, rs in buckets.items()}


def headline_vs_baseline(rows: list) -> dict:
    """The comparison money-stat: baseline_wall / mechanism_warm_wall (medians)."""
    lat = latency_by_schema_phase(rows)
    base, warm = lat.get("baseline"), lat.get("warm")
    speedup = None
    if base and warm and warm["median_s"] > 0:
        speedup = base["median_s"] / warm["median_s"]
    return {"n_ok": sum(1 for r in rows if r["ok"]),
            "total_bytes": sum(d["total_bytes"] for d in lat.values()),
            "baseline_median_s": base["median_s"] if base else None,
            "warm_median_s": warm["median_s"] if warm else None,
            "speedup": speedup}


def _fmt(v, spec: str) -> str:  # None-safe number formatting
    return format(v, spec) if v is not None else "n/a"


def _headline_lines(jobs: list) -> list[str]:
    h = headline_vs_baseline(timing_rows(jobs))
    lines = ["## Headline", ""]
    if h["speedup"] is not None:
        lines.append(f"- **Baseline→warm speedup: {h['speedup']:.1f}× faster** "
                     f"(median wall {h['baseline_median_s']:.2f}s → {h['warm_median_s']:.3f}s)")
    legacy = headline(jobs)   # shim-only cold→warm (kept for continuity with older reports)
    if legacy["speedup"] is not None:
        lines.append(f"- Cold→warm speedup (legacy, handler wall): {legacy['speedup']:.1f}×")
    lines += [f"- Jobs completed OK: {h['n_ok']}", f"- Total bytes served: {h['total_bytes']}", ""]
    return lines


def _coldstart_lines(jobs: list) -> list[str]:
    cs = coldstart(jobs)
    return ["## Cold-start vs steady-state", "",
            f"- Cold (first-invocation) jobs: {cs['n_cold']}",
            f"- Cold mean wall_seconds: {_fmt(cs['cold_mean_s'], '.3f')}",
            f"- Warm mean wall_seconds: {_fmt(cs['warm_mean_s'], '.3f')}",
            f"- Mean dep-upgrade ms: {_fmt(cs['dep_upgrade_ms'], '.0f')}", ""]


def _latency_lines(jobs: list) -> list[str]:
    lat = latency_by_schema_phase(timing_rows(jobs))
    lines = ["## Latency by phase", "", "| phase | n | median_s | p95_s | total_bytes |", "|---|---|---|---|---|"]
    for phase in SCHEMA_PHASES:
        if phase in lat:
            d = lat[phase]
            lines.append(f"| {phase} | {d['n']} | {d['median_s']:.3f} | {d['p95_s']:.3f} | {d['total_bytes']} |")
    return lines + [""]


def render_timing_core(runid: str, mechanism: str, jobs: list) -> list[str]:
    return ([f"# Runpod cache testbed report — {mechanism} {runid}", ""]
            + _headline_lines(jobs) + _coldstart_lines(jobs) + _latency_lines(jobs))


def _load_jobs(path: str) -> list:
    import json
    jobs = []
    with open(path) as fh:
        for line in fh:
            if line.strip():
                jobs.append(json.loads(line))
    return jobs


def _load_metrics(path: str, runid: str) -> list:
    import os
    import pyarrow.parquet as pq
    if os.path.exists(path):
        return pq.read_table(path).to_pylist()
    print(f"warning: {path} not found — omitting pod hit-rate and peering sections "
          f"(run `make scrape RUNID={runid}` during a run to capture them)")
    return []


def _plots(mechanism: str, runid: str, jobs: list, metric_rows: list) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from runpod_testbed.mechanisms.shim import per_pod_stats
    lat = latency_by_schema_phase(timing_rows(jobs))
    phases = [p for p in SCHEMA_PHASES if p in lat]
    if phases:
        fig, ax = plt.subplots()
        ax.bar(phases, [lat[p]["median_s"] for p in phases])
        ax.set_ylabel("median wall_seconds (driver-observed)")
        ax.set_title(f"Latency by phase — {mechanism} ({runid})")
        fig.savefig(plot_path(mechanism, runid, "latency"))
        plt.close(fig)
    per_pod = per_pod_stats(metric_rows)
    if per_pod:
        fig, ax = plt.subplots()
        ax.bar(list(per_pod), [d["effective_hit_rate"] for d in per_pod.values()])
        ax.set_ylabel("effective_hit_rate")
        ax.set_title(f"Per-pod hit rate ({runid})")
        fig.savefig(plot_path(mechanism, runid, "hitrate"))
        plt.close(fig)


def main() -> None:  # integration: load jobs+metrics -> report.md + plots
    import os
    import sys
    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState, state_path

    runid = sys.argv[1]
    mechanism = "shim"   # pre-abstraction runs have no state.mechanism
    if os.path.exists(state_path(runid)):
        mechanism = ProvisionState.load(state_path(runid)).mechanism
    mech = get_mechanism(mechanism)

    jobs = _load_jobs(f"data/jobs-{runid}.jsonl")
    metric_rows = _load_metrics(f"data/pod-metrics-{runid}.parquet", runid) if mech.has_metrics() else []
    os.makedirs("data", exist_ok=True)
    _plots(mechanism, runid, jobs, metric_rows)

    lines = render_timing_core(runid, mechanism, jobs) + mech.report_sections(jobs, metric_rows)
    out_path = report_path(mechanism, runid)
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
