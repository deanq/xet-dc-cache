#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["huggingface_hub[hf_xet]>=0.24"]
# ///
"""External black-box acceptance test for the Go shim binary.

Unlike m1/integration_test.py (which boots the Python app in-process via
`import shim; uvicorn.Config(shim.app, ...)`), this drives the *compiled Go
binary* as a real subprocess over HTTP, exactly as a real client would: build
`xetcache`, launch it with CACHE_DIR/PUBLIC_BASE/PORT env vars, point
`hf_hub_download` at it via HF_ENDPOINT, and verify:

  1. a through-shim download completes
  2. the Go on-disk xorb cache actually populated (traffic went through it)
  3. a direct (no-shim) download of the same file is byte-identical (sha256)
  4. a second through-shim download increases /metrics hits + wan_bytes_saved
  5. after killing and restarting the binary on the SAME cache dir, a fresh
     download still produces hits -- the on-disk cache survives restarts and
     the LRU is correctly seeded from disk.

Needs network. Downloads ~269 MB three times (steps 3, 6, 7). Uses a scratch
temp CACHE_DIR only -- never touches a real cache directory.

    uv run acceptance.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("SMOKE_REPO", "HuggingFaceTB/SmolLM2-135M-Instruct")
REV = os.environ.get("SMOKE_REV", "main")
FILE = os.environ.get("SMOKE_PATH", "model.safetensors")

SHIM_GO_DIR = Path(__file__).resolve().parent
BINARY = SHIM_GO_DIR / "xetcache"
PORT = "8000"
BASE = f"http://127.0.0.1:{PORT}"

PASS = "PASS"
FAIL = "FAIL"


class Step:
    """One recorded assertion for the final summary."""

    def __init__(self, name: str):
        self.name = name
        self.status = PASS
        self.detail = ""

    def fail(self, detail: str) -> None:
        self.status = FAIL
        self.detail = detail
        raise AssertionError(f"{self.name}: {detail}")


def build_binary() -> None:
    print(f"building {BINARY} ...")
    subprocess.run(["go", "build", "-o", "xetcache", "."], cwd=SHIM_GO_DIR, check=True)


def start_shim(cache_dir: Path) -> subprocess.Popen:
    """Launch the Go binary as a real subprocess and wait for /healthz."""
    env = dict(os.environ)
    env.update(
        {
            "CACHE_DIR": str(cache_dir),
            "PUBLIC_BASE": BASE,
            "PORT": PORT,
            "XORB_CACHE_MAX_GIB": "0",
        }
    )
    proc = subprocess.Popen(
        [str(BINARY)],
        cwd=SHIM_GO_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"shim exited early (code={proc.returncode}):\n{out}")
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=1) as resp:
                if resp.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.2)
    stop_shim(proc)
    raise RuntimeError("shim did not become healthy within 20s")


def stop_shim(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_DOWNLOAD_HELPER = """
import os, sys
from huggingface_hub import hf_hub_download

