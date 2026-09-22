# Workload driver: expands the multi-model overlap matrix into a
# COLD-then-WARM-BURST job list, submits jobs to the 3 Flash serverless
# endpoints, and records per-job timings to JSONL.
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


def record(job: dict, result: dict, submit_ts: float, path: str) -> None:
    row = {**job, "submit_ts": submit_ts, "return_ts": time.time(), "result": result}
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def _submit_and_wait(eid: str, job: dict, jobs_path: str, timeout_s: int) -> None:
    import runpod

    submit_ts = time.time()
    payload = {"input": {"models": [job["model"]]}}
    handle = runpod.Endpoint(eid).run(payload)
    # runpod's Job.output(timeout=0) does NOT poll — it returns None the instant
    # the result isn't ready, which for a cold worker is always. Pass a real
    # ceiling so we wait for boot + dep install + download. A timeout is a value:
    # record it (with the last status) instead of crashing the burst pool.
    try:
        result = handle.output(timeout=timeout_s)
    except TimeoutError as e:
        result = {"ok": False, "error": str(e), "status": handle.status()}
    record(job, result, submit_ts, jobs_path)


def main(argv: list | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", default="config.toml")
    parser.add_argument("runid")
    parser.add_argument("--dry-run", metavar="CACHE_URL", default=None)
    args = parser.parse_args(argv)

    from runpod_testbed.config import load

    cfg = load(args.config)

    if args.dry_run:
        os.environ["HF_ENDPOINT"] = args.dry_run
        from runpod_testbed.worker.timing import hf_download

        for m in cfg.models:
            print(m, hf_download(m))
        return

    from runpod_testbed.provision.up import State

    State.load(f"data/state-{args.runid}.json")  # validates runid before spend

    with open("runpod_testbed/worker/.flash/flash_manifest.json") as fh:
        manifest = json.load(fh)
    eids = endpoint_ids(manifest)

    jobs = expand_jobs(cfg.overlap, cfg.burst)
    cold = [j for j in jobs if j["phase"] == "cold"]
    warm = [j for j in jobs if j["phase"] == "warm"]

    os.makedirs("data", exist_ok=True)
    jobs_path = f"data/jobs-{args.runid}.jsonl"
    start_jobs_file(jobs_path)  # fresh file so a re-run doesn't double-count

    for job in cold:
        _submit_and_wait(eids[job["group"]], job, jobs_path, cfg.job_timeout_s)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.burst) as pool:
        futures = [pool.submit(_submit_and_wait, eids[job["group"]], job,
                               jobs_path, cfg.job_timeout_s)
                   for job in warm]
        for f in concurrent.futures.as_completed(futures):
            f.result()


if __name__ == "__main__":
    main()
