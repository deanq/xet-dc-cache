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


# --- formatting helpers (human-readable units; no raw byte/fraction dumps) ---

_SUBSECOND_THRESHOLD_S = 1.0
_BYTES_PER_MB = 1_000_000
_BYTES_PER_GB = 1_000_000_000
_POD_ID_SHORT_CHARS = 6
_MS_PER_SECOND = 1000
_RUNID_FORMAT = "%Y%m%d-%H%M%S"


def format_seconds(seconds) -> str:  # None-safe; sub-second gets more precision
    if seconds is None:
        return "n/a"
    if abs(seconds) < _SUBSECOND_THRESHOLD_S:
        return f"{seconds:.2f}s"
    return f"{seconds:.1f}s"


_BYTES_PER_KB = 1_000


def format_bytes(n) -> str:  # None-safe; decimal KB/MB/GB, always labeled
    if n is None:
        return "n/a"
    if abs(n) < _BYTES_PER_KB:
        return f"{n:.0f} B"
    if abs(n) < _BYTES_PER_MB:
        return f"{n / _BYTES_PER_KB:.0f} KB"
    if abs(n) < _BYTES_PER_GB:
        return f"{n / _BYTES_PER_MB:.1f} MB"
    return f"{n / _BYTES_PER_GB:.1f} GB"


def format_percent(fraction) -> str:  # None-safe; 0.9 -> "90%"
    if fraction is None:
        return "n/a"
    return f"{fraction * 100:.0f}%"


def pod_label(pod_id: str, state) -> str:
    """Friendly label for a metrics 'pod' value: look it up in
    ProvisionState.pods (label -> raw pod id, inverted); fall back to a short
    id if the run has no state or the id isn't known."""
    if state is not None:
        for label, pid in state.pods.items():
            if pid == pod_id:
                return f"pod {label}"
    return f"pod {pod_id[:_POD_ID_SHORT_CHARS]}"


def _run_datetime(runid: str) -> str:
    from datetime import datetime
    try:
        return datetime.strptime(runid, _RUNID_FORMAT).strftime("%Y-%m-%d %H:%M")
    except ValueError:  # unexpected runid shape -- show it verbatim rather than crash
        return runid


def _model_names(jobs: list) -> list[str]:
    names = (r["model"].split("@", 1)[0].split("/", 1)[-1] for r in timing_rows(jobs))
    return list(dict.fromkeys(names))


def _fleet_description(state) -> str:
    from runpod_testbed.mechanisms.base import BASELINE_LABEL
    if state is None:
        return "no pods/endpoints recorded"
    if state.pods:
        n = len(state.pods)
        return f"{n} cache pod{'s' if n != 1 else ''}"
    owned = [k for k in state.endpoints if k != BASELINE_LABEL]
    if owned:
        return f"{len(owned)} endpoint{'s' if len(owned) != 1 else ''}"
    return "no pods/endpoints recorded"


def _header_lines(runid: str, mechanism: str, jobs: list, state) -> list[str]:
    models = _model_names(jobs)
    model_str = ", ".join(models) if models else "no models recorded"
    return [f"# Cache testbed report — {mechanism}", "",
            f"**{_run_datetime(runid)} · models: {model_str} · {_fleet_description(state)}**", ""]


def _one_line_lines(jobs: list, metrics_rows: list) -> list[str]:
    """Plain-English money paragraph: cold time, warm time, speedup, and (if
    this run has peering data) what fraction of miss bytes came from a peer."""
    h = headline_vs_baseline(timing_rows(jobs))
    lines = ["## In one line", ""]
    if h["speedup"] is None:
        lines += ["_Not enough data yet to compute a baseline→warm speedup "
                  "(need at least one baseline run and one warm run)._", ""]
        return lines
    sentence = (f"A cold model download took **{format_seconds(h['baseline_median_s'])}**; "
                f"once the cache was warm, repeat pulls took **{format_seconds(h['warm_median_s'])}** "
                f"— **{h['speedup']:.1f}× faster**.")
    payoff = peering_payoff(metrics_rows) if metrics_rows else None
    if payoff and (payoff["peer_bytes"] or payoff["wan_bytes"]):
        sentence += (f" **{format_percent(payoff['peer_fraction'])} of cache-miss bytes came from a "
                     f"neighboring cache pod** over the datacenter backbone instead of the public internet.")
    lines += [sentence, ""]
    return lines


