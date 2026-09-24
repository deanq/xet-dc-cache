import harness
from scenarios import parse_scenarios, ORDER


def test_parse_scenarios_defaults_to_all():
    names = [n for n, _ in ORDER]
    assert parse_scenarios(None) == names
    assert parse_scenarios("") == names


def test_parse_scenarios_selects_subset_and_trims():
    assert parse_scenarios("snapshot, eviction") == ["snapshot", "eviction"]


def test_delta_subtracts_by_key():
    assert harness.delta({"wan_bytes": 10}, {"wan_bytes": 45}, "wan_bytes") == 35
    assert harness.delta({}, {"hits": 3}, "hits") == 3      # missing before -> 0


def test_rate_guards_zero_duration():
    assert "MB/s" in harness.rate(1_000_000, 0.0)          # no ZeroDivisionError


def test_report_ok_reflects_failures():
    r = harness.Report()
    r.check("a", True)
    assert r.ok() is True
    r.check("b", False, "boom")
    assert r.ok() is False
