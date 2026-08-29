# xet_verify — pinned xet-core chunker + hash (and why download verification is impossible)

A thin pyo3 wrapper over **xet-core**, pinned to the exact release the fleet's
`hf-xet` is built from (**v1.6.0** / xet-core tag `v1.6.0`). It reproduces the
*client's own* CDC chunker and file-hash — nothing hand-rolled (fail-secure: a
drifted chunker would silently disagree with the fleet). `--parity` proves the
reproduction is exact against the installed `hf_xet`.

## The finding: content-address verification of downloads is NOT achievable

This crate began as "product B: verify served bytes against the file's
`X-Xet-Hash`." Building it disproved that goal. Verified live (2026-08):

| Candidate anchor | Reproducible from wire data? |
|---|---|
| Xorb hash (`term.hash`) | ❌ Needs whole-xorb bytes; the ~42 KB footer sits past the signed range and the CDN 403s beyond it (the earlier Tier 2 finding). |
| Per-chunk / per-range hash | ❌ Reconstruction v2 exposes **no** hashes — only xorb hash + chunk-index ranges. And `range_hash_from_chunks` is itself **HMAC-salted**. |
| **File hash** (`X-Xet-Hash`) | ❌ It is an HMAC-**salted** file hash. The salt is server-side; the xet-read-token carries only `{casUrl, exp, accessToken}`. Never sent → not reproducible. |

Proof it's salted: on identical bytes, `xet_verify.file_hash_hex` ==
`hf_xet.hash_files` (the client's *unsalted* hash) but **≠** `X-Xet-Hash`, and
≠ sha256. See `m1/fsck.py --parity`.

Crucially, the **hf-xet download path performs no content verification** of
fetched bytes (only the upload path hashes) — it trusts TLS + server-side
addressing. So there is no client behavior to mirror and no anchor on the wire.
An inline or offline "fsck against the published hash" cannot exist without a
server-provided salt or shard-sourced chunk hashes (neither is on the download
path). This closes the long-deferred "MerkleHash verification" item as *not
achievable via the client API*, not "todo later."

## What the tool IS good for

- **Version-parity / drift detection** (`--parity`): a CI gate proving the
  shim's chunker still matches the fleet's `hf-xet`. Load-bearing whenever the
  pin moves.
- **Chunk-level dedup analysis** (`--dedup A B`): measure the shared chunks /
  bytes between two files or revisions using the real (unsalted) chunk hashes —
  the property the Tier 2 cache monetizes, now measurable directly.
- The on-disk integrity the cache actually relies on stays where it was:
  Tier 2 **per-range self-consistency** (catches cache bit-rot) + the
  smoke/integration **round-trip byte-identity** tests (direct == through-shim).

## API

```python
import xet_verify
xet_verify.file_hash_hex(data: bytes) -> str          # UNSALTED file hash (== hf_xet.hash_files); NOT X-Xet-Hash
xet_verify.chunk_hashes(data: bytes) -> list[tuple[int, str]]   # [(len, hex), ...]
xet_verify.__xet_core_tag__      # "v1.6.0"
xet_verify.__hf_xet_version__    # "1.6.0"
```

## Build

Needs Rust ≥ 1.89 (xet-core's transitive deps: icu, redb, konst). `rustup update stable`.

```bash
uvx maturin@1 build --release -m xet_verify/Cargo.toml
# -> xet_verify/target/wheels/xet_verify-0.1.0-cp310-abi3-*.whl
```

The wheel is abi3 (`cp310+`), so one build serves every Python ≥ 3.10.

## Use

```bash
# Offline parity — proves our chunker == the fleet's hf-xet. Run in CI.
uv run --with xet_verify/target/wheels/xet_verify-*.whl m1/fsck.py --parity

# Chunk-level dedup between two files/revisions (via HF_ENDPOINT or HF direct).
uv run --with xet_verify/target/wheels/xet_verify-*.whl \
  m1/fsck.py --dedup org/model@rev1/model.safetensors org/model@rev2/model.safetensors
```

## The pin is load-bearing

The chunker params (gearhash mask, min/target/max chunk size) must match the
client byte-for-byte or every recomputed hash diverges. When the fleet's
`hf-xet` moves, bump the `tag` in `Cargo.toml`, `__hf_xet_version__` in
`src/lib.rs`, **and** the crate `version` (Cargo.toml + pyproject.toml) in
lockstep, rebuild, and re-run `--parity`. Bumping the version matters: `uv`/pip
cache wheels by name+version, so a rebuild that keeps `0.1.0` will be silently
served from the stale cache (use `--reinstall-package xet_verify` otherwise). CI
failing `--parity` means the shim must not rely on xet_verify until realigned.
