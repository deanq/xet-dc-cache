"""Mechanism protocol + shared dataclasses (spec: "The Mechanism protocol")."""
from __future__ import annotations
import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

BASELINE_LABEL = "baseline"
ENDPOINT_PREFIX = "xet-dl"


def endpoint_name(label: str) -> str:
    """Flash endpoint name for a driver label ("A" -> "xet-dl-A")."""
    return f"{ENDPOINT_PREFIX}-{label}"


def state_path(runid: str) -> str:
    return f"data/state-{runid}.json"


@dataclass
class WorkerSpec:
    """How to build the Flash worker for this mechanism."""
    handler: str                                   # downloader key in worker/plan.py
    env: dict[str, str] = field(default_factory=dict)   # deploy-time env for `flash deploy`
    deps: list[str] = field(default_factory=list)
    network_volume_gb: int | None = None           # None = no volume attached


@dataclass
class ProvisionState:
    """Everything teardown needs; serialized to data/state-<runid>.json."""
    mechanism: str
    runid: str
    endpoints: dict[str, str] = field(default_factory=dict)     # label -> endpoint id
    pods: dict[str, str] = field(default_factory=dict)          # label -> pod id (shim only)
    volumes: dict[str, str] = field(default_factory=dict)       # label -> network volume id
    metrics_urls: dict[str, str] = field(default_factory=dict)  # label -> scrape base URL

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ProvisionState":
        return cls(**json.loads(text))

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            fh.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "ProvisionState":
        with open(path) as fh:
            return cls.from_json(fh.read())


class Mechanism(Protocol):
    name: str

    def provision(self, config, runid: str) -> ProvisionState: ...
    def worker_spec(self, config) -> WorkerSpec: ...
    def teardown(self, state: ProvisionState) -> None: ...
    def report_sections(self, jobs: list, metrics_rows: list) -> list[str]: ...
    def has_metrics(self) -> bool: ...
    def jobs(self, config) -> list[dict]: ...
