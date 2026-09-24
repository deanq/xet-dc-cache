import pytest

import harness
from scenarios import parse_scenarios, ORDER


def test_parse_scenarios_defaults_to_all():
    names = [n for n, _ in ORDER]
    assert parse_scenarios(None) == names
    assert parse_scenarios("") == names


def test_parse_scenarios_selects_subset_and_trims():
    assert parse_scenarios("snapshot, eviction") == ["snapshot", "eviction"]


def test_parse_scenarios_rejects_unknown_name():
    # Fail secure: a typo must raise, not silently select zero scenarios
    # (which would exit 0 with RESULT: PASS -- a false green for CI).
    with pytest.raises(ValueError):
        parse_scenarios("snpashot")


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


def test_report_to_markdown_renders_result_checks_and_notes():
    r = harness.Report()
    r.check("s1 ok", True, "wan_bytes +42")
    r.check("s2 bad", False)
    r.note("WAN cold: 40.0s -> 2.5x faster")
    md = r.to_markdown(model="org/m@main", scenarios=["peering", "snapshot"])
    assert "RESULT: FAIL" in md          # any failed check -> FAIL
    assert "1/2 checks passed" in md
    assert "org/m@main" in md
    assert "peering, snapshot" in md
    assert "| s1 ok | PASS | wan_bytes +42 |" in md
    assert "| s2 bad | FAIL |" in md
    assert "WAN cold: 40.0s -> 2.5x faster" in md


def test_report_note_does_not_affect_ok():
    r = harness.Report()
    r.check("only", True)
    r.note("timing line")
    assert r.ok() is True
