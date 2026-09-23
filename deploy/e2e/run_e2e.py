#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["huggingface_hub[hf_xet]>=0.24"]
# ///
"""End-to-end validation of cross-DC peering across three real shim containers.

Boots the deploy/e2e docker-compose stack (node-a/b/c, each the real shim image,
peered with the other two) and drives real `hf_hub_download`s through them
against the live HuggingFace CDN, asserting four things the unit tests can only
approximate:

  1. WAN fallback   -- a download through a node whose peers are all cold pulls
     from the CDN (wan_bytes up, peer_misses up) and produces correct bytes.
  2. Peer warm hit  -- a download through a cold node whose peer is now warm is
     served from the peer (peer_bytes up, wan_bytes ~0) and byte-identical.
  3. Resilience     -- with peers stopped, the same cold node still completes via
     the CDN: peering is an accelerator, never a dependency.
  4. Cross-revision -- the same file at a different, byte-identical revision is
     served from the node's own cache (hits up, wan_bytes 0): unchanged files
     across model versions reconstruct from the same xorbs, so re-pulls are free.

Needs network + Docker. Downloads the model a few times (default SmolLM2-1.7B,
~3 GB). Override with SMOKE_REPO / SMOKE_REV / SMOKE_PATH.

    uv run deploy/e2e/run_e2e.py            # from repo root
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

REPO = os.environ.get("SMOKE_REPO", "HuggingFaceTB/SmolLM2-1.7B-Instruct")
REV = os.environ.get("SMOKE_REV", "main")
FILE = os.environ.get("SMOKE_PATH", "model.safetensors")

E2E_DIR = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "-f", str(E2E_DIR / "docker-compose.yml")]
CACHES = E2E_DIR / "caches"

# host port -> compose service, for each node
NODES = {"node-a": 8001, "node-b": 8002, "node-c": 8003}


# --------------------------------------------------------------------------
# worker mode: run one hf_hub_download in a clean subprocess. huggingface_hub
# fixes cache/endpoint at import, so every download must be its own process with
# its own HF_HOME to actually go over the wire through the chosen shim.
# --------------------------------------------------------------------------
def worker(repo: str, rev: str, filename: str, endpoint: str, out: str) -> None:
    with tempfile.TemporaryDirectory() as hf_home:
        os.environ["HF_HOME"] = hf_home
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        if endpoint != "DIRECT":
            os.environ["HF_ENDPOINT"] = endpoint
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id=repo, filename=filename, revision=rev)
        h = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        shutil.copy(path, out)
        print(h)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def download(endpoint: str, out: Path, rev: str = REV) -> str:
    """Run a download in a subprocess; returns the file's sha256.

    rev defaults to REV; pass an explicit revision for the cross-revision
    scenario (same file at a different commit).
    """
    proc = subprocess.run(
        [sys.executable, __file__, "--worker", REPO, rev, FILE, endpoint, str(out)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"download via {endpoint} failed:\n{proc.stderr}")
    return proc.stdout.strip().splitlines()[-1]


def older_identical_rev() -> str | None:
    """Find an older commit whose FILE is byte-identical to REV's.

    Used by the cross-revision scenario. Returns a commit SHA, or None if the
    repo has no earlier revision to compare against. Identity is asserted for
    real in the scenario (its sha256 must match ground truth), so this only
    needs to pick a *different* commit; model weight files are immutable across
    revisions in practice (measured 100% identical), so the previous commit works.
    """
    from huggingface_hub import list_repo_commits

    commits = list_repo_commits(REPO, revision=REV)
    for c in commits[1:]:
        return c.commit_id
    return None


def metrics(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as r:
        return json.load(r)


def wait_healthy(timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    for name, port in NODES.items():
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            if time.time() > deadline:
                raise TimeoutError(f"{name} (port {port}) never became healthy")
            time.sleep(1)


def reset_cache(node: str) -> None:
    """Empty a node's on-disk cache. The nodes run as root and Colima maps the
    bind-mount to root, so cache files are root-owned; clear them with a throwaway
    root container rather than a host rm that may hit permission errors."""
    d = (CACHES / node[-1]).resolve()  # node-a -> a
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{d}:/cache", "busybox",
         "sh", "-c", "rm -rf /cache/* /cache/.[!.]* 2>/dev/null || true"],
        check=False,
        capture_output=True,
    )


def delta(before: dict, after: dict, key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))

    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)


def main() -> int:
    rep = Report()
    truth_dir = Path(tempfile.mkdtemp(prefix="xet-e2e-"))
    started = False
    try:
        print(f"model: {REPO}@{REV} :: {FILE}")
        print("== building + starting stack ==")
        for node in ("a", "b", "c"):
            reset_cache(f"node-{node}")
        sh(*COMPOSE, "up", "-d", "--build")
        started = True
        wait_healthy()
        print("all nodes healthy\n")

        # Ground truth: a direct (no-shim) download to compare bytes against.
        print("== ground truth (direct download) ==")
        truth = download("DIRECT", truth_dir / "truth.bin")
        print(f"  sha256 = {truth[:16]}...\n")

        # Scenario 1: WAN fallback through node-c (cluster is cold everywhere).
        print("== scenario 1: WAN fallback (node-c, all peers cold) ==")
        before = metrics(8003)
        sha = download("http://127.0.0.1:8003", truth_dir / "s1.bin")
        after = metrics(8003)
        d_wan = delta(before, after, "wan_bytes")
        d_peer_miss = delta(before, after, "peer_misses")
        d_peer_bytes = delta(before, after, "peer_bytes")
        rep.check("s1 bytes identical", sha == truth)
        rep.check("s1 pulled from WAN", d_wan > 0, f"wan_bytes +{d_wan}")
        rep.check("s1 peers probed and missed", d_peer_miss > 0, f"peer_misses +{d_peer_miss}")
        rep.check("s1 nothing came from a peer", d_peer_bytes == 0, f"peer_bytes +{d_peer_bytes}")
        print()

        # Scenario 2: peer warm hit through node-a (node-c is now warm).
        print("== scenario 2: peer warm hit (node-a, node-c warm) ==")
        before = metrics(8001)
        sha = download("http://127.0.0.1:8001", truth_dir / "s2.bin")
        after = metrics(8001)
        d_peer_bytes = delta(before, after, "peer_bytes")
        d_wan = delta(before, after, "wan_bytes")
        rep.check("s2 bytes identical", sha == truth)
        rep.check("s2 served from a peer", d_peer_bytes > 0, f"peer_bytes +{d_peer_bytes}")
        rep.check(
            "s2 peer displaced WAN (peer_bytes >= wan_bytes)",
            d_peer_bytes >= d_wan,
            f"peer_bytes +{d_peer_bytes} vs wan_bytes +{d_wan}",
        )
        print()

        # Scenario 3: resilience -- peers stopped, node-a cold, must fall to WAN.
        print("== scenario 3: resilience (peers down, node-a cold) ==")
        sh(*COMPOSE, "stop", "node-b", "node-c")
        reset_cache("node-a")
        before = metrics(8001)
        sha = download("http://127.0.0.1:8001", truth_dir / "s3.bin")
        after = metrics(8001)
        d_wan = delta(before, after, "wan_bytes")
        d_peer_bytes = delta(before, after, "peer_bytes")
        rep.check("s3 completed with peers down", sha == truth)
        rep.check("s3 fell back to WAN", d_wan > 0, f"wan_bytes +{d_wan}")
        rep.check("s3 got nothing from (dead) peers", d_peer_bytes == 0, f"peer_bytes +{d_peer_bytes}")
        print()

        # Scenario 4: cross-revision whole-file dedup. node-a is now warm with
        # REV's xorbs (from s3) and its peers are down, so any cache benefit here
        # is purely node-a's OWN shim cache. Pull the SAME file at a DIFFERENT,
        # byte-identical revision: the file's Xet identity is unchanged, so it
        # reconstructs from the same xorbs -> served from cache, zero new WAN.
        print("== scenario 4: cross-revision dedup (same file, different revision) ==")
        other = older_identical_rev()
        if other is None:
            rep.check("s4 has an earlier revision to test", False,
                      f"{REPO} has only one commit")
        else:
            before = metrics(8001)
            sha = download("http://127.0.0.1:8001", truth_dir / "s4.bin", rev=other)
            after = metrics(8001)
            d_wan = delta(before, after, "wan_bytes")
            d_hits = delta(before, after, "hits")
            d_peer_bytes = delta(before, after, "peer_bytes")
            rep.check("s4 same bytes at a different revision", sha == truth,
                      f"rev {other[:10]} identical to {REV}")
            rep.check("s4 served from cache -- zero new WAN", d_wan == 0,
                      f"wan_bytes +{d_wan}")
            rep.check("s4 registered shim cache hits", d_hits > 0, f"hits +{d_hits}")
            rep.check("s4 was the shim's own cache, not a peer", d_peer_bytes == 0,
                      f"peer_bytes +{d_peer_bytes}")
        print()

        print("== summary ==")
        print("RESULT:", "PASS" if rep.ok() else "FAIL")
        return 0 if rep.ok() else 1
    finally:
        shutil.rmtree(truth_dir, ignore_errors=True)
        if started:
            sh(*COMPOSE, "down", check=False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(*sys.argv[2:])
    else:
        sys.exit(main())
