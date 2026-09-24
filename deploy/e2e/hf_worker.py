#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["huggingface_hub[hf_xet]>=0.24"]
# ///
"""Run one HF download in a clean subprocess. huggingface_hub fixes cache/
endpoint paths at import, so every download must be its own process with its own
HF_HOME. Two modes: --file (one file) and --snapshot (repo weights + configs)."""
from __future__ import annotations
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# Alt-format weight exports we never want in a snapshot: a repo like
# SmolLM2-1.7B-Instruct ships ONNX/GGUF variants that balloon a full snapshot to
# ~23 GB, while a typical transformers user pulls only safetensors + the small
# JSON/text companions. Excluding these keeps the snapshot representative AND
# bounded, and still exercises the mixed LFS (safetensors, X-Linked-Size) +
# non-LFS (config.json/tokenizer.json, Content-Length) pass-through this scenario
# guards.
_SNAPSHOT_IGNORE = [
    "onnx/*", "*.onnx", "*.onnx_data",
    "*.gguf", "*.msgpack", "*.h5",
    "coreml/*", "*.mlmodel", "*.tflite",
]


def sha_manifest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".cache" not in p.parts:
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _prep_env(endpoint: str) -> None:
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if endpoint != "DIRECT":
        os.environ["HF_ENDPOINT"] = endpoint


def worker_file(repo: str, rev: str, filename: str, endpoint: str) -> None:
    with tempfile.TemporaryDirectory() as hf_home:
        os.environ["HF_HOME"] = hf_home
        _prep_env(endpoint)
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(repo_id=repo, filename=filename, revision=rev)
        print(hashlib.sha256(Path(path).read_bytes()).hexdigest())


def worker_snapshot(repo: str, rev: str, endpoint: str) -> None:
    with tempfile.TemporaryDirectory() as hf_home:
        os.environ["HF_HOME"] = hf_home
        _prep_env(endpoint)
        from huggingface_hub import snapshot_download
        path = snapshot_download(repo_id=repo, revision=rev,
                                 ignore_patterns=_SNAPSHOT_IGNORE)
        print(json.dumps(sha_manifest(Path(path))))


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "--file":
        worker_file(*sys.argv[2:])
    elif mode == "--snapshot":
        worker_snapshot(*sys.argv[2:])
    else:
        sys.exit(f"unknown mode {mode!r}")
