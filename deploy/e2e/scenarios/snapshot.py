from __future__ import annotations
from harness import REV, SMOL_REPO, Report, bring_up, delta, metrics, reset_cache, snapshot


def run(rep: Report) -> None:
    print("== scenario #1: snapshot_download (mixed LFS + non-LFS) ==")
    truth = snapshot("DIRECT", SMOL_REPO, REV)
    bring_up("node-a")
    reset_cache("node-a")
    before = metrics(8001)
    try:
        got = snapshot("http://127.0.0.1:8001", SMOL_REPO, REV)
    except Exception as e:  # the Content-Length regression surfaced as a hard failure
        rep.check("s#1 snapshot completed through shim", False, str(e)[:200])
        print()
        return
    after = metrics(8001)
    rep.check("s#1 snapshot completed through shim", True)
    rep.check("s#1 every file byte-identical incl non-LFS companions",
              got == truth, f"{len(got)} files")
    rep.check("s#1 LFS bytes flowed through the shim",
              delta(before, after, "wan_bytes") > 0,
              f"wan_bytes +{delta(before, after, 'wan_bytes')}")
    print()
