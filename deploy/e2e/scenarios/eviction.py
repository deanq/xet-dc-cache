from __future__ import annotations
from harness import Report, bring_up, delta, download, metrics, reset_cache

EP = "http://127.0.0.1:8004"


def run(rep: Report) -> None:
    print("== scenario #4: byte-budget eviction (XORB_CACHE_MAX_GIB=1) ==")
    truth = download("DIRECT")
    bring_up("node-evict")
    reset_cache("node-evict")

    # First pull fills a cache smaller than the file -> older xorbs get evicted.
    sha1 = download(EP)
    rep.check("s#4 first pull byte-identical", sha1 == truth)

    # Re-pull: evicted xorbs must be re-fetched from WAN (misses > 0). A fully
    # warm cache would show zero misses; misses prove eviction actually happened.
    before = metrics(8004)
    sha2 = download(EP)
    after = metrics(8004)
    d_miss = delta(before, after, "misses")
    rep.check("s#4 re-pull re-fetched evicted xorbs (eviction happened)",
              d_miss > 0, f"misses +{d_miss}")
    rep.check("s#4 bytes still identical after eviction (no false HIT)",
              sha2 == truth)
    print()
