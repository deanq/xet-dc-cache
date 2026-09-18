from __future__ import annotations
import re, time, urllib.request

_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<val>[^\s]+)\s*$')
_LBL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> list:
    """Parse Prometheus text exposition format into flat rows.

    Pure function, stdlib-only (`re`) so the unit test needs no extra deps.
    Handles plain samples, `{k="v"}`-labeled samples (including multi-label
    histogram bucket lines with `le="+Inf"`), and `%g`-formatted values
    (including exponential notation like `1.048576e+06`) — `float()` parses
    those natively, so no extra handling is needed for that case.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        labels = {k: v for k, v in _LBL.findall(m.group("labels") or "")}
        try:
            val = float(m.group("val"))
        except ValueError:
            continue
        rows.append({"name": m.group("name"), "labels": labels, "value": val})
    return rows


def scrape_once(pod_id: str, addr: str) -> list:
    """Fetch and parse one pod's /metrics/prometheus. Network; not unit-tested."""
    with urllib.request.urlopen(f"{addr}/metrics/prometheus", timeout=10) as r:
        rows = parse_prometheus(r.read().decode())
    ts = time.time()
    for row in rows:
        row["pod"] = pod_id
        row["ts"] = ts
    return rows


def main() -> None:  # integration: loop scrape all pods -> parquet
    import sys, pyarrow as pa, pyarrow.parquet as pq
    from runpod_testbed.provision.up import State
    from runpod_testbed.provision.fleet import Fleet, parse_external_addr

    st = State.load(sys.argv[1])
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    fleet = Fleet()
    addrs = {p: parse_external_addr(fleet.get_pod_ports(p)) for p in st.pods}
    out = f"data/pod-metrics-{st.runid}.parquet"
    buf = []
    try:
        while True:
            for pid, a in addrs.items():
                if a:
                    buf.extend(scrape_once(pid, a))
            time.sleep(interval)
    except KeyboardInterrupt:
        pq.write_table(pa.Table.from_pylist(buf), out)
        print(f"wrote {len(buf)} rows -> {out}")
