from __future__ import annotations

from harness import (
    COMPOSE,
    REV,
    Report,
    bring_up,
    delta,
    download,
    metrics,
    rate,
    reset_cache,
    sh,
    timed_download,
)
from harness import SMOL_REPO as REPO
from harness import FILE


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


def run(rep: Report) -> None:
    """Cross-DC peering: WAN fallback, peer warm hit, resilience, cross-revision
    dedup -- against real shim containers node-a/b/c and the live HF CDN.

    Owns the cold cluster: brings up and resets node-a/b/c at the start, since it
    stops node-b/c mid-run (scenario 3). The next scenario's own bring_up()
    restarts whatever it needs.
    """
    bring_up("node-a", "node-b", "node-c")
    for node in ("node-a", "node-b", "node-c"):
        reset_cache(node)

    print(f"model: {REPO}@{REV} :: {FILE}")

    # Ground truth: a direct (no-shim) download to compare bytes against.
    print("== ground truth (direct download) ==")
    truth = download("DIRECT")
    print(f"  sha256 = {truth[:16]}...\n")

    # Scenario 1: WAN fallback through node-c (cluster is cold everywhere).
    print("== scenario 1: WAN fallback (node-c, all peers cold) ==")
    before = metrics(8003)
    sha, dt_wan = timed_download("http://127.0.0.1:8003")
    after = metrics(8003)
    d_wan = delta(before, after, "wan_bytes")
    size = d_wan  # full-file transfer size, reused for throughput below
    d_peer_miss = delta(before, after, "peer_misses")
    d_peer_bytes = delta(before, after, "peer_bytes")
    rep.check("s1 bytes identical", sha == truth)
    rep.check("s1 pulled from WAN", d_wan > 0, f"wan_bytes +{d_wan}")
    rep.check("s1 peers probed and missed", d_peer_miss > 0, f"peer_misses +{d_peer_miss}")
    rep.check("s1 nothing came from a peer", d_peer_bytes == 0, f"peer_bytes +{d_peer_bytes}")
    print(f"  time: {rate(size, dt_wan)}")
    print()

    # Scenario 2: peer warm hit through node-a (node-c is now warm).
    print("== scenario 2: peer warm hit (node-a, node-c warm) ==")
    before = metrics(8001)
    sha, dt_peer = timed_download("http://127.0.0.1:8001")
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
    print(f"  time: {rate(size, dt_peer)}")
    print()

    # Scenario 3: resilience -- peers stopped, node-a cold, must fall to WAN.
    print("== scenario 3: resilience (peers down, node-a cold) ==")
    sh(*COMPOSE, "stop", "node-b", "node-c")
    reset_cache("node-a")
    before = metrics(8001)
    sha = download("http://127.0.0.1:8001")
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
    dt_cache = None
    other = older_identical_rev()
    if other is None:
        rep.check("s4 has an earlier revision to test", False,
                  f"{REPO} has only one commit")
    else:
        before = metrics(8001)
        sha, dt_cache = timed_download("http://127.0.0.1:8001", rev=other)
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
        print(f"  time: {rate(size, dt_cache)}")
    print()

    # Timing summary: WAN cold pull vs LAN peer vs local cache, same file.
    gib = size / 2**30
    print(f"== timing ({gib:.2f} GiB file, same bytes each pull) ==")
    print(f"  WAN cold pull (s1):        {rate(size, dt_wan)}")
    print(f"  peer warm hit (s2):        {rate(size, dt_peer)}"
          f"   -> {dt_wan / max(dt_peer, 1e-6):.1f}x faster than WAN")
    if dt_cache is not None:
        print(f"  local cache hit (s4):      {rate(size, dt_cache)}"
              f"   -> {dt_wan / max(dt_cache, 1e-6):.1f}x faster than WAN")
    print("  (local loopback overstates LAN speed vs a real DC backbone, but"
          " the direction — cache/peer >> WAN — is the point.)")
    print()
