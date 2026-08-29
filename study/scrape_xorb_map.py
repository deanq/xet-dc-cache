#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pandas>=2.0", "pyarrow>=14.0", "requests>=2.31", "huggingface_hub>=0.24"]
# ///
"""Offline builder for the Tier 2 xorb map (xorbs.parquet).

For each (repo_id, revision), lists the repo's files, reads each Xet-backed
file's reconstruction manifest from the CAS server, and flattens it to:

    repo_id, revision, xorb_hash, xorb_size_bytes

This is the pre-relay path (instrumentation doc §6): once the CAS relay exists it
emits these rows for free. Until then this standalone job produces a real
xorbs.parquet so Tier 2 dedup can be measured.

Protocol (verified against the live HF Xet API 2026-08):
  1. GET {endpoint}/api/{repo_type}s/{repo}/xet-read-token/{rev}
       -> {casUrl, accessToken, exp}. One call per revision, reused for all files.
  2. HEAD {endpoint}/{repo}/resolve/{rev}/{path}
       -> X-Xet-Hash (file_id). 302; no cas/token headers here (unlike design
       doc §3.1 — those come from step 1). No X-Xet-Hash => not Xet-backed, skip.
  3. GET {casUrl}/v2/reconstructions/{file_id}  (Bearer accessToken)
       -> terms[] + xorbs{hash: [{url, ranges:[{chunks, bytes}]}]}.
  4. xorb_size_bytes := max(bytes.end) observed for that hash. See §"Sizing".

Auth: token from --token or $HF_TOKEN (required for private repos / higher rate
limits). Revisions SHOULD be commit SHAs (instrumentation doc §4) so the map
joins cleanly against events.parquet.

Usage:
    uv run scrape_xorb_map.py --events events.parquet          # distinct (repo,rev)
    uv run scrape_xorb_map.py --repos org/model@<sha> org/m2@<sha>
    uv run scrape_xorb_map.py --self-test                      # no network/creds

Sizing note: the reconstruction reveals only the byte ranges a file *uses*
within a xorb, not the xorb's total stored size. We take the maximum observed
`bytes.end` across every file that references the xorb as a lower-bound estimate.
It is exact when some file references the xorb's tail (common); otherwise it
slightly undercounts. Verify against a known xorb before trusting absolute GiB
(design doc §12) — relative dedup ratios are unaffected.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import requests
from huggingface_hub import HfApi

DEFAULT_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested by --self-test; no network)
# --------------------------------------------------------------------------- #
def flatten_manifest(reconstruction: dict) -> dict[str, int]:
    """Map xorb_hash -> estimated size in bytes from a v2 reconstruction.

    Size = max observed `bytes.end` across all ranges referencing the xorb.
    """
    sizes: dict[str, int] = {}
    for xorb_hash, entries in reconstruction.get("xorbs", {}).items():
        for entry in entries:
            for rng in entry.get("ranges", []):
                end = int(rng["bytes"]["end"])
                sizes[xorb_hash] = max(sizes.get(xorb_hash, 0), end)
    return sizes


def merge_sizes(target: dict[str, int], incoming: dict[str, int]) -> None:
    """Accumulate xorb sizes across files, keeping the largest estimate."""
    for xorb_hash, size in incoming.items():
        target[xorb_hash] = max(target.get(xorb_hash, 0), size)


# --------------------------------------------------------------------------- #
# Network layer (isolated; each fn is one documented endpoint)
# --------------------------------------------------------------------------- #
def get_xet_token(
    session: requests.Session, endpoint: str, repo_id: str, revision: str, repo_type: str
) -> dict:
    """GET the read token + CAS url for a revision (reused across its files)."""
    url = f"{endpoint}/api/{repo_type}s/{repo_id}/xet-read-token/{revision}"
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    return {"cas_url": body["casUrl"], "access_token": body["accessToken"]}


def resolve_file_id(
    session: requests.Session, endpoint: str, repo_id: str, revision: str, path: str
) -> str | None:
    """HEAD the resolve route; return the file_id, or None if not Xet-backed."""
    url = f"{endpoint}/{repo_id}/resolve/{revision}/{path}"
    resp = session.head(url, allow_redirects=False, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.headers.get("X-Xet-Hash")


def fetch_reconstruction(
    session: requests.Session, cas_url: str, file_id: str, access_token: str
) -> dict:
    """GET the v2 reconstruction manifest for a file_id."""
    url = f"{cas_url}/v2/reconstructions/{file_id}"
    resp = session.get(
        url, headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def scrape_revision(
    session: requests.Session,
    api: HfApi,
    endpoint: str,
    repo_id: str,
    revision: str,
    repo_type: str,
    token: str | None,
) -> dict[str, int]:
    """All xorbs (hash -> size) referenced by one repo revision."""
    files = api.list_repo_files(repo_id, revision=revision, repo_type=repo_type, token=token)
    creds = get_xet_token(session, endpoint, repo_id, revision, repo_type)
    sizes: dict[str, int] = {}
    for path in files:
        file_id = resolve_file_id(session, endpoint, repo_id, revision, path)
        if file_id is None:
            continue  # LFS/plain file, not Xet-backed
        manifest = fetch_reconstruction(
            session, creds["cas_url"], file_id, creds["access_token"]
        )
        merge_sizes(sizes, flatten_manifest(manifest))
    return sizes


# --------------------------------------------------------------------------- #
# Input / output
# --------------------------------------------------------------------------- #
def targets_from_events(path: Path) -> list[tuple[str, str]]:
    reader = pd.read_parquet if path.suffix == ".parquet" else pd.read_csv
    frame = reader(path)
    pairs = frame[["repo_id", "revision"]].drop_duplicates()
    return list(pairs.itertuples(index=False, name=None))


def targets_from_args(specs: list[str]) -> list[tuple[str, str]]:
    """Parse 'repo_id@revision' specs."""
    out = []
    for spec in specs:
        if "@" not in spec:
            raise ValueError(f"expected repo_id@revision, got: {spec}")
        repo_id, revision = spec.rsplit("@", 1)
        out.append((repo_id, revision))
    return out


def build_rows(targets: list[tuple[str, str]], args, session, api) -> list[dict]:
    rows = []
    for repo_id, revision in targets:
        sizes = scrape_revision(
            session, api, args.endpoint, repo_id, revision, args.repo_type, args.token
        )
        for xorb_hash, size in sizes.items():
            rows.append(
                dict(
                    repo_id=repo_id,
                    revision=revision,
                    xorb_hash=xorb_hash,
                    xorb_size_bytes=size,
                )
            )
        print(f"  {repo_id}@{revision}: {len(sizes)} xorbs", file=sys.stderr)
    return rows


# --------------------------------------------------------------------------- #
# Self-test (proves flatten/merge without network or credentials)
# --------------------------------------------------------------------------- #
def self_test() -> None:
    manifest_a = {
        "offset_into_first_range": 0,
        "terms": [{"hash": "XORB_A", "unpacked_length": 100, "range": {"start": 0, "end": 4}}],
        "xorbs": {
            "XORB_A": [{"url": "x", "ranges": [{"chunks": {"start": 0, "end": 4},
                                                "bytes": {"start": 0, "end": 1_000_000}}]}],
            "XORB_B": [{"url": "y", "ranges": [{"chunks": {"start": 0, "end": 2},
                                                "bytes": {"start": 0, "end": 500_000}}]}],
        },
    }
    a = flatten_manifest(manifest_a)
    assert a == {"XORB_A": 1_000_000, "XORB_B": 500_000}, a

    # A second file references more of XORB_A -> size grows to the larger estimate.
    manifest_b = {
        "xorbs": {"XORB_A": [{"ranges": [{"bytes": {"start": 500_000, "end": 4_000_000}}]}]}
    }
    merged: dict[str, int] = {}
    merge_sizes(merged, a)
    merge_sizes(merged, flatten_manifest(manifest_b))
    assert merged == {"XORB_A": 4_000_000, "XORB_B": 500_000}, merged

    # Empty / non-Xet manifest yields nothing.
    assert flatten_manifest({}) == {}
    print("self-test OK")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--events", type=Path, help="parquet/csv event log; scrape its distinct (repo,revision)")
    src.add_argument("--repos", nargs="+", help="explicit repo_id@revision specs")
    parser.add_argument("--out", type=Path, default=Path("xorbs.parquet"))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="HF Hub endpoint")
    parser.add_argument("--repo-type", default="model", choices=["model", "dataset"])
    parser.add_argument("--token", default=os.environ.get("HF_TOKEN"), help="HF token (or $HF_TOKEN)")
    parser.add_argument("--self-test", action="store_true", help="run offline logic check and exit")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return
    if not (args.events or args.repos):
        parser.error("provide --events, --repos, or --self-test")

    targets = targets_from_events(args.events) if args.events else targets_from_args(args.repos)
    print(f"scraping {len(targets)} repo-revisions from {args.endpoint}", file=sys.stderr)

    session = requests.Session()
    if args.token:
        session.headers["Authorization"] = f"Bearer {args.token}"
    api = HfApi(endpoint=args.endpoint)

    rows = build_rows(targets, args, session, api)
    frame = pd.DataFrame(rows, columns=["repo_id", "revision", "xorb_hash", "xorb_size_bytes"])
    frame.to_parquet(args.out)
    total_gib = frame["xorb_size_bytes"].sum() / 1024**3
    print(f"wrote {args.out} ({len(frame)} xorbs, ~{total_gib:.1f} GiB)", file=sys.stderr)


if __name__ == "__main__":
    main()
