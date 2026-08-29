//! Thin pyo3 wrapper over xet-core's content-defined chunker + file-hash
//! aggregation, pinned to hf-xet v1.6.0 (xet-core tag `v1.6.0`). Nothing here is
//! hand-rolled: the chunker (gearhash CDC), per-chunk BLAKE3 (`MerkleHash`), and
//! the file-hash tree aggregation all come from xet-core (fail-secure — a
//! drifted chunker would silently disagree with the fleet).
//!
//! IMPORTANT — what `file_hash` here is NOT: it is the client's *unsalted* file
//! hash (identical to `hf_xet.hash_files`, proven by `m1/fsck.py --parity`). It
//! is **not** the `X-Xet-Hash` `file_id` from `resolve`: that is an HMAC-*salted*
//! variant whose salt lives server-side and is never sent to the client (the
//! xet-read-token carries only casUrl/exp/accessToken). So these hashes cannot
//! verify a download against `X-Xet-Hash` — there is no reproducible anchor on
//! the wire (see `xet_verify/README.md`). Their real use is chunk-level analysis
//! the shim controls both ends of: dedup validation and version-parity.
//!
//! Two entry points, mirroring `xet_data/examples/hash`:
//!   * `chunk_hashes(bytes) -> [(len, hex)]` — CDC chunk boundaries + hashes.
//!   * `file_hash(bytes) -> hex`             — unsalted aggregated file hash.

use pyo3::prelude::*;
use xet_core_structures::merklehash::{file_hash, MerkleHash};
use xet_data::deduplication::Chunker;

/// CDC-chunk `data` with the client's default params; return (hash, len) per
/// chunk. `next_block(.., is_final=true)` emits every chunk including the final
/// partial one and resets the chunker, so a single call is complete.
fn chunk(data: &[u8]) -> Vec<(MerkleHash, u64)> {
    Chunker::default()
        .next_block(data, true)
        .into_iter()
        .map(|c| (c.hash, c.data.len() as u64))
        .collect()
}

/// Per-chunk boundaries + content hashes: `[(uncompressed_len, blake3_hex), ...]`.
#[pyfunction]
fn chunk_hashes(data: &[u8]) -> Vec<(u64, String)> {
    chunk(data)
        .into_iter()
        .map(|(hash, len)| (len, hash.hex()))
        .collect()
}

/// The client's unsalted file hash as a 64-char hex string. Equals
/// `hf_xet.hash_files`; NOT `X-Xet-Hash` (which is salted server-side).
#[pyfunction]
fn file_hash_hex(data: &[u8]) -> String {
    file_hash(&chunk(data)).hex()
}

#[pymodule]
fn xet_verify(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(chunk_hashes, m)?)?;
    m.add_function(wrap_pyfunction!(file_hash_hex, m)?)?;
    // The pin, discoverable at runtime so ops can assert it matches the fleet.
    m.add("__xet_core_tag__", "v1.6.0")?;
    m.add("__hf_xet_version__", "1.6.0")?;
    Ok(())
}
