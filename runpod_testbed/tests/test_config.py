import pytest
from runpod_testbed.config import load_str

VALID = """
dc = "EU-RO-1"
registry = "docker.io/me"
cache_image = "me/xet-cache-testbed:latest"
worker_cpu = "cpu5c-4-8"
worker_deps = ["huggingface_hub", "hf_xet"]
models = ["org/x@main", "org/y@main", "org/z@main"]
burst = 4
max_pods = 3
max_burst = 8
pod_instance_id = "cpu3c-2-4"
container_disk_gb = 60
scrape_interval_s = 5
[overlap]
A = ["org/x@main", "org/y@main"]
B = ["org/y@main", "org/z@main"]
C = ["org/z@main", "org/x@main"]
"""

def test_load_ok():
    cfg = load_str(VALID)
    assert cfg.burst == 4
    assert cfg.overlap["A"] == ["org/x@main", "org/y@main"]

def test_rejects_changeme_image_placeholder():
    bad = VALID.replace("me/xet-cache-testbed:latest", "CHANGEME/xet-cache-testbed:latest")
    with pytest.raises(ValueError, match="placeholder"):
        load_str(bad)

def test_rejects_changeme_model_placeholder():
    bad = VALID.replace("org/x@main", "org/CHANGEME-a@main")
    with pytest.raises(ValueError, match="placeholder"):
        load_str(bad)

def test_rejects_burst_over_ceiling():
    bad = VALID.replace("burst = 4", "burst = 99")
    with pytest.raises(ValueError, match="burst"):
        load_str(bad)

def test_rejects_overlap_model_not_in_models():
    bad = VALID.replace('C = ["org/z@main", "org/x@main"]', 'C = ["org/z@main", "org/UNKNOWN@main"]')
    with pytest.raises(ValueError, match="UNKNOWN"):
        load_str(bad)


def test_overlap_must_be_exactly_a_b_c():
    bad = VALID.replace('C = ["org/z@main", "org/x@main"]\n', "")
    with pytest.raises(ValueError, match="A,B,C"):
        load_str(bad)


def test_overlap_a_b_c_still_loads():
    cfg = load_str(VALID)
    assert set(cfg.overlap) == {"A", "B", "C"}