_BASELINE_PHASE_DESC = "naive pull straight from HuggingFace (no cache)"
_PHASE_DESCRIPTIONS = {
    "shim": {"populate": "first pull, fills the cache",
             "warm": "pull through a cache that already has it"},
    "volumecache": {"populate": "first pull, fills the volume",
                     "warm": "later pull, restored from the volume"},
    "modelstore": {"warm": "pull from the platform's pre-staged model cache"},
}


def _phase_description(mechanism: str, phase: str) -> str:
    if phase == "baseline":
        return _BASELINE_PHASE_DESC
    return _PHASE_DESCRIPTIONS.get(mechanism, {}).get(phase, phase)


def _cold_vs_warm_lines(mechanism: str, jobs: list) -> list[str]:
    lat = latency_by_schema_phase(timing_rows(jobs))
    if not lat:
        return ["## Cold vs warm", "", "_No timing data captured for this run._", ""]
    lines = ["## Cold vs warm", "",
             "| stage | what it measures | runs | median | data |", "|---|---|---|---|---|"]
    for phase in SCHEMA_PHASES:
        if phase not in lat:
            continue
        d = lat[phase]
        lines.append(f"| {phase} | {_phase_description(mechanism, phase)} | {d['n']} | "
                     f"{format_seconds(d['median_s'])} | {format_bytes(d['total_bytes'])} |")
    h = headline_vs_baseline(timing_rows(jobs))
    lines.append("")
    if h["speedup"] is not None:
        lines += [f"_Warm reads are **{h['speedup']:.1f}×** faster than going to HuggingFace._", ""]
    return lines


def _coldstart_lines(jobs: list) -> list[str]:
    cs = coldstart(jobs)
    if cs["n_cold"] == 0:
        return ["## Cold start", "", "_No cold-start (first-invocation) jobs recorded this run._", ""]
    lines = ["## Cold start", "",
             f"**{cs['n_cold']}** job(s) hit a genuine cold start (first invocation on a fresh worker) "
             "this run.",
             f"Cold jobs averaged **{format_seconds(cs['cold_mean_s'])}**; "
             f"warm jobs averaged **{format_seconds(cs['warm_mean_s'])}**."]
    if cs["dep_upgrade_ms"]:
        lines.append(f"Installing dependencies on that cold worker added "
                     f"**{cs['dep_upgrade_ms'] / _MS_PER_SECOND:.1f}s** on average.")
    lines.append("")
    return lines


def render_timing_core(runid: str, mechanism: str, jobs: list, metrics_rows: list, state) -> list[str]:
    return (_header_lines(runid, mechanism, jobs, state)
            + _one_line_lines(jobs, metrics_rows)
            + _cold_vs_warm_lines(mechanism, jobs)
            + _coldstart_lines(jobs))


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
    state = None
    if os.path.exists(state_path(runid)):
        state = ProvisionState.load(state_path(runid))
        mechanism = state.mechanism
    mech = get_mechanism(mechanism)

    jobs = _load_jobs(f"data/jobs-{runid}.jsonl")
    metric_rows = _load_metrics(f"data/pod-metrics-{runid}.parquet", runid) if mech.has_metrics() else []
    os.makedirs("data", exist_ok=True)
    _plots(mechanism, runid, jobs, metric_rows)

    lines = (render_timing_core(runid, mechanism, jobs, metric_rows, state)
             + mech.report_sections(jobs, metric_rows, state))
    out_path = report_path(mechanism, runid)
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
