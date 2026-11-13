import json

from runpod_testbed.provision.templates import (
    delete_flash_templates, is_flash_template_of, list_flash_templates)

APP = "xet-testbed-20261113-001000"


def test_matches_suffixed_2_2_names_and_legacy_2_1_names():
    # runpod.apps.deploy: 2.2 `{app}-{resource}-template-<6 hex>`, 2.1 without the suffix
    assert is_flash_template_of(APP, f"{APP}-xet-dl-A-template-0caecc")
    assert is_flash_template_of(APP, f"{APP}-xet-dl-baseline-template")


def test_rejects_templates_of_other_apps_and_runs():
    assert not is_flash_template_of(APP, "xet-testbed-20261113-001001-xet-dl-A-template-0caecc")
    assert not is_flash_template_of(APP, f"teammate-{APP}-xet-dl-A-template")
    assert not is_flash_template_of(APP, f"{APP}-xet-dl-A-template-notahex")
    assert not is_flash_template_of(APP, "vllm-worker")


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_list_asks_for_endpoint_bound_templates_and_filters_by_app():
    seen = []
    listed = [{"name": f"{APP}-xet-dl-B-template-0a0b0c"}, {"name": "someone-else"},
              {"name": f"{APP}-xet-dl-A-template-038ec2"}]

    def opener(req, timeout):
        seen.append(req.full_url)
        return _Resp(200, json.dumps(listed).encode())

    assert list_flash_templates(APP, "k", opener=opener) == [
        f"{APP}-xet-dl-A-template-038ec2", f"{APP}-xet-dl-B-template-0a0b0c"]
    assert "includeEndpointBoundTemplates=true" in seen[0]


def test_delete_flash_templates_deletes_each_listed_name():
    sent = []
    names = [f"{APP}-xet-dl-A-template-038ec2", f"{APP}-xet-dl-baseline-template-038ec2"]
    failures = delete_flash_templates(APP, "k", list_templates=lambda app, key: names,
                                      post=lambda q, v, key: sent.append((v["name"], key)))
    assert failures == []
    assert sent == [(n, "k") for n in names]


def test_held_template_is_reported_by_exact_name_and_others_still_deleted():
    sent = []
    names = [f"{APP}-A-template-aa", f"{APP}-B-template-bb"]

    def post(q, v, key):
        sent.append(v["name"])
        if v["name"].endswith("-aa"):
            raise RuntimeError("GraphQL error: Template is still held by deleted endpoint e1")

    failures = delete_flash_templates(APP, "k", list_templates=lambda app, key: names, post=post)
    assert sent == names
    assert failures == [f"template {APP}-A-template-aa: GraphQL error: "
                        "Template is still held by deleted endpoint e1"]


def test_listing_failure_is_reported_not_raised():
    def boom(app, key):
        raise RuntimeError("GET /templates -> 500")

    failures = delete_flash_templates(APP, "k", list_templates=boom, post=None)
    assert len(failures) == 1 and "500" in failures[0] and APP in failures[0]
