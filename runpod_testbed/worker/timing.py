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


HF_HOME_FALLBACK = "/root/.cache/huggingface"


def volumecache_download(model: str, *, download_fn=None, cache_factory=None,
                         hf_home: str | None = None):
    """VolumeCache path: hydrate() restores the mirror from /runpod-volume, the HF
    download is then a local cache hit (or a real WAN pull on the populate run),
    and a SYNCHRONOUS sync() writes new files back so the next job can hydrate
    them. Explicit calls rather than `with VolumeCache(...)`: __exit__ syncs on a
    background daemon thread, which would return the populate job before the
    mirror is written and let the first warm job race it.
    """
    if cache_factory is None:
        from runpod.serverless import VolumeCache as cache_factory
    download_fn = download_fn or hf_download
    hf_home = hf_home or os.environ.get("HF_HOME", HF_HOME_FALLBACK)
    vc = cache_factory(dirs=[hf_home], best_effort=True)   # namespace defaults to RUNPOD_ENDPOINT_ID
    t0 = time.monotonic()
    vc.hydrate()
    hydrate_s = time.monotonic() - t0
    t1 = time.monotonic()
    nbytes, first_byte_s, _ = _unpack(download_fn(model))
    download_s = time.monotonic() - t1
    vc.sync(background=False)
    return (nbytes, first_byte_s,
            {"hydrate_s": round(hydrate_s, 3), "download_s": round(download_s, 3)})


MODELSTORE_ROOT = "/runpod-volume/huggingface-cache/hub"
_DEFAULT_REV = "main"


def _find_model_dir_case_insensitive(root: str, org: str, name: str) -> Path | None:
    """The staged dir keeps HF's ORIGINAL case (models--Org--Name); our model id
    may be lowercased (Model Store's `modelReferences` normalizes to lowercase),
    so match by enumerating root rather than assuming exact case."""
    target = f"models--{org}--{name}".lower()
    root_path = Path(root)
    if not root_path.is_dir():
        return None
    return next((p for p in root_path.iterdir() if p.is_dir() and p.name.lower() == target), None)


def modelstore_snapshot_dir(model: str, root: str) -> Path:
    """Runpod cached-model layout mirrors HF_HOME/hub: models--{org}--{name}/snapshots/{hash}."""
    repo, _, rev = model.partition("@")
    org, _, name = repo.partition("/")
    base = _find_model_dir_case_insensitive(root, org, name)
    if base is None:
        raise FileNotFoundError(
            f"model {model}: no models--{org}--{name} dir (case-insensitive) under {root} "
            f"— is the cached model declared on this endpoint?")
    ref = base / "refs" / (rev or _DEFAULT_REV)
    if ref.is_file():
        return base / "snapshots" / ref.read_text().strip()
    snapshots = base / "snapshots"
    dirs = sorted(p for p in snapshots.iterdir() if p.is_dir()) if snapshots.is_dir() else []
    if len(dirs) == 1:
        return dirs[0]
    raise FileNotFoundError(
        f"model {model}: expected refs/{rev or _DEFAULT_REV} or exactly one snapshot under "
        f"{snapshots} (found {len(dirs)}) — is the cached model declared on this "
        f"endpoint and finished staging?")


def modelstore_local_read(model: str, root: str | None = None):
    """Model Store path: the platform staged the weights before the handler ran;
    assert they are present and time the local walk. No HF download."""
    expected = os.environ.get("MODEL")
    if expected and expected != model:
        raise ValueError(f"job asked for {model} but this endpoint caches {expected}")
    root = root or os.environ.get("MODELSTORE_ROOT", MODELSTORE_ROOT)
    t0 = time.monotonic()
    snap = modelstore_snapshot_dir(model, root)
    files = [f for f in snap.rglob("*") if f.is_file()]
    if not files:
        raise FileNotFoundError(f"model {model}: snapshot {snap} has no files")
    total = sum(f.stat().st_size for f in files)
    local_read_s = time.monotonic() - t0
    return (total, local_read_s, {"local_read_s": round(local_read_s, 3)})


def schema_phase(job_phase: str) -> str:
    return _PHASE_ALIAS.get(job_phase, job_phase)


def make_timing_row(mechanism: str, job: dict, result: dict, wall_seconds: float, *,
                    delay_seconds: float | None = None, exec_seconds: float | None = None) -> dict:
    """The spec's shared timing schema — one row per driver job.

    `delay_seconds`/`exec_seconds` are the platform's own placement/staging wait
    and execution time (Runpod job status `delayTime`/`executionTime`, read by
    `drive/run.py`). They are optional and default to None so mechanisms/rows
    that don't carry them (or pre-existing jobs files) stay valid. They let
    `harvest/report.py` build a like-for-like "steady-state" view alongside the
    end-to-end `wall_seconds` "cold-start latency" view — this matters most for
    Model Store, whose acquisition happens outside the handler as unbilled
    platform staging that inflates `wall_seconds` without inflating actual work.
    """
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
        "delay_seconds": delay_seconds,
        "exec_seconds": exec_seconds,
    }
