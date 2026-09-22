from runpod_testbed.provision.preflight import check

_GOOD_ENV = {"RUNPOD_API_KEY": "k", "HF_TOKEN": "t"}
_empty_graphql = lambda q, k: {"data": {"myself": {"pods": [], "endpoints": []}}}
_ok_config = lambda path: object()


def test_clean_environment_has_no_problems():
    assert check(env=_GOOD_ENV, graphql=_empty_graphql, config_load=_ok_config) == []


def test_missing_secrets_are_reported():
    problems = check(env={}, graphql=_empty_graphql, config_load=_ok_config)
    assert any("RUNPOD_API_KEY" in p for p in problems)
    assert any("HF_TOKEN" in p for p in problems)


def test_orphaned_resources_are_reported():
    graphql = lambda q, k: {"data": {"myself": {
        "pods": [{"id": "p1"}], "endpoints": [{"id": "e1"}, {"id": "e2"}]}}}
    problems = check(env=_GOOD_ENV, graphql=graphql, config_load=_ok_config)
    assert any("orphaned resources" in p and "1 pods" in p and "2 endpoints" in p
               for p in problems)


def test_invalid_config_is_reported():
    def bad_config(path):
        raise ValueError("still has example placeholders")
    problems = check(env=_GOOD_ENV, graphql=_empty_graphql, config_load=bad_config)
    assert any("config invalid" in p and "placeholders" in p for p in problems)


def test_account_query_failure_is_reported():
    def boom(q, k):
        raise RuntimeError("403 Forbidden")
    problems = check(env=_GOOD_ENV, graphql=boom, config_load=_ok_config)
    assert any("could not query" in p for p in problems)
