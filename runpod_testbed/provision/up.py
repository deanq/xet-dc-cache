from __future__ import annotations
import json
import os
import sys
from dataclasses import asdict, dataclass


def flash_deploy_env(addrs: dict, cfg, hf_token: str) -> dict:
    env = {f"POD_ADDR_{g}": a for g, a in addrs.items()}
    env["HF_TOKEN"] = hf_token
    env["WORKER_CPU"] = cfg.worker_cpu
    env["WORKER_DEPS"] = ",".join(cfg.worker_deps)
    env["WORKER_MAX"] = str(cfg.burst)
    return env


@dataclass
class State:
    runid: str
    pods: list
    flash_env: str

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "State":
        with open(path) as fh:
            return cls(**json.load(fh))


def main() -> None:
    raise SystemExit("provision.up.main is rewired in Task 9")  # placeholder removed in Task 9


if __name__ == "__main__":
    main()
