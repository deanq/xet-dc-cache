from runpod_testbed.drive.run import expand_jobs, endpoint_ids

def test_expand_cold_then_warm():
    overlap = {"A": ["x", "y"], "B": ["y", "z"]}
    jobs = expand_jobs(overlap, burst=3)
    cold = [j for j in jobs if j["phase"] == "cold"]
    warm = [j for j in jobs if j["phase"] == "warm"]
    assert len(cold) == 4                      # 2 groups * 2 models
    assert len(warm) == 4 * 3                   # each (group,model) * burst
    # all cold jobs precede all warm jobs
    assert jobs.index(cold[-1]) < jobs.index(warm[0])
    assert {j["group"] for j in jobs} == {"A", "B"}

def test_endpoint_ids_maps_group_to_id():
    # Real flash_manifest.json shape (verified live 2026-09-22): a "resources"
    # dict keyed by endpoint name, each carrying its endpoint_id.
    manifest = {"resources": {
        "xet-dl-A": {"functions": [{"name": "xet_dl_A"}], "endpoint_id": "ep-a"},
        "xet-dl-B": {"functions": [{"name": "xet_dl_B"}], "endpoint_id": "ep-b"},
    }}
    assert endpoint_ids(manifest) == {"A": "ep-a", "B": "ep-b"}
