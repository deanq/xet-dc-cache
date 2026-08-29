#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pandas>=2.0", "pyarrow>=14.0", "numpy>=1.24"]
# ///
"""Generate illustrative sample data for placement_locality_sim.py.

Emits events.parquet (worker-start log) + xorbs.parquet (Tier 2 map). Designed to
exercise BOTH signals the sim reports:
  - stickiness: placement prefers hosts already warm for a model, so
    stickiness_x > 1 (unlike a flat random set).
  - cache-size knee: ~60 models with a Zipf popularity tail and a working set
    far larger than the smallest cache, so hit_rate climbs with cache_gib then
    plateaus — the plateau is the NVMe-sizing signal.
Model index 0 has two revisions sharing 80% of xorbs (the Tier 2 dedup case).

This is a DEMO fixture, not real data; replace with the scheduler-log extract
(see the SQL in placement-locality-measurement.md) for a real answer.

    uv run make_sample_data.py            # writes events.parquet, xorbs.parquet
"""

from __future__ import annotations

import numpy as np
import pandas as pd

GIB = 1024**3
RNG = np.random.default_rng(7)

N_STARTS = 20_000
HOSTS = [f"h{i:02d}" for i in range(60)]
HOSTS_PER_RACK = 10  # 60 hosts across 6 racks — rack scope sits between host and DC
STICKINESS = 0.75  # P(new worker lands on a host already warm for the model)


def rack_of(host: str) -> str:
    return f"rack-{int(host[1:]) // HOSTS_PER_RACK}"

N_MODELS = 60
SIZE_CHOICES_GIB = np.array([7, 13, 14, 32, 70, 145])
SIZE_WEIGHTS = np.array([0.25, 0.2, 0.2, 0.2, 0.1, 0.05])
XORB_GIB = 1.5
DEDUP_MODEL_IDX = 0     # this model gets two revisions...
DEDUP_SHARE = 0.8       # ...sharing this fraction of xorbs


def build_catalog() -> list[dict]:
    """~60 models with varied sizes; index 0 carries a second revision."""
    catalog = []
    for i in range(N_MODELS):
        size_gib = int(RNG.choice(SIZE_CHOICES_GIB, p=SIZE_WEIGHTS))
        repo = f"org{i % 7}/model-{i:02d}"
        revs = ["r1", "r2"] if i == DEDUP_MODEL_IDX else ["r1"]
        for rev in revs:
            catalog.append(dict(idx=i, repo_id=repo, revision=rev, size_gib=size_gib))
    return catalog


def zipf_weights(n: int) -> np.ndarray:
    """Popularity ∝ 1/rank — a few models dominate starts, long tail is rare."""
    w = 1.0 / np.arange(1, n + 1)
    return w / w.sum()


def make_events(catalog: list[dict]) -> pd.DataFrame:
    weights = zipf_weights(len(catalog))
    warm_hosts: dict[tuple, list[str]] = {}
    rows = []
    ts = 1_700_000_000
    for _ in range(N_STARTS):
        ts += int(RNG.integers(1, 120))
        entry = catalog[RNG.choice(len(catalog), p=weights)]
        model = (entry["repo_id"], entry["revision"])
        warm = warm_hosts.get(model, [])
        if warm and RNG.random() < STICKINESS:
            host = str(RNG.choice(warm))
        else:
            host = str(RNG.choice(HOSTS))
            warm_hosts.setdefault(model, [])
            if host not in warm_hosts[model]:
                warm_hosts[model].append(host)
        size_gib = entry["size_gib"]
        rows.append(
            dict(
                ts=ts,
                endpoint_id=f"ep-{entry['repo_id'].split('/')[-1]}",
                repo_id=entry["repo_id"],
                revision=entry["revision"],
                host_id=host,
                gpu_type="H100" if size_gib < 100 else "H200",
                datacenter="US-CA-1",
                rack_id=rack_of(host),
                model_size_bytes=size_gib * GIB,
                cold_pull_bytes=size_gib * GIB,
                pull_duration_s=size_gib * 8,  # ~8s/GiB cold pull
            )
        )
    return pd.DataFrame(rows)


def make_xorb_map(catalog: list[dict]) -> pd.DataFrame:
    """~1.5 GiB xorbs per model; the dedup model's r2 reuses 80% of r1's xorbs."""
    rows = []
    for entry in catalog:
        n = max(1, round(entry["size_gib"] / XORB_GIB))
        for i in range(n):
            reused = (
                entry["idx"] == DEDUP_MODEL_IDX
                and entry["revision"] == "r2"
                and i < int(n * DEDUP_SHARE)
            )
            source_rev = "r1" if reused else entry["revision"]
            rows.append(
                dict(
                    repo_id=entry["repo_id"],
                    revision=entry["revision"],
                    xorb_hash=f"{entry['repo_id']}:{source_rev}:{i}",
                    xorb_size_bytes=int(XORB_GIB * GIB),
                )
            )
    return pd.DataFrame(rows)


def main() -> None:
    catalog = build_catalog()
    events = make_events(catalog)
    xorbs = make_xorb_map(catalog)
    events.to_parquet("events.parquet")
    xorbs.to_parquet("xorbs.parquet")
    working_set_gib = sum(e["size_gib"] for e in catalog)
    print(
        f"wrote events.parquet ({len(events):,} starts), "
        f"xorbs.parquet ({len(xorbs)} xorbs) | "
        f"{len(catalog)} model-revisions, ~{working_set_gib} GiB working set"
    )


if __name__ == "__main__":
    main()
