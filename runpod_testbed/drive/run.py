# Workload driver: expands the multi-model overlap matrix into a
# COLD-then-WARM-BURST job list, submits baseline jobs to the control
# endpoint, then the mechanism's job plan (`mechanism.jobs`) to the
# endpoints recorded in `ProvisionState`, and records per-job timings to
# JSONL.
#
# flash_manifest.json shape (confirmed 2026-09-22 against a live `flash deploy`
# output): {"resources": {"xet-dl-A": {"functions": [...], "endpoint_id": "..."},
# ...}}. `endpoint_ids` below parses that shape, keying each endpoint by the
# trailing group letter. The resources carry resource_type "CpuLiveServerless"
# / is_live_resource:true, but that is a manifest scan-time artifact — because
# they are is_load_balanced:false, Flash deploys them as queue-based endpoints
# invoked via runpod.Endpoint(eid).run()/.runsync() (the `/runsync` path).
from __future__ import annotations
import argparse
import concurrent.futures
import json
import os
import sys
import time


def expand_jobs(overlap: dict, burst: int) -> list:
    cold, warm = [], []
    for g, models in overlap.items():
        for m in models:
            cold.append({"group": g, "model": m, "phase": "cold", "replica": 0})
            for r in range(burst):
                warm.append({"group": g, "model": m, "phase": "warm", "replica": r})
    return cold + warm


def endpoint_ids(manifest: dict) -> dict:
    # Flash writes {"resources": {"xet-dl-A": {..., "endpoint_id": "..."}}}.
    # Key each endpoint by the trailing group letter (xet-dl-A -> "A").
    out = {}
    for res_name, res in manifest.get("resources", {}).items():
        out[res_name.rsplit("-", 1)[-1]] = res["endpoint_id"]
    return out


def start_jobs_file(path: str) -> None:
    """Truncate/create the per-run jobs file before recording into it.

    record() appends, so without this a second drive pass on the same RUNID
    would append to (and silently double-count) the previous pass's rows.
    Reset to a clean file at the start of each run instead.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").close()


SEQUENTIAL_PHASES = ("baseline", "cold", "populate")


def record(job: dict, result: dict, submit_ts: float, path: str) -> None:
    return_ts = time.time()
    row = {**job, "submit_ts": submit_ts, "return_ts": return_ts, "result": result}
    if "mechanism" in job:  # shared timing schema (spec) — driver-observed wall
        from runpod_testbed.worker.timing import make_timing_row
        row["timing"] = make_timing_row(job["mechanism"], job, result, return_ts - submit_ts)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def split_jobs(jobs: list) -> tuple[list, list]:
    """Sequential phases (baseline / populate) first, then the warm burst."""
    seq = [j for j in jobs if j["phase"] in SEQUENTIAL_PHASES]
    burst = [j for j in jobs if j["phase"] not in SEQUENTIAL_PHASES]
    return seq, burst


def resolve_endpoints(jobs: list, endpoints: dict) -> dict:
    wanted = sorted({j["endpoint"] for j in jobs})
    missing = [w for w in wanted if w not in endpoints]
    if missing:
        raise ValueError(f"no endpoint id recorded for labels {missing}; state has {sorted(endpoints)}")
    return {w: endpoints[w] for w in wanted}


def _submit_and_wait(eid: str, job: dict, jobs_path: str, timeout_s: int) -> None:
    import runpod

    submit_ts = time.time()
    payload = {"input": {"models": [job["model"]]}}
    handle = runpod.Endpoint(eid).run(payload)
    # runpod's Job.output(timeout=0) does NOT poll — it returns None the instant
    # the result isn't ready, which for a cold worker is always. Pass a real
    # ceiling so we wait for boot + dep install + download.
    #
    # Any failure here is a value, not a crash: a job timeout OR a transient
    # error from the Runpod API during polling (e.g. requests.ReadTimeout on the
    # SDK's 10s status GET) must not kill the burst pool and abort the whole run.
    # Record it (with the last status if reachable) and move on.
    try:
        result = handle.output(timeout=timeout_s)
    except Exception as e:
        try:
            status = handle.status()
        except Exception:
            status = None
        result = {"ok": False, "error": f"{type(e).__name__}: {e}", "status": status}
    record(job, result, submit_ts, jobs_path)


def main(argv: list | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", default="config.toml")
    parser.add_argument("runid")
    parser.add_argument("--dry-run", metavar="CACHE_URL", default=None)
    args = parser.parse_args(argv)

    from runpod_testbed.config import load

    cfg = load(args.config, mechanism_override=os.environ.get("MECHANISM"))

    if args.dry_run:
        os.environ["HF_ENDPOINT"] = args.dry_run
        from runpod_testbed.worker.timing import hf_download

        for m in cfg.models:
            print(m, hf_download(m))
        return

    from runpod_testbed.mechanisms import get_mechanism
    from runpod_testbed.mechanisms.base import ProvisionState, state_path
    from runpod_testbed.mechanisms.baseline import baseline_jobs

    state = ProvisionState.load(state_path(args.runid))  # validates runid before spend
    mech = get_mechanism(state.mechanism)
    jobs = baseline_jobs(cfg.models) + mech.jobs(cfg)
    eids = resolve_endpoints(jobs, state.endpoints)
    sequential, burst = split_jobs(jobs)

    os.makedirs("data", exist_ok=True)
    jobs_path = f"data/jobs-{args.runid}.jsonl"
    start_jobs_file(jobs_path)  # fresh file so a re-run doesn't double-count

    for job in sequential:
        _submit_and_wait(eids[job["endpoint"]], job, jobs_path, cfg.job_timeout_s)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.burst) as pool:
        futures = [pool.submit(_submit_and_wait, eids[job["endpoint"]], job,
                               jobs_path, cfg.job_timeout_s)
                   for job in burst]
        for f in concurrent.futures.as_completed(futures):
            f.result()


if __name__ == "__main__":
    main()
