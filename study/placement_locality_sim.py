#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pandas>=2.0", "pyarrow>=14.0", "numpy>=1.24"]
# ///
"""Placement-locality cache simulation for the Xet DC cache.

Retrospective log-replay study (see placement-locality-measurement.md §4-6).
Nothing in the data path: reads a worker-start event log, replays it through a
simulated LRU cache at host / rack / datacenter scope, and reports the hit rate
that decides whether host-local Tier 1 is worth building.

Input: CSV or Parquet with the worker_start schema (§3 of the design doc):
    ts, endpoint_id, repo_id, revision, host_id, gpu_type, datacenter,
    model_size_bytes, [cold_pull_bytes], [pull_duration_s], [rack_id]

Two modes:
    tier1 (default) — cache whole models keyed by (repo_id, revision). Answers
        "does host-local range caching pay off" (the scale-out cold-start tax).
    tier2 — cache individual xorbs keyed by hash; a start partially hits on the
        xorbs already present. Byte-weighted. Measures cross-revision dedup.
        Requires --xorb-map with columns: repo_id, revision, xorb_hash,
        xorb_size_bytes  (one row per xorb per revision).

Usage:
    python placement_locality_sim.py events.parquet --mode tier1 --ttl-hours 24
    python placement_locality_sim.py events.parquet --mode tier2 \
        --xorb-map xorbs.parquet --scopes datacenter --cache-sizes 1000 2000 4000
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

GIB = 1024**3
REQUIRED_COLUMNS = (
    "ts",
    "repo_id",
    "revision",
    "host_id",
    "gpu_type",
    "datacenter",
    "model_size_bytes",
)


# --------------------------------------------------------------------------- #
# Cache model
# --------------------------------------------------------------------------- #
@dataclass
class LruCache:
    """Size-bounded LRU with an optional wall-clock TTL.

    Keys are model identities `(repo_id, revision)`; values are (size, last_ts).
    Eviction is by least-recently-used until the new entry fits.
    """

    capacity_bytes: int
    ttl_seconds: float | None = None
    _entries: OrderedDict = field(default_factory=OrderedDict)
    _used_bytes: int = 0

    def get(self, key: tuple, now: float) -> bool:
        """Return True on hit. Expires TTL-stale entries first."""
        entry = self._entries.get(key)
        if entry is None:
            return False
        _, last_ts = entry
        if self.ttl_seconds is not None and now - last_ts > self.ttl_seconds:
            self._evict(key)
            return False
        self._entries.move_to_end(key)
        return True

    def put(self, key: tuple, size_bytes: int, now: float) -> None:
        if key in self._entries:
            self._entries.move_to_end(key)
            self._entries[key] = (size_bytes, now)
            return
        if size_bytes > self.capacity_bytes:
            return  # never fits; leave cache untouched
        while self._used_bytes + size_bytes > self.capacity_bytes:
            self._evict(next(iter(self._entries)))
        self._entries[key] = (size_bytes, now)
        self._used_bytes += size_bytes

    def _evict(self, key: tuple) -> None:
        size, _ = self._entries.pop(key)
        self._used_bytes -= size


def scope_key(row: pd.Series, scope: str) -> str:
    """Cache node identity for the chosen scope."""
    if scope == "host":
        return str(row["host_id"])
    if scope == "rack":
        # Fall back to host if rack_id absent — degrades to host-local.
        return str(row.get("rack_id", row["host_id"]))
    if scope == "datacenter":
        return str(row["datacenter"])
    raise ValueError(f"unknown scope: {scope}")


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def simulate(
    events: pd.DataFrame,
    scope: str,
    capacity_bytes: int,
    ttl_seconds: float | None,
) -> pd.DataFrame:
    """Replay events through per-node LRU caches. Returns per-event hit flags.

    Caches are keyed by scope node; each holds `(repo_id, revision)` models.
    Segment by gpu_type: a model runnable on multiple SKUs lives in disjoint
    host pools, so mixing them would understate real locality (§9).
    """
    caches: dict[str, LruCache] = {}
    hits: list[bool] = []
    for row in events.itertuples(index=False):
        row = pd.Series(row._asdict())
        node = f"{row['gpu_type']}::{scope_key(row, scope)}"
        model = (row["repo_id"], row["revision"])
        now = float(row["ts"])
        cache = caches.setdefault(node, LruCache(capacity_bytes, ttl_seconds))
        hit = cache.get(model, now)
        hits.append(hit)
        cache.put(model, int(row["model_size_bytes"]), now)
    out = events.copy()
    out["hit"] = hits
    return out


def random_placement_baseline(
    events: pd.DataFrame, scope: str, ttl_seconds: float | None
) -> float:
    """Expected hit rate if placement were random — the stickiness gut-check.

    Point-in-time: for each start, count nodes warm for that model *at that
    instant* (served it within TTL), over the candidate pool for its
    gpu_type+datacenter. Compared against the measured rate, this separates real
    scheduler stickiness (measured >> baseline) from pool-size luck (≈ baseline).

    Whole-window counting would saturate to ~1.0 on small inputs and mask the
    signal; point-in-time stays honest at any scale.
    """
    pools = events.copy()
    pools["node"] = pools.apply(lambda r: scope_key(r, scope), axis=1)
    pools["pool"] = list(zip(pools["gpu_type"], pools["datacenter"]))
    candidate = pools.groupby("pool")["node"].nunique().to_dict()

    warm: dict[tuple, dict[str, float]] = {}  # (pool, repo, rev) -> {node: last_ts}
    expectations: list[float] = []
    for pool, repo, rev, node, ts in zip(
        pools["pool"], pools["repo_id"], pools["revision"], pools["node"], pools["ts"]
    ):
        now = float(ts)
        seen = warm.setdefault((pool, repo, rev), {})
        if ttl_seconds is not None:
            seen = {n: t for n, t in seen.items() if now - t <= ttl_seconds}
            warm[(pool, repo, rev)] = seen
        expectations.append(min(1.0, len(seen) / candidate[pool]))
        seen[node] = now  # this start makes the node warm for subsequent starts
    return float(np.mean(expectations)) if expectations else 0.0


# --------------------------------------------------------------------------- #
# Tier 2 — content-addressed xorb replay (cross-revision dedup)
# --------------------------------------------------------------------------- #
XORB_MAP_COLUMNS = ("repo_id", "revision", "xorb_hash", "xorb_size_bytes")


def load_xorb_map(path: Path) -> dict[tuple, list[tuple[str, int]]]:
    """Map (repo_id, revision) -> [(xorb_hash, size_bytes), ...]."""
    reader = pd.read_parquet if path.suffix == ".parquet" else pd.read_csv
    frame = reader(path)
    missing = [c for c in XORB_MAP_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"xorb map missing required columns: {missing}")
    mapping: dict[tuple, list[tuple[str, int]]] = {}
    for row in frame.itertuples(index=False):
        key = (row.repo_id, row.revision)
        mapping.setdefault(key, []).append((row.xorb_hash, int(row.xorb_size_bytes)))
    return mapping


def simulate_tier2(
    events: pd.DataFrame,
    xorb_map: dict[tuple, list[tuple[str, int]]],
    scope: str,
    capacity_bytes: int,
    ttl_seconds: float | None,
) -> pd.DataFrame:
    """Replay events through per-node xorb caches keyed by hash.

    Each start partially hits: xorbs already cached are free, the rest are
    fetched. Byte-weighted, so a revision bump that reuses unchanged xorbs
    scores a high hit even though (repo, revision) is new. gpu_type does NOT
    segment here — xorb content is identical across SKUs, so dedup is global
    to the scope node.

    Granularity note: this keys by whole xorb hash, the dedup *upper bound*. The
    real cache keys on (hash, byte-range) because there is no whole-xorb download
    (design §5); the two agree for whole-file reuse (the common revision-bump
    case) and diverge only when files share a xorb but request different ranges.
    """
    caches: dict[str, LruCache] = {}
    hit_bytes_col: list[int] = []
    total_bytes_col: list[int] = []
    for row in events.itertuples(index=False):
        row = pd.Series(row._asdict())
        node = scope_key(row, scope)
        now = float(row["ts"])
        cache = caches.setdefault(node, LruCache(capacity_bytes, ttl_seconds))
        xorbs = xorb_map.get((row["repo_id"], row["revision"]), [])
        hit_bytes = 0
        total_bytes = 0
        for xorb_hash, size in xorbs:
            total_bytes += size
            if cache.get((xorb_hash,), now):
                hit_bytes += size
            cache.put((xorb_hash,), size, now)
        hit_bytes_col.append(hit_bytes)
        total_bytes_col.append(total_bytes)
    out = events.copy()
    out["hit_bytes"] = hit_bytes_col
    out["total_bytes"] = total_bytes_col
    return out


def summarize_tier2(result: pd.DataFrame) -> dict:
    """Byte-weighted dedup hit rate = cached bytes / total xorb bytes served."""
    total = int(result["total_bytes"].sum())
    hit = int(result["hit_bytes"].sum())
    unmapped = int((result["total_bytes"] == 0).sum())
    return {
        "starts": len(result),
        "hit_rate": round(hit / total, 4) if total else 0.0,
        "wan_gib_saved": round(hit / GIB, 1),
        "unmapped_starts": unmapped,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarize(result: pd.DataFrame) -> dict:
    """Volume-weighted hit rate + WAN saved (§9: weight by starts, not models)."""
    total = len(result)
    hit_rate = result["hit"].mean() if total else 0.0
    saved_bytes = 0
    if "cold_pull_bytes" in result:
        saved_bytes = int(result.loc[result["hit"], "cold_pull_bytes"].sum())
    saved_seconds = 0.0
    if "pull_duration_s" in result:
        saved_seconds = float(result.loc[result["hit"], "pull_duration_s"].sum())
    return {
        "starts": total,
        "hit_rate": round(hit_rate, 4),
        "wan_gib_saved": round(saved_bytes / GIB, 1),
        "cold_start_hours_saved": round(saved_seconds / 3600, 1),
    }


def load_events(path: Path) -> pd.DataFrame:
    reader = pd.read_parquet if path.suffix == ".parquet" else pd.read_csv
    events = reader(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in events.columns]
    if missing:
        raise ValueError(f"event log missing required columns: {missing}")
    return events.sort_values("ts").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path, help="CSV or Parquet worker-start log")
    parser.add_argument("--mode", choices=["tier1", "tier2"], default="tier1")
    parser.add_argument(
        "--xorb-map",
        type=Path,
        default=None,
        help="tier2 only: (repo_id, revision, xorb_hash, xorb_size_bytes) table",
    )
    parser.add_argument("--scopes", nargs="+", default=["host", "rack", "datacenter"])
    parser.add_argument(
        "--cache-sizes",
        nargs="+",
        type=int,
        default=[200, 500, 1000, 2000, 4000],
        help="per-node cache sizes in GiB to sweep",
    )
    parser.add_argument(
        "--ttl-hours",
        type=float,
        default=None,
        help="cache-worker recycle TTL; omit for pure size-bound LRU",
    )
    args = parser.parse_args()

    events = load_events(args.events)
    ttl_seconds = args.ttl_hours * 3600 if args.ttl_hours else None

    if args.mode == "tier2" and args.xorb_map is None:
        parser.error("--mode tier2 requires --xorb-map")

    xorb_map = load_xorb_map(args.xorb_map) if args.mode == "tier2" else None

    print(f"loaded {len(events):,} starts | mode={args.mode} | ttl={args.ttl_hours or 'none'}h\n")
    rows = []
    for scope in args.scopes:
        baseline = (
            random_placement_baseline(events, scope, ttl_seconds)
            if args.mode == "tier1"
            else None
        )
        for size_gib in args.cache_sizes:
            if args.mode == "tier1":
                result = simulate(events, scope, size_gib * GIB, ttl_seconds)
                summary = summarize(result)
                summary["random_baseline"] = round(baseline, 4)
                summary["stickiness_x"] = (
                    round(summary["hit_rate"] / baseline, 2) if baseline else float("inf")
                )
            else:
                result = simulate_tier2(events, xorb_map, scope, size_gib * GIB, ttl_seconds)
                summary = summarize_tier2(result)
            summary.update(scope=scope, cache_gib=size_gib)
            rows.append(summary)

    tier1_cols = [
        "scope", "cache_gib", "hit_rate", "random_baseline", "stickiness_x",
        "wan_gib_saved", "cold_start_hours_saved", "starts",
    ]
    tier2_cols = ["scope", "cache_gib", "hit_rate", "wan_gib_saved", "unmapped_starts", "starts"]
    cols = tier1_cols if args.mode == "tier1" else tier2_cols
    print(pd.DataFrame(rows)[cols].to_string(index=False))


if __name__ == "__main__":
    main()
