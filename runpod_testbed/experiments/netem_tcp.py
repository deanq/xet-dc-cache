"""Netem TCP-knob experiment: does socket-buffer sizing + BBR help bulk transfer
on a high-BDP (cross-DC-like) link? This is the measurable half of the
"peer transport tuning" recommendation from the network-transport feasibility
study — the knobs are PEER_SOCKET_BUFFER_BYTES (shim-go/peer_transport.go) and
the host's tcp_congestion_control.

It is deliberately self-contained (stdlib only) and transport-only: it does NOT
use the shim. It shapes loopback with `tc netem` (delay + rate [+ loss]) to
synthesize a fat, distant link, then measures single-stream throughput under the
cross product of {default vs large socket buffers} x {cubic vs bbr}. On a clean
same-DC link these knobs do ~nothing; the point is to quantify the payoff as the
link gets long and fat.

    sudo python3 -m runpod_testbed.experiments.netem_tcp --delay-ms 25 --rate-mbit 1000 --size-mb 256

Requires root / NET_ADMIN (for `tc`). If the environment denies it, that denial
IS the finding: transport tuning needs host access, so it can't be done from an
unprivileged serverless worker — record that and recommend host-level config.
BBR must be available (`modprobe tcp_bbr` or a kernel with it built in); the
script skips bbr with a note if it isn't listed as available.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time

# TCP_CONGESTION is not exported by the socket module on all versions; its value
# is 13 on Linux (uapi/linux/tcp.h). setsockopt takes the algo name as bytes.
_TCP_CONGESTION = getattr(socket, "TCP_CONGESTION", 13)
_LARGE_BUF = 16 << 20  # 16 MiB — comfortably above a 25ms*1Gbit (~3MiB) BDP


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def have_netadmin() -> tuple[bool, str]:
    """Probe for NET_ADMIN by adding then deleting a no-op qdisc on lo."""
    try:
        _run(["tc", "qdisc", "add", "dev", "lo", "root", "netem", "delay", "0ms"])
        _run(["tc", "qdisc", "del", "dev", "lo", "root"], check=False)
        return True, ""
    except FileNotFoundError:
        return False, "`tc` not found (install iproute2)"
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or str(e)).strip()


def available_cc() -> list[str]:
    try:
        with open("/proc/sys/net/ipv4/tcp_available_congestion_control") as fh:
            return fh.read().split()
    except OSError:
        return []


def set_netem(delay_ms: int, rate_mbit: int, loss_pct: float) -> None:
    _run(["tc", "qdisc", "del", "dev", "lo", "root"], check=False)
    cmd = ["tc", "qdisc", "add", "dev", "lo", "root", "netem", "delay", f"{delay_ms}ms"]
    if rate_mbit:
        cmd += ["rate", f"{rate_mbit}mbit"]
    if loss_pct:
        cmd += ["loss", f"{loss_pct}%"]
    _run(cmd)


def clear_netem() -> None:
    _run(["tc", "qdisc", "del", "dev", "lo", "root"], check=False)


def _try_set_cc(sock: socket.socket, cc: str) -> None:
    """Best-effort per-socket congestion control; ignored if the kernel refuses."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, _TCP_CONGESTION, cc.encode())
    except OSError:
        pass


def _serve_once(listener: socket.socket, payload: bytes, buf: int, cc: str) -> None:
    conn, _ = listener.accept()
    with conn:
        if buf:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buf)
        _try_set_cc(conn, cc)
        conn.sendall(payload)


def measure(size_mb: int, buf: int, cc: str) -> float:
    """One transfer of size_mb over loopback; returns throughput in MiB/s."""
    payload = b"\0" * (1 << 20)
    payload = payload * size_mb
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    server = threading.Thread(target=_serve_once, args=(listener, payload, buf, cc), daemon=True)
    server.start()

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if buf:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buf)
    _try_set_cc(client, cc)
    t0 = time.monotonic()
    client.connect(("127.0.0.1", port))
    got = 0
    while got < len(payload):
        chunk = client.recv(1 << 20)
        if not chunk:
            break
        got += len(chunk)
    elapsed = time.monotonic() - t0
    client.close()
    listener.close()
    server.join(timeout=5)
    return (got / (1 << 20)) / elapsed if elapsed > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay-ms", type=int, default=25, help="one-way netem delay (RTT ~= 2x)")
    ap.add_argument("--rate-mbit", type=int, default=1000, help="netem rate cap (0 = uncapped)")
    ap.add_argument("--loss-pct", type=float, default=0.0)
    ap.add_argument("--size-mb", type=int, default=256)
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    ok, why = have_netadmin()
    if not ok:
        print(f"FINDING: cannot shape the link (NET_ADMIN denied): {why}")
        print("Transport tuning (socket buffers, BBR) needs host access — it cannot be")
        print("applied from an unprivileged serverless worker. Recommend host/pod-level")
        print("config on the peer fleet, not a shim change alone.")
        return 2

    ccs = ["cubic"]
    if "bbr" in available_cc():
        ccs.append("bbr")
    else:
        print("note: bbr not in tcp_available_congestion_control; testing cubic only "
              "(try `modprobe tcp_bbr`).")

    print(f"netem: delay={args.delay_ms}ms rate={args.rate_mbit}mbit loss={args.loss_pct}% "
          f"size={args.size_mb}MiB reps={args.reps}  (BDP ~= "
          f"{(args.rate_mbit/8)*(2*args.delay_ms/1000):.1f} MiB)\n")
    try:
        set_netem(args.delay_ms, args.rate_mbit, args.loss_pct)
        print(f"{'cc':6} {'sockbuf':>9} {'MiB/s (median)':>16}")
        for cc in ccs:
            for buf, label in ((0, "default"), (_LARGE_BUF, "16MiB")):
                runs = sorted(measure(args.size_mb, buf, cc) for _ in range(args.reps))
                print(f"{cc:6} {label:>9} {runs[len(runs)//2]:>16.1f}")
    finally:
        clear_netem()
    return 0


if __name__ == "__main__":
    sys.exit(main())
