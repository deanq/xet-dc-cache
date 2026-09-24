from __future__ import annotations
from harness import QWEN_REPO, Report, bring_up, delta, metrics, reset_cache, snapshot

REV = "main"


def run(rep: Report) -> None:
    print("== scenario #2: sharded model (Qwen2.5-3B, 2 shards) ==")
    truth = snapshot("DIRECT", QWEN_REPO, REV)
    bring_up("node-a", "node-b", "node-c")
    for n in ("node-a", "node-b", "node-c"):
        reset_cache(n)

    # Cold WAN pull through node-c (whole cluster cold): both shards + index.
    before = metrics(8003)
    cold = snapshot("http://127.0.0.1:8003", QWEN_REPO, REV)
    after = metrics(8003)
    rep.check("s#2 cold manifest byte-identical", cold == truth, f"{len(cold)} files")
    rep.check("s#2 cold pulled shard bytes from WAN",
              delta(before, after, "wan_bytes") > 0,
              f"wan_bytes +{delta(before, after, 'wan_bytes')}")
    rep.check("s#2 cold took nothing from a peer",
              delta(before, after, "peer_bytes") == 0)

    # Warm peer pull through node-a (node-c now warm): multi-file peering.
    before = metrics(8001)
    warm = snapshot("http://127.0.0.1:8001", QWEN_REPO, REV)
    after = metrics(8001)
    rep.check("s#2 peer manifest byte-identical", warm == truth)
    rep.check("s#2 shards served from a peer",
              delta(before, after, "peer_bytes") > 0,
              f"peer_bytes +{delta(before, after, 'peer_bytes')}")
    print()
