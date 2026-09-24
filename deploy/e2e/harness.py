from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SMOL_REPO = os.environ.get("SMOKE_REPO", "HuggingFaceTB/SmolLM2-1.7B-Instruct")
REV = os.environ.get("SMOKE_REV", "main")
FILE = os.environ.get("SMOKE_PATH", "model.safetensors")
QWEN_REPO = "Qwen/Qwen2.5-3B-Instruct"

E2E_DIR = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "-f", str(E2E_DIR / "docker-compose.yml")]
CACHES = E2E_DIR / "caches"
WORKER = str(E2E_DIR / "hf_worker.py")

NODES = {"node-a": 8001, "node-b": 8002, "node-c": 8003, "node-evict": 8004}


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _worker(mode: str, *args: str) -> str:
    proc = subprocess.run([sys.executable, WORKER, mode, *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{mode} {args} failed:\n{proc.stderr}")
    return proc.stdout.strip().splitlines()[-1]


def download(endpoint: str, rev: str = REV) -> str:
    return _worker("--file", SMOL_REPO, rev, FILE, endpoint)


def timed_download(endpoint: str, rev: str = REV) -> tuple[str, float]:
    t0 = time.monotonic()
    sha = download(endpoint, rev)
    return sha, time.monotonic() - t0


def snapshot(endpoint: str, repo: str, rev: str) -> dict[str, str]:
    return json.loads(_worker("--snapshot", repo, rev, endpoint))


def metrics(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as r:
        return json.load(r)


def delta(before: dict, after: dict, key: str) -> int:
    return int(after.get(key, 0)) - int(before.get(key, 0))


def rate(nbytes: int, seconds: float) -> str:
    mbps = nbytes / max(seconds, 1e-6) / 1e6
    return f"{seconds:.1f}s ({mbps:,.0f} MB/s)"


def reset_cache(node: str) -> None:
    d = (CACHES / node.removeprefix("node-")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["docker", "run", "--rm", "-v", f"{d}:/cache", "busybox",
                    "sh", "-c", "rm -rf /cache/* /cache/.[!.]* 2>/dev/null || true"],
                   check=False, capture_output=True)


def bring_up(*nodes: str) -> None:
    sh(*COMPOSE, "up", "-d", *nodes)
    wait_healthy(nodes)


def wait_healthy(nodes: "list | tuple | None" = None, timeout: float = 60.0) -> None:
    targets = {n: NODES[n] for n in nodes} if nodes else dict(NODES)
    deadline = time.time() + timeout
    for name, port in targets.items():
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


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []
        self.notes: list[str] = []  # freeform lines (e.g. timing) for the report

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))

    def note(self, text: str = "") -> None:
        """Record a freeform line for the persisted report (does not print or
        affect ok()). Scenarios use it to capture their timing summary."""
        self.notes.append(text)

    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.rows)

    def to_markdown(self, *, model: str, scenarios: "list[str]") -> str:
        """Render a shareable markdown report from the collected checks + notes,
        mirroring the demo report's headline-first style."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        n_pass = sum(1 for _, ok, _ in self.rows if ok)
        result = "PASS" if self.ok() else "FAIL"
        lines = [
            f"# xet-dc-cache e2e report — {ts}",
            "",
            f"- **RESULT: {result}** ({n_pass}/{len(self.rows)} checks passed)",
            f"- Primary model: {model}",
            f"- Scenarios: {', '.join(scenarios)}",
            "",
        ]
        if self.notes:
            lines += ["## Timing", "", "```"]
            lines += self.notes
            lines += ["```", ""]
        lines += ["## Checks", "", "| check | result | detail |", "|---|---|---|"]
        for name, ok, detail in self.rows:
            lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
        lines.append("")
        return "\n".join(lines)
