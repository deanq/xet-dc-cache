from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from harness import Report, bring_up, delta, download, metrics, reset_cache

N = 6


def run(rep: Report) -> None:
    print(f"== scenario #3: {N} concurrent cold pulls (singleflight) ==")
    truth = download("DIRECT")
    bring_up("node-a")
    reset_cache("node-a")
    before = metrics(8001)
    with ThreadPoolExecutor(max_workers=N) as pool:
        shas = list(pool.map(lambda _: download("http://127.0.0.1:8001"), range(N)))
    after = metrics(8001)

    d_wan = delta(before, after, "wan_bytes")
    d_served = delta(before, after, "served_bytes")
    rep.check(f"s#3 all {N} callers got identical bytes",
              all(s == truth for s in shas))
    rep.check("s#3 concurrent cold fetches collapsed (wan << served)",
              0 < d_wan <= d_served / 2,
              f"wan_bytes +{d_wan} vs served_bytes +{d_served}")
    print()
