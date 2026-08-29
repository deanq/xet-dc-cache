# Xet DC-cache — empirical findings (2026-08)

Two questions were investigated with live data and the pinned xet-core client
(`xet_verify/`, hf-xet v1.6.0). Both resolve to **"don't build it"** — recorded
here so they are not re-litigated.

---

## 1. Does Tier 2 (chunk-level coalescing) earn its complexity? — NO, for model serving

Tier 1 keys the cache by `(xorb_hash, byte-range)` with the CDN signature
normalized out. Tier 2 adds a coverage-map that coalesces *overlapping/partial*
ranges of a shared xorb. Tier 2 only pays off when large files change
**partially** (most chunks reused, some new). Measured whether that happens.

### Cross-revision (same file, across a repo's commit history) — `tools/dedup_study.py`

Compared `X-Xet-Hash` (file identity) of `model.safetensors` across every commit
of 6 Xet-backed repos (no downloads — HEAD only):

| Repo | commits | weight-file transitions | left IDENTICAL |
|---|---|---|---|
| unsloth/Llama-3.2-1B-Instruct | 16 | 14 | 100% |
| unsloth/Llama-3.2-3B-Instruct | 16 | 13 | 100% |
| unsloth/Qwen2.5-0.5B-Instruct | 12 | 10 | 100% |
| unsloth/gemma-2-2b-it | 30 | 29 | 100% |
| HuggingFaceTB/SmolLM2-135M-Instruct | 20 | 19 | 100% |
| unsloth/Llama-3.2-1B | 10 | 8 | 100% |

**~90 transitions, zero weight-file changes.** Weight files are effectively
immutable once uploaded; all commit churn is config/tokenizer/README. There is
**no partial-change workload** for Tier 2 to exploit cross-revision.

### Cross-model — `tools/fsck.py --dedup`

- `tokenizer.json`, Llama-3.2-1B **base vs instruct**: byte-**identical** (same
  `X-Xet-Hash`, 17 MB). Cross-model whole-file reuse is real.
- `model.safetensors`, SmolLM2-135M **base vs instruct** (269 MB each):
  **0 shared chunks (0.0%)**. Fine-tuning perturbs every tensor → no chunk
  survives.

### Conclusion

The dedup that exists in the wild is **whole-file identity** — the same file
pulled across workers, across revisions, and across related models (shared
tokenizers/configs). **Tier 1 captures all of it** (the signature-normalized
`(hash, range)` key is the load-bearing trick). Chunk-level partial overlap is
either 100% (identical → Tier 1) or ~0% (full rewrite → nothing to coalesce).

**Ship Tier 1. Do not invest further in Tier 2.** Keep the Tier 2 code as-is
(it's correct and cheap to leave), but it is dead weight for stock vLLM model
pulls. Tier 2 *would* matter for **iterative** artifacts — training checkpoints
across epochs, appended datasets, GGUF requant of one base — where a large file
changes incrementally. If the DC's serverless workload grows to include those,
revisit with the same `--dedup` measurement.

Caveat: sample is unsloth/HF-heavy, representative of the target (stock images
pulling popular models) but not exhaustive.

---

## 2. Can the cache verify served bytes against a content-address? — NO, via the client API

Goal: have the cache reject poisoned/bit-rotted bytes before fan-out to N
workers, by recomputing a cryptographic content-address. `xet_verify/` (pinned
xet-core chunker + hashes) was built to do it. Every candidate anchor is
unreachable on the **download/read path**:

| Anchor | Verdict |
|---|---|
| **`X-Xet-Hash`** (file id from `resolve`) | Server-side **per-repo HMAC** of the file identity. Proven: on identical bytes `hf_xet.hash_files` == `xet_verify.file_hash_hex` (unsalted `file_hash`, salt=0) but **≠** `X-Xet-Hash`, and ≠ sha256. The salt is never sent (xet-read-token = `{casUrl, exp, accessToken}`). Not reproducible. |
| **Xorb hash** (`term.hash`) | Needs whole-xorb bytes incl. the ~42 KB chunk-index footer; the CDN 403s beyond the signed byte range. Unreachable (prior Tier 2 finding). |
| **Range verification hash** (`FileVerificationEntry.range_hash`) | Uses a **public** constant `VERIFICATION_KEY` (in open-source xet-core) — so it *is* reproducible in principle. BUT the anchors are delivered only inside mdb **shards**. Neither v1 nor v2 reconstruction carries them (verified live: no `verification`/`range_hash` field), and read tokens don't grant the file's shard. The hf-xet **download path itself skips range verification** when `verification` is empty — which is the download case. |

**The hf-xet download path performs no content verification at all** — it trusts
TLS + server-side addressing. There is no client behavior to mirror and no
anchor on the wire.

### Conclusion

Content-address verification of downloads is **not achievable** with
client-available data. This is a hard protocol limit, not a "todo." What remains
(and is sufficient in practice):

- **On-disk integrity**: `Tier2Store.verify_self` re-hashes stored ranges with
  BLAKE3 (per-range self-consistency) — catches cache bit-rot.
- **End-to-end integrity**: the smoke/integration tests assert through-shim
  bytes are byte-identical to a direct upstream fetch.

`xet_verify` stays useful for what it *can* do: `--parity` (CI drift gate that
the shim's chunker still matches the fleet's hf-xet) and `--dedup` (the dedup
measurement above).
