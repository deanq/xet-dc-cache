from __future__ import annotations
import os, time
from pathlib import Path

EMPTY_BREAKDOWN = {"download_s": None, "hydrate_s": None, "local_read_s": None}
SCHEMA_PHASES = ("baseline", "populate", "warm")
_PHASE_ALIAS = {"cold": "populate"}   # shim driver still says "cold" (tests pin it)


def _unpack(ret) -> tuple[int, float, dict]:
    """download_fn returns (bytes, first_byte_s) or (bytes, first_byte_s, breakdown)."""
    if len(ret) == 2:
        nbytes, first_byte_s = ret
        return nbytes, first_byte_s, dict(EMPTY_BREAKDOWN)
    nbytes, first_byte_s, breakdown = ret
    return nbytes, first_byte_s, {**EMPTY_BREAKDOWN, **breakdown}


def time_one(download_fn, model: str) -> dict:
    start = time.monotonic()
    try:
        nbytes, first_byte_s, breakdown = _unpack(download_fn(model))
        return {"model": model, "bytes": int(nbytes),
                "first_byte_ms": round(first_byte_s * 1000),
                "wall_seconds": round(time.monotonic() - start, 3),
                "breakdown": breakdown, "ok": True, "error": None}
    except Exception as e:  # errors are values — report, don't crash the worker
        return {"model": model, "bytes": 0, "first_byte_ms": None,
                "wall_seconds": round(time.monotonic() - start, 3),
                "breakdown": dict(EMPTY_BREAKDOWN), "ok": False, "error": str(e)}


def run_download(payload: dict, download_fn, *,
                 cold_first_invocation: bool = False, dep_upgrade_ms: int = 0) -> dict:
    return {"worker_id": os.environ.get("RUNPOD_POD_ID", "unknown"),
            "cold_first_invocation": cold_first_invocation,
            "dep_upgrade_ms": dep_upgrade_ms,
            "results": [time_one(download_fn, m) for m in payload.get("models", [])]}


def hf_download(model: str):
    """Real download (through the shim when HF_ENDPOINT is set; straight from HF otherwise)."""
    from huggingface_hub import snapshot_download
    repo, _, rev = model.partition("@")
    t0 = time.monotonic()
    path = snapshot_download(repo_id=repo, revision=rev or None)
    download_s = time.monotonic() - t0
    total = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    return (total, download_s, {"download_s": round(download_s, 3)})  # first-byte stays coarse


def schema_phase(job_phase: str) -> str:
    return _PHASE_ALIAS.get(job_phase, job_phase)


def make_timing_row(mechanism: str, job: dict, result: dict, wall_seconds: float) -> dict:
    """The spec's shared timing schema — one row per driver job."""
    per_model = (result.get("results") or [{}])[0]
    return {
        "mechanism": mechanism,
        "phase": schema_phase(job["phase"]),
        "model": job["model"],
        "wall_seconds": float(wall_seconds),
        "bytes": int(per_model.get("bytes", 0)),
        "breakdown": {**EMPTY_BREAKDOWN, **per_model.get("breakdown", {})},
        "worker_cold": bool(result.get("cold_first_invocation", False)),
        "ok": bool(per_model.get("ok", False)),
    }
