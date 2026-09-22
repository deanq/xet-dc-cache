from __future__ import annotations
import tomllib
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    dc: str
    registry: str
    cache_image: str
    worker_cpu: str
    worker_deps: list[str]
    models: list[str]
    overlap: dict[str, list[str]]
    burst: int
    max_pods: int
    max_burst: int
    pod_instance_id: str
    container_disk_gb: int
    scrape_interval_s: int

_REQUIRED = ("dc", "registry", "cache_image", "worker_cpu", "worker_deps",
             "models", "overlap", "burst", "max_pods", "max_burst",
             "pod_instance_id", "container_disk_gb", "scrape_interval_s")

_DEFAULTS = {"dc": "EU-RO-1"}

def load_str(text: str) -> Config:
    raw = tomllib.loads(text)
    for key, default in _DEFAULTS.items():
        raw.setdefault(key, default)
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise ValueError(f"config missing keys: {missing}")
    cfg = Config(**{k: raw[k] for k in _REQUIRED})
    placeholders = [
        f"{name}={val}"
        for name, val in (("registry", cfg.registry), ("cache_image", cfg.cache_image))
        if "CHANGEME" in val
    ] + [f"models[{i}]={m}" for i, m in enumerate(cfg.models) if "CHANGEME" in m]
    if placeholders:
        raise ValueError(
            "config still has example placeholders — edit config.toml before "
            "provisioning (pods would pull a nonexistent image and time out): "
            + ", ".join(placeholders))
    if cfg.burst > cfg.max_burst:
        raise ValueError(f"burst {cfg.burst} exceeds max_burst {cfg.max_burst}")
    if len(cfg.overlap) > cfg.max_pods:
        raise ValueError(f"overlap groups {len(cfg.overlap)} exceed max_pods {cfg.max_pods}")
    if set(cfg.overlap) != {"A", "B", "C"}:
        raise ValueError(f"overlap groups must be exactly A,B,C (got {sorted(cfg.overlap)})")
    known = set(cfg.models)
    for grp, ms in cfg.overlap.items():
        for m in ms:
            if m not in known:
                raise ValueError(f"overlap[{grp}] references unknown model {m}")
    return cfg

def load(path: str) -> Config:
    with open(path, "rb") as fh:
        return load_str(fh.read().decode())
