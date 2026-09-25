from runpod_testbed.mechanisms.base import BASELINE_LABEL
from runpod_testbed.mechanisms.baseline import (
    BASELINE_MECHANISM, BASELINE_WORKER_SPEC, baseline_jobs,
)


def test_baseline_jobs_one_per_model_on_control_endpoint():
    jobs = baseline_jobs(["org/a@main", "org/b@main"])
    assert jobs == [
        {"mechanism": BASELINE_MECHANISM, "endpoint": BASELINE_LABEL,
         "model": "org/a@main", "phase": "baseline", "replica": 0},
        {"mechanism": BASELINE_MECHANISM, "endpoint": BASELINE_LABEL,
         "model": "org/b@main", "phase": "baseline", "replica": 0},
    ]


def test_baseline_worker_spec_is_plain_hf():
    assert BASELINE_WORKER_SPEC.handler == "baseline"
    assert "HF_ENDPOINT" not in BASELINE_WORKER_SPEC.env
    assert BASELINE_WORKER_SPEC.network_volume_gb is None
