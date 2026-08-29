#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27", "hf_xet==1.6.0"]
# ///
"""xet_verify exerciser — parity proof + chunk-level dedup diagnostics.

WHAT THIS IS NOT: it does not verify a download against `X-Xet-Hash`. That
header is an HMAC-*salted* file hash whose salt is server-side and never sent to
the client, so it is not reproducible offline (see xet_verify/README.md). Neither
the reconstruction nor the xet-read-token carries any anchor we can recompute,
and the hf-xet download path itself does no content-address verification. So the
useful, *reproducible* work xet_verify enables is chunk-level analysis where the
shim controls both ends:

  --parity          No network. Proves `xet_verify.file_hash_hex(bytes)` equals
                    the workers' own hf-xet 1.6.0 hash on identical bytes. Run in
                    CI on every xet-core pin bump — if it fails, the shim's
                    chunker has drifted from the fleet and MUST NOT be trusted.

  --dedup A B       Downloads two files (repo[@rev]/path, via $HF_ENDPOINT or HF),
                    CDC-chunks each with the client's chunker, and reports the
                    shared-chunk count and shared bytes — the cross-file /
                    cross-revision dedup the Tier 2 cache monetizes, measured
                    with the real (unsalted) chunk hashes.

`xet_verify` is a compiled wheel (see xet_verify/README.md); inject it:

    uvx maturin@1 build --release -m xet_verify/Cargo.toml
    uv run --with xet_verify/target/wheels/xet_verify-*.whl tools/fsck.py --parity
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

import httpx

try:
    import xet_verify
except ImportError:
    sys.exit(
        "xet_verify not importable. Build + inject the wheel:\n"
        "  uvx maturin@1 build --release -m xet_verify/Cargo.toml\n"
        "  uv run --with xet_verify/target/wheels/xet_verify-*.whl tools/fsck.py ..."
    )

HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co")


def _auth() -> dict[str, str]:
    token = os.environ.get("HF_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def parity_selftest(sizes: tuple[int, ...] = (0, 1, 100_000, 5_000_000)) -> None:
    """xet_verify.file_hash_hex == hf-xet's own hash, identical bytes, no network."""
    import hf_xet

    print(f"xet_verify pinned to hf-xet {xet_verify.__hf_xet_version__} "
          f"(xet-core {xet_verify.__xet_core_tag__})")
    for n in sizes:
        data = os.urandom(n)
        with tempfile.NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            oracle = hf_xet.hash_files([f.name])[0].hash
        mine = xet_verify.file_hash_hex(data)
        print(f"  {n:>10,} B  {mine}  {'OK' if mine == oracle else 'MISMATCH'}")
        assert mine == oracle, f"parity broken at {n} B: {mine} != {oracle}"
    print("parity OK — shim chunker matches the fleet's hf-xet")


def _fetch(spec: str) -> bytes:
    """spec = 'org/model[@rev]/path/to/file' -> served bytes.

    org/model is always the first two path segments; the rest is the file path,
    and an optional @rev may be appended to the model segment.
    """
    org, model_rev, path = spec.split("/", 2)
    model, _, rev = model_rev.partition("@")
    url = f"{HF}/{org}/{model}/resolve/{rev or 'main'}/{path}"
    return httpx.get(url, follow_redirects=True, timeout=300, headers=_auth()).content


def dedup_report(a: str, b: str) -> None:
    """Chunk both files; report shared chunks + bytes using real chunk hashes."""
    ca = xet_verify.chunk_hashes(_fetch(a))
    cb = xet_verify.chunk_hashes(_fetch(b))
    # multiset of (len, hash) per chunk; shared = intersection by hash
    from collections import Counter

    ha = Counter(h for _, h in ca)
    len_by_hash = {h: n for n, h in ca}
    shared_hashes = ha & Counter(h for _, h in cb)
    shared_chunks = sum(shared_hashes.values())
    shared_bytes = sum(len_by_hash[h] * k for h, k in shared_hashes.items())
    tot_a = sum(n for n, _ in ca)
    tot_b = sum(n for n, _ in cb)
    print(f"A {a}: {len(ca):,} chunks, {tot_a:,} B")
    print(f"B {b}: {len(cb):,} chunks, {tot_b:,} B")
    print(f"shared: {shared_chunks:,} chunks, {shared_bytes:,} B "
          f"({shared_bytes / max(tot_b, 1):.1%} of B dedups against A)")


def main() -> None:
    ap = argparse.ArgumentParser(description="xet_verify exerciser")
    ap.add_argument("--parity", action="store_true", help="offline parity self-test")
    ap.add_argument("--dedup", nargs=2, metavar=("A", "B"),
                    help="two 'org/model[@rev]/path' specs to compare")
    args = ap.parse_args()

    if args.parity:
        parity_selftest()
        return
    if args.dedup:
        dedup_report(*args.dedup)
        return
    ap.error("pass --parity, or --dedup A B")


if __name__ == "__main__":
    main()
