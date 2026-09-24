from __future__ import annotations
import os, time
from pathlib import Path

def time_one(download_fn, model: str) -> dict:
    start = time.monotonic()
    try:
        nbytes, first_byte_s = download_fn(model)
        return {"model": model, "bytes": int(nbytes),
                "first_byte_ms": round(first_byte_s * 1000),
                "wall_seconds": round(time.monotonic() - start, 3),
                "ok": True, "error": None}
    except Exception as e:  # errors are values — report, don't crash the worker
        return {"model": model, "bytes": 0, "first_byte_ms": None,
                "wall_seconds": round(time.monotonic() - start, 3),
                "ok": False, "error": str(e)}

def run_download(payload: dict, download_fn, *,
                 cold_first_invocation: bool = False, dep_upgrade_ms: int = 0) -> dict:
    return {"worker_id": os.environ.get("RUNPOD_POD_ID", "unknown"),
            "cold_first_invocation": cold_first_invocation,
            "dep_upgrade_ms": dep_upgrade_ms,
            "results": [time_one(download_fn, m) for m in payload.get("models", [])]}

def hf_download(model: str):
    """Real download through the cache (HF_ENDPOINT set in the Flash endpoint env)."""
    from huggingface_hub import snapshot_download
    repo, _, rev = model.partition("@")
    t0 = time.monotonic()
    path = snapshot_download(repo_id=repo, revision=rev or None)
    first_byte_s = time.monotonic() - t0  # coarse: first file resolved
    total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    return (total, first_byte_s)
