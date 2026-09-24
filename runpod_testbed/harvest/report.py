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


def main() -> None:  # integration: load jobs+metrics -> report.md + plots
    import json
    import os
    import sys
    import pyarrow.parquet as pq
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    runid = sys.argv[1]
    jobs_path = f"data/jobs-{runid}.jsonl"
    metrics_path = f"data/pod-metrics-{runid}.parquet"

    jobs = []
    with open(jobs_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                jobs.append(json.loads(line))

    # Pod metrics are optional: they come from `make scrape` running during a
    # run. Without them we still emit the job-latency report (the core signal)
    # and simply omit the hit-rate / peering sections rather than crashing.
    if os.path.exists(metrics_path):
        metric_rows = pq.read_table(metrics_path).to_pylist()
    else:
        metric_rows = []
        print(f"warning: {metrics_path} not found — omitting pod hit-rate and "
              f"peering sections (run `make scrape RUNID={runid}` during a run "
              f"to capture them)")

    latency = latency_by_phase(jobs)
    payoff = peering_payoff(metric_rows)

    pods = sorted({r["pod"] for r in metric_rows})
    per_pod = {}
    for pod in pods:
        pod_rows = [r for r in metric_rows if r["pod"] == pod]
        per_pod[pod] = {
            "effective_hit_rate": _final_by_pod(pod_rows, "xet_effective_hit_rate"),
            "wan_bytes_saved": _final_by_pod(pod_rows, "xet_wan_bytes_saved"),
        }

    os.makedirs("data", exist_ok=True)

    # Latency-by-phase bar plot.
    phases = sorted(latency.keys())
    if phases:
        fig, ax = plt.subplots()
        ax.bar(phases, [latency[p]["median_s"] for p in phases])
        ax.set_ylabel("median wall_seconds")
        ax.set_title(f"Latency by phase ({runid})")
        fig.savefig(f"data/report-{runid}-latency.png")
        plt.close(fig)

    # Per-pod effective hit rate plot.
    if pods:
        fig, ax = plt.subplots()
        ax.bar(pods, [per_pod[p]["effective_hit_rate"] for p in pods])
        ax.set_ylabel("effective_hit_rate")
        ax.set_title(f"Per-pod hit rate ({runid})")
        fig.savefig(f"data/report-{runid}-hitrate.png")
        plt.close(fig)

    lines = [f"# Runpod cache testbed report — {runid}", ""]

    h = headline(jobs)
    lines.append("## Headline")
    lines.append("")
    if h["speedup"] is not None:
        lines.append(f"- **Cold→warm speedup: {h['speedup']:.1f}× faster** "
                     f"(median wall {h['cold_median_s']:.2f}s → {h['warm_median_s']:.3f}s)")
    lines.append(f"- Jobs completed OK: {h['n_ok']}")
    lines.append(f"- Total bytes served: {h['total_bytes']}")
    lines.append("")

    cs = coldstart(jobs)

    def _fmt(v, spec: str) -> str:  # None-safe number formatting for the table
        return format(v, spec) if v is not None else "n/a"

    lines.append("## Cold-start vs steady-state")
    lines.append("")
    lines.append(f"- Cold (first-invocation) jobs: {cs['n_cold']}")
    lines.append(f"- Cold mean wall_seconds: {_fmt(cs['cold_mean_s'], '.3f')}")
    lines.append(f"- Warm mean wall_seconds: {_fmt(cs['warm_mean_s'], '.3f')}")
    lines.append(f"- Mean dep-upgrade ms: {_fmt(cs['dep_upgrade_ms'], '.0f')}")
    lines.append("")

    lines.append("## Latency by phase")
    lines.append("")
    lines.append("| phase | n | median_s | p95_s | total_bytes |")
    lines.append("|---|---|---|---|---|")
    for phase in phases:
        d = latency[phase]
        lines.append(f"| {phase} | {d['n']} | {d['median_s']:.3f} | {d['p95_s']:.3f} | {d['total_bytes']} |")
    lines.append("")

    lines.append("## Per-pod hit rate / WAN bytes saved")
    lines.append("")
    if not metric_rows:
        lines.append("_No pod metrics captured for this run "
                     "(`make scrape` was not running); section omitted._")
    else:
        lines.append("| pod | effective_hit_rate | wan_bytes_saved |")
        lines.append("|---|---|---|")
        for pod in pods:
            d = per_pod[pod]
            lines.append(f"| {pod} | {d['effective_hit_rate']:.4f} | {d['wan_bytes_saved']} |")
    lines.append("")

    lines.append("## Peering payoff")
    lines.append("")
    if not metric_rows:
        lines.append("_No pod metrics captured for this run "
                     "(`make scrape` was not running); section omitted._")
    else:
        lines.append(f"- peer_bytes: {payoff['peer_bytes']}")
        lines.append(f"- wan_bytes: {payoff['wan_bytes']}")
        lines.append(f"- peer_fraction: {payoff['peer_fraction']:.4f}")
        lines.append(f"- hedge_win_ratio: {payoff['hedge_win_ratio']:.4f}")
    lines.append("")

    lines.append("## Cost note")
    lines.append("")
    lines.append("TODO(dean, 2026-09-18): pull per-pod GPU-hour billing and WAN egress "
                  "pricing once Runpod cost-export API access is confirmed; not available "
                  "at report-generation time.")
    lines.append("")

    out_path = f"data/report-{runid}.md"
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