path = hf_hub_download(
    repo_id={repo!r}, filename={filename!r}, revision={revision!r},
    token=os.environ.get("HF_TOKEN"),
)
print(path)
"""


def download(endpoint: str | None, cache_root: Path) -> Path:
    """hf_hub_download into an isolated client cache; endpoint=None -> real HF.

    Runs in a *fresh subprocess* rather than in-process: huggingface_hub
    computes module-level constants like HF_XET_CACHE once at import time,
    so reusing the already-imported module across multiple downloads in the
    same process would silently keep serving from the FIRST download's xet
    chunk cache regardless of later HF_XET_CACHE/HF_HOME overrides -- which
    would make every download after the first look like a "hit" even if the
    shim were never contacted. A subprocess per download guarantees the
    isolated cache dirs are actually honored.
    """
    env = dict(os.environ)
    env.update(
        {
            "HF_HOME": str(cache_root / "home"),
            "HF_XET_CACHE": str(cache_root / "xet"),
            "HF_HUB_DISABLE_XET": "0",
        }
    )
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
    else:
        env.pop("HF_ENDPOINT", None)

    code = _DOWNLOAD_HELPER.format(repo=REPO, filename=FILE, revision=REV)
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"download subprocess failed (endpoint={endpoint}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    path_line = result.stdout.strip().splitlines()[-1]
    return Path(path_line)


def fetch_metrics() -> dict:
    with urllib.request.urlopen(f"{BASE}/metrics", timeout=5) as resp:
        return json.loads(resp.read())


def cached_xorb_files(cache_dir: Path) -> list[Path]:
    """Regular files directly under cache_dir, excluding the manifests subdir."""
    if not cache_dir.is_dir():
        return []
    return [p for p in cache_dir.iterdir() if p.is_file()]


def main() -> int:
    steps: list[Step] = []
    proc: subprocess.Popen | None = None
    results: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="xetcache-acceptance-") as tmp:
        root = Path(tmp)
        go_cache_dir = root / "gocache"

        try:
            build_binary()

            # --- 1/2: start shim, wait for health ---
            step = Step("start shim + healthz")
            steps.append(step)
            proc = start_shim(go_cache_dir)
            print(f"shim healthy on {BASE}, CACHE_DIR={go_cache_dir}")

            # --- 3: download through shim ---
            step = Step("download through shim")
            steps.append(step)
            t0 = time.time()
            via_shim = download(BASE, root / "client-a")
            via_shim_sha = sha256_of(via_shim)
            via_shim_size = via_shim.stat().st_size
            elapsed = time.time() - t0
            print(
                f"  via-shim: {via_shim_size:,} bytes  sha={via_shim_sha[:16]}...  {elapsed:.1f}s"
            )
            results["via_shim_sha256"] = via_shim_sha
            results["via_shim_size"] = via_shim_size

            # --- 4: cache populated ---
            step = Step("Go xorb cache populated")
            steps.append(step)
            cached = cached_xorb_files(go_cache_dir)
            if not cached:
                step.fail(f"no xorb files found under {go_cache_dir} (excl. manifests)")
            print(f"  cache populated: {len(cached)} xorb file(s) under {go_cache_dir}")
            results["cache_files_after_first_download"] = len(cached)

            # --- 5: direct download, byte-identical ---
            step = Step("direct download byte-identical to via-shim")
            steps.append(step)
            direct = download(None, root / "client-b")
            direct_sha = sha256_of(direct)
            print(f"  direct:   sha={direct_sha[:16]}...")
            results["direct_sha256"] = direct_sha
            if via_shim_sha != direct_sha:
                step.fail(f"sha256 mismatch: via_shim={via_shim_sha} direct={direct_sha}")
            print("  sha256 match -> shim served byte-identical content")

            # --- 6: metrics show hits + wan_bytes_saved after 2nd download ---
            step = Step("metrics show hits + wan_bytes_saved after 2nd through-shim download")
            steps.append(step)
            metrics_before = fetch_metrics()
            print(f"  metrics before 2nd download: {metrics_before}")
            download(BASE, root / "client-c")
            metrics_after = fetch_metrics()
            print(f"  metrics after 2nd download:  {metrics_after}")
            results["metrics_before_2nd"] = metrics_before
            results["metrics_after_2nd"] = metrics_after
            hits_before = metrics_before.get("hits", 0)
            hits_after = metrics_after.get("hits", 0)
            wan_saved_after = metrics_after.get("wan_bytes_saved", 0)
            if not (hits_after > hits_before):
                step.fail(f"hits did not increase: before={hits_before} after={hits_after}")
            if not (wan_saved_after > 0):
                step.fail(f"wan_bytes_saved not positive: {wan_saved_after}")
            print(
                f"  hits {hits_before} -> {hits_after}, "
                f"wan_bytes_saved={wan_saved_after:,}"
            )

            # --- 7: restart reuse ---
            step = Step("restart reuse: on-disk cache survives restart, LRU reseeded")
            steps.append(step)
            stop_shim(proc)
            proc = None
            proc = start_shim(go_cache_dir)
            print("  shim restarted on same CACHE_DIR")
            metrics_after_restart_baseline = fetch_metrics()
            download(BASE, root / "client-d")
            metrics_after_restart = fetch_metrics()
            print(f"  metrics after restart download: {metrics_after_restart}")
            results["metrics_after_restart"] = metrics_after_restart
            restart_hits = metrics_after_restart.get("hits", 0)
            baseline_hits = metrics_after_restart_baseline.get("hits", 0)
            if not (restart_hits > baseline_hits):
                step.fail(
                    "no hits after restart: cache was not reused "
                    f"(baseline={baseline_hits} after={restart_hits})"
                )
            print(f"  restart hits {baseline_hits} -> {restart_hits} -> on-disk cache reused")

        except AssertionError:
            pass  # captured on the Step; fall through to summary
        finally:
            stop_shim(proc)

    print("\n" + "=" * 70)
    print("ACCEPTANCE TEST SUMMARY")
    print("=" * 70)
    for step in steps:
        marker = "PASS" if step.status == PASS else "FAIL"
        line = f"[{marker}] {step.name}"
        if step.detail:
            line += f" -- {step.detail}"
        print(line)

    if "via_shim_sha256" in results:
        print(f"\nvia_shim sha256: {results['via_shim_sha256']}")
    if "direct_sha256" in results:
        print(f"direct   sha256: {results['direct_sha256']}")

    overall = PASS if all(s.status == PASS for s in steps) and steps else FAIL
    print(f"\nOVERALL: {overall}")
    return 0 if overall == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
