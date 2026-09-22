# Workload driver: expands the multi-model overlap matrix into a
# COLD-then-WARM-BURST job list, submits jobs to the 3 Flash serverless
# endpoints, and records per-job timings to JSONL.
#
# NOTE on flash_manifest.json shape (unverified as of Task 5):
# `endpoint_ids` below assumes manifest = {"endpoints": [{"function":
# "xet-dl-A", "endpoint_id": "..."}]}. A best-effort check of the Flash
# docs/source (docs.runpod.io/flash, github.com/runpod/flash) suggests the
# real manifest instead carries a "functions" array (name/module/
# resource_name/is_class/routes) plus a "resources" array, and that
# function-name -> endpoint_id resolution happens at call time via a
# separate State Manager (GraphQL) lookup keyed on (environment_id,
# resource_name) -- not as a static "endpoints" list with function/
# endpoint_id pairs baked into the manifest file. This assumed shape is
# unconfirmed against a real `flash deploy` output. Keep this
# implementation as-is (it matches the Task-5 brief and is unit-tested
# against the assumed shape below); confirm against a live manifest and
# adjust in Task 8 before spending real endpoint calls on it.
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


def record(job: dict, result: dict, submit_ts: float, path: str) -> None:
    row = {**job, "submit_ts": submit_ts, "return_ts": time.time(), "result": result}
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def _submit_and_wait(eid: str, job: dict, jobs_path: str) -> None:
    import runpod

    submit_ts = time.time()
    payload = {"input": {"models": [job["model"]]}}
    handle = runpod.Endpoint(eid).run(payload)
    result = handle.output()
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

    for job in cold:
        _submit_and_wait(eids[job["group"]], job, jobs_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.burst) as pool:
        futures = [pool.submit(_submit_and_wait, eids[job["group"]], job, jobs_path)
                   for job in warm]
        for f in concurrent.futures.as_completed(futures):
            f.result()


if __name__ == "__main__":
    main()
