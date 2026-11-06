from runpod_testbed.provision.templates import delete_flash_templates, flash_template_name


def test_template_name_matches_runpod_2_1_deploy():
    # runpod.apps.deploy._deployed_endpoint_input: f"{app.name}-{spec.name}-template"
    assert flash_template_name("xet-testbed-r1", "xet-dl-A") == "xet-testbed-r1-xet-dl-A-template"


def test_delete_flash_templates_deletes_each_by_exact_name():
    sent = []
    failures = delete_flash_templates("xet-testbed-r1", ["xet-dl-A", "xet-dl-baseline"], "k",
                                      post=lambda q, v, key: sent.append((v["name"], key)))
    assert failures == []
    assert sent == [("xet-testbed-r1-xet-dl-A-template", "k"),
                    ("xet-testbed-r1-xet-dl-baseline-template", "k")]


def test_delete_flash_templates_collects_failures_and_keeps_going():
    sent = []

    def post(q, v, key):
        sent.append(v["name"])
        if v["name"].endswith("-A-template"):
            raise RuntimeError("GraphQL error: Template is still held by deleted endpoint e1")

    failures = delete_flash_templates("app", ["A", "B"], "k", post=post)
    assert sent == ["app-A-template", "app-B-template"]
    assert len(failures) == 1 and "still held" in failures[0]
