#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""Empirical Tier 2 payoff study — how much of a model re-pull is already cached?

Two cheap-to-expensive layers:

  probe   For each commit in a repo's history, HEAD the target file(s) and record
          the X-Xet-Hash (the file identity). NO downloads. Answers: across a
          repo's real history, how often does a new revision leave the big
          weight file byte-IDENTICAL? Those revisions are 100% cache hits — the
          cross-revision whole-file dedup the cache captures for free.

          It also surfaces adjacent commits where the file CHANGED — the only
          interesting inputs for chunk-level (Tier 2) measurement.

Feed a changed pair to `tools/fsck.py --dedup repo@shaA/path repo@shaB/path` to
measure how much survives at the chunk level (Tier 2's incremental payoff over a
whole-file cache).
"""

from __future__ import annotations

import argparse
import os

import httpx

HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co")


def _auth() -> dict[str, str]:
    tok = os.environ.get("HF_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def commits(repo: str, client: httpx.Client, limit: int) -> list[dict]:
    """Newest-first commit list for a model repo."""
    out: list[dict] = []
    for page in range(0, 20):  # HF paginates with a 0-indexed `p`
        r = client.get(f"{HF}/api/models/{repo}/commits/main",
                        params={"p": page}, headers=_auth())
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(out) >= limit:
            break
    return out[:limit]


def xet_hash(repo: str, sha: str, path: str, client: httpx.Client) -> tuple[str | None, str | None]:
    """(X-Xet-Hash, content-length) for a file at a commit; (None, None) if absent."""
    r = client.head(f"{HF}/{repo}/resolve/{sha}/{path}",
                    follow_redirects=False, headers=_auth())
    if r.status_code not in (200, 302):
        return None, None
    return r.headers.get("X-Xet-Hash"), r.headers.get("X-Linked-Size") or r.headers.get("Content-Length")


def probe(repo: str, paths: list[str], limit: int) -> None:
    c = httpx.Client(timeout=60)
    log = commits(repo, c, limit)
    print(f"{repo}: {len(log)} commits (newest first)\n")
    for path in paths:
        print(f"# {path}")
        prev: str | None = None
        identical = changed = missing = 0
        changed_pairs: list[tuple[str, str]] = []
        rows = []
        for cm in log:
            sha = cm["id"]
            h, size = xet_hash(repo, sha, path, c)
            if h is None:
                missing += 1
                mark = "—  (not xet/absent)"
            elif prev is None:
                mark = "first"
            elif h == prev:
                identical += 1
                mark = "= identical"
            else:
                changed += 1
                mark = "≠ CHANGED"
                changed_pairs.append((sha, rows[-1][0]))
            rows.append((sha, h, size, mark))
            if h is not None:
                prev = h
        for sha, h, size, mark in rows:
            hs = (h[:16] + "…") if h else "-"
            print(f"  {sha[:10]}  {str(size or '-'):>10}  {hs}  {mark}")
        transitions = identical + changed
        if transitions:
            print(f"  → {identical}/{transitions} revision transitions left this file "
                  f"IDENTICAL ({identical / transitions:.0%} free cross-revision hits)")
        if changed_pairs:
            newsha, oldsha = changed_pairs[0]
            print(f"  → changed pair to measure at chunk level:")
            print(f"     uv run --with <wheel> tools/fsck.py --dedup \\")
            print(f"       {repo}@{oldsha}/{path} {repo}@{newsha}/{path}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Tier 2 payoff study")
    ap.add_argument("repo")
    ap.add_argument("paths", nargs="+", help="file path(s) in the repo")
    ap.add_argument("--limit", type=int, default=40, help="max commits to scan")
    ap.parse_args()
    args = ap.parse_args()
    probe(args.repo, args.paths, args.limit)


if __name__ == "__main__":
    main()
