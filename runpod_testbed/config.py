# runpod_testbed/config.py
from __future__ import annotations
import tomllib
from dataclasses import dataclass, field

SUPPORTED_MECHANISMS = ("shim", "volumecache", "modelstore")


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
    job_timeout_s: int
    mechanism: str = "shim"
    volume_gb: int = 0                                   # [volumecache]
    modelstore_endpoints: dict[str, str] = field(default_factory=dict)  # [modelstore.endpoints]
    worker_gpu: str = ""                                 # "" = CPU; comma-list of GpuGroup/GpuType names


_SHARED = ("dc", "worker_cpu", "worker_deps", "models", "burst", "max_burst",
           "container_disk_gb", "scrape_interval_s", "job_timeout_s", "worker_gpu")
_SHIM = ("registry", "cache_image", "overlap", "max_pods", "pod_instance_id")
_SHIM_ABSENT = {"registry": "", "cache_image": "", "overlap": {}, "max_pods": 0,
                "pod_instance_id": ""}

# job_timeout_s: how long drive waits for a single job's result. runpod's
# Job.output(timeout=0) does NOT wait — it returns None immediately — so drive
# must pass a real ceiling that covers a cold worker's boot + dep install +
# download. Optional (defaulted) so pre-existing configs keep working.
_DEFAULTS = {"dc": "EU-RO-1", "job_timeout_s": 600, "worker_gpu": ""}


def _shim_section(raw: dict) -> dict:
    """[shim] subtable, falling back to legacy top-level keys."""
    shim = dict(raw.get("shim", {}))
    for key in _SHIM:
        if key not in shim and key in raw:
            shim[key] = raw[key]
    return shim


def _check_placeholders(cfg: Config) -> None:
    pairs = [("registry", cfg.registry), ("cache_image", cfg.cache_image)] if cfg.mechanism == "shim" else []
    placeholders = [f"{n}={v}" for n, v in pairs if "CHANGEME" in v]
    placeholders += [f"models[{i}]={m}" for i, m in enumerate(cfg.models) if "CHANGEME" in m]
    if placeholders:
        raise ValueError(
            "config still has example placeholders — edit config.toml before "
            "provisioning (pods would pull a nonexistent image and time out): "
            + ", ".join(placeholders))


def _check_shim(cfg: Config) -> None:
    if len(cfg.overlap) > cfg.max_pods:
        raise ValueError(f"overlap groups {len(cfg.overlap)} exceed max_pods {cfg.max_pods}")
    if set(cfg.overlap) != {"A", "B", "C"}:
        raise ValueError(f"overlap groups must be exactly A,B,C (got {sorted(cfg.overlap)})")
    known = set(cfg.models)
    for grp, ms in cfg.overlap.items():
        for m in ms:
            if m not in known:
                raise ValueError(f"overlap[{grp}] references unknown model {m}")


def _mechanism_fields(raw: dict, mechanism: str) -> dict:
    """Shim keys (required for shim, defaulted otherwise) + per-mechanism subtable."""
    if mechanism not in SUPPORTED_MECHANISMS:
        raise ValueError(f"unknown mechanism {mechanism!r}; expected one of {SUPPORTED_MECHANISMS}")
    shim = _shim_section(raw)
    if mechanism == "shim":
        missing = [k for k in _SHIM if k not in shim]
        if missing:
            raise ValueError(f"config missing [shim] keys: {missing}")
        fields = {k: shim[k] for k in _SHIM}
    else:
        fields = dict(_SHIM_ABSENT)
    vc = raw.get("volumecache", {})
    if mechanism == "volumecache" and "volume_gb" not in vc:
        raise ValueError("config missing [volumecache] volume_gb")
    fields["volume_gb"] = int(vc.get("volume_gb", 0))
    fields["modelstore_endpoints"] = dict(raw.get("modelstore", {}).get("endpoints", {}))
    return fields


def load_str(text: str, mechanism_override: str | None = None) -> Config:
    raw = tomllib.loads(text)
    for key, default in _DEFAULTS.items():
        raw.setdefault(key, default)
    missing = [k for k in _SHARED if k not in raw]
    if missing:
        raise ValueError(f"config missing keys: {missing}")
    mechanism = mechanism_override or raw.get("mechanism", "shim")
    cfg = Config(**{k: raw[k] for k in _SHARED}, mechanism=mechanism,
                 **_mechanism_fields(raw, mechanism))
    _check_placeholders(cfg)
    if cfg.burst > cfg.max_burst:
        raise ValueError(f"burst {cfg.burst} exceeds max_burst {cfg.max_burst}")
    if cfg.mechanism == "shim":
        _check_shim(cfg)
    return cfg


def load(path: str, mechanism_override: str | None = None) -> Config:
    with open(path, "rb") as fh:
        return load_str(fh.read().decode(), mechanism_override)
