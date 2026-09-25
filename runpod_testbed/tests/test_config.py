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


VALID_VOLUMECACHE = """
mechanism = "volumecache"
dc = "EU-RO-1"
worker_cpu = "cpu5c-4-8"
worker_deps = ["huggingface_hub", "hf_xet"]
models = ["org/x@main", "org/y@main"]
burst = 4
max_burst = 8
container_disk_gb = 20
scrape_interval_s = 5
[volumecache]
volume_gb = 50
"""

VALID_SHIM_SUBTABLE = """
mechanism = "shim"
dc = "EU-RO-1"
worker_cpu = "cpu5c-4-8"
worker_deps = ["huggingface_hub"]
models = ["org/x@main", "org/y@main", "org/z@main"]
burst = 2
max_burst = 8
container_disk_gb = 20
scrape_interval_s = 5
[shim]
registry = "docker.io/me"
cache_image = "me/xet-cache-testbed:latest"
max_pods = 3
pod_instance_id = "cpu3c-2-4"
[shim.overlap]
A = ["org/x@main", "org/y@main"]
B = ["org/y@main", "org/z@main"]
C = ["org/z@main", "org/x@main"]
"""


def test_legacy_toplevel_config_defaults_to_shim():
    cfg = load_str(VALID)
    assert cfg.mechanism == "shim"
    assert cfg.volume_gb == 0 and cfg.modelstore_endpoints == {}


def test_shim_keys_read_from_shim_subtable():
    cfg = load_str(VALID_SHIM_SUBTABLE)
    assert cfg.registry == "docker.io/me" and cfg.max_pods == 3
    assert cfg.overlap["B"] == ["org/y@main", "org/z@main"]


def test_volumecache_config_needs_no_shim_keys():
    cfg = load_str(VALID_VOLUMECACHE)
    assert cfg.mechanism == "volumecache"
    assert cfg.volume_gb == 50
    assert cfg.registry == "" and cfg.overlap == {}


def test_volumecache_requires_volume_gb():
    bad = VALID_VOLUMECACHE.replace("volume_gb = 50", "")
    with pytest.raises(ValueError, match="volume_gb"):
        load_str(bad)


def test_modelstore_config_reads_optional_endpoint_map():
    text = VALID_VOLUMECACHE.replace('mechanism = "volumecache"', 'mechanism = "modelstore"') \
        .replace("[volumecache]\nvolume_gb = 50", '[modelstore]\n[modelstore.endpoints]\n"org/x@main" = "ep-x"')
    cfg = load_str(text)
    assert cfg.modelstore_endpoints == {"org/x@main": "ep-x"}


def test_shim_mechanism_still_requires_shim_keys():
    bad = VALID_VOLUMECACHE.replace('mechanism = "volumecache"', 'mechanism = "shim"')
    with pytest.raises(ValueError, match="registry"):
        load_str(bad)


def test_unknown_mechanism_rejected():
    with pytest.raises(ValueError, match="mechanism"):
        load_str(VALID_VOLUMECACHE.replace('"volumecache"', '"turbo"'))


def test_mechanism_override_wins_over_toml():
    cfg = load_str(VALID_SHIM_SUBTABLE.replace("[shim]", "[volumecache]\nvolume_gb = 20\n[shim]"),
                   mechanism_override="volumecache")
    assert cfg.mechanism == "volumecache" and cfg.volume_gb == 20


def test_worker_gpu_defaults_to_empty_string():
    assert load_str(VALID).worker_gpu == ""


def test_worker_gpu_loads_from_toml():
    # Insert before [overlap] -- appending after it would land inside that
    # TOML table instead of at the top level.
    cfg = load_str(VALID.replace("[overlap]", 'worker_gpu = "AMPERE_16"\n[overlap]'))
    assert cfg.worker_gpu == "AMPERE_16"
