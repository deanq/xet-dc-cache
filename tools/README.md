# m1 — offline analysis tools

> The Xet DC-cache **shim** was ported to Go and now lives in [`../shim-go/`](../shim-go/).
> The Python shim (`shim.py` + support modules + their tests) has been removed —
> `shim-go/` is a validated 1:1 parity port. Recover the old Python shim from git
> history if ever needed. This directory now holds only the **offline tools**, which
> Go does not replace (they bind the Rust xet-core chunker via the `xet_verify`
> pyo3 helper).

## Tools

Both are self-contained PEP 723 `uv` scripts (`uv run <script>`).

### `fsck.py` — parity proof + chunk-level dedup diagnostics

Exercises [`../xet_verify/`](../xet_verify/) (the pinned xet-core chunker). It does
**not** verify a download against `X-Xet-Hash` — that is an HMAC-salted, server-side
file hash and is not reproducible offline (see `../docs/xet-cache-findings.md`). What it
does:

```bash
uv run fsck.py --parity        # no network: proves xet_verify.file_hash_hex == hf-xet's own hash
uv run fsck.py --dedup A B      # CDC-chunk two files, report shared-chunk count / bytes
```

`--parity` is the drift gate for a xet-core pin bump: if it fails, the chunker has
diverged from the fleet's `hf-xet` and must not be trusted. Requires the
`xet_verify` wheel:

```bash
uvx maturin@1 build --release -m ../xet_verify/Cargo.toml
uv run --with ../xet_verify/target/wheels/xet_verify-*.whl fsck.py --parity
```

### `dedup_study.py` — cross-revision immutability probe

Enumerates a repo's commits via the HF API and HEADs a target file per commit,
collecting `X-Xet-Hash` to report identical-vs-changed transitions across revisions.
Pure `httpx`; no shim or xet_verify dependency.

```bash
uv run dedup_study.py probe <org/model> <path>
```

## Findings

The empirical results these tools produced are written up in
[`../docs/xet-cache-findings.md`](../docs/xet-cache-findings.md) (why whole-file dedup is the
whole game; why content-address verification isn't achievable via the client API).
