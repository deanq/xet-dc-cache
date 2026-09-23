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


def write_metrics(buf: list, out: str) -> str:
    """Flush collected metric rows to a parquet file. Returns a status line.

    Uses an explicit schema (labels as a string→string map) so a cycle whose
    rows all carry empty labels still writes — inferring the type from data
    would fail with "struct type 'labels' with no child field".
    """
    import pyarrow as pa, pyarrow.parquet as pq
    schema = pa.schema([
        ("name", pa.string()),
        ("labels", pa.map_(pa.string(), pa.string())),
        ("value", pa.float64()),
        ("pod", pa.string()),
        ("ts", pa.float64()),
    ])
    pq.write_table(pa.Table.from_pylist(buf, schema=schema), out)
    return f"wrote {len(buf)} rows -> {out}"


def main() -> None:  # integration: loop scrape all pods -> parquet
    import sys, signal
    from runpod_testbed import config
    from runpod_testbed.provision.up import State
    from runpod_testbed.provision.fleet import Fleet, parse_external_addr

    st = State.load(sys.argv[1])
    # Config is the source of truth for the scrape interval (scrape_interval_s);
    # sys.argv[2] remains as an optional one-off override.
    cfg = config.load("runpod_testbed/config.toml")
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else cfg.scrape_interval_s
    fleet = Fleet()
    addrs = {p: parse_external_addr(fleet.get_pod_ports(p)) for p in st.pods}
    out = f"data/pod-metrics-{st.runid}.parquet"

    # Flush on Ctrl-C (SIGINT → KeyboardInterrupt) AND on SIGTERM. The demo runs
    # this scraper in the background and stops it with `kill`; without a SIGTERM
    # handler that would drop every collected metric. Reuse the KeyboardInterrupt
    # path so both signals flush identically.
    def _flush_and_exit(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _flush_and_exit)

    print(f"scrape: {sum(1 for a in addrs.values() if a)} pods, "
          f"interval {interval}s -> {out}", flush=True)
    buf = []
    try:
        while True:
            for pid, a in addrs.items():
                if a:
                    try:
                        buf.extend(scrape_once(pid, a))
                    except Exception as e:  # one flaky pod must not stop the run
                        print(f"scrape: {pid} scrape failed: {e}", flush=True)
            # Write every cycle so the parquet survives ANY stop (SIGTERM, SIGKILL,
            # or the process being reaped by a parent) — the report only needs the
            # latest sample per pod, so re-writing the growing buffer is correct.
            if buf:
                write_metrics(buf, out)
            time.sleep(interval)
    except KeyboardInterrupt:
        msg = write_metrics(buf, out) if buf else "scrape: no metrics collected"
        print(msg, flush=True)


if __name__ == "__main__":
    main()
