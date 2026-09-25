"""Registry of cache mechanisms under test. Phases 2/3 add entries."""
from __future__ import annotations
from runpod_testbed.mechanisms.modelstore import ModelStoreMechanism
from runpod_testbed.mechanisms.shim import ShimMechanism
from runpod_testbed.mechanisms.volumecache import VolumeCacheMechanism

MECHANISMS = {
    "shim": ShimMechanism(),
    "volumecache": VolumeCacheMechanism(),
    "modelstore": ModelStoreMechanism(),
}


def get_mechanism(name: str):
    if name not in MECHANISMS:
        raise ValueError(f"unknown mechanism {name!r}; known: {sorted(MECHANISMS)}")
    return MECHANISMS[name]
