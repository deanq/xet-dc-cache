"""Delete (or report) the endpoint templates a run's Flash app leaves behind.

`rp flash deploy` creates one template per endpoint and undeploy deletes only
the endpoints. The platform then holds each template for its deleted endpoint
for up to ~90 min ("Template is still held by deleted endpoint <id>, which is
pending cleanup"), so a delete right after teardown is refused.

runpod-python 2.2 (#696) names new templates `<app>-<resource>-template-<6 hex>`,
so a redeploy of the same app no longer collides with a held template, but the
held templates still exist after teardown. The names are not predictable any
more, so they are found by listing: the run's app name (`xet-testbed-<runid>`)
is unique per run, which makes the `<app>-` prefix an unambiguous ownership
test on a shared account. Teardown tries each delete and returns the refusals
with the exact template name, so they can be deleted later by hand.

GraphQL `deleteTemplate(templateName:)` is used because REST
`DELETE /v1/templates/<id>` answers 500 "template not found" for these
endpoint-bound templates even though `GET /v1/templates` lists them.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from runpod_testbed.provision.modelstore_api import post_graphql
from runpod_testbed.provision.volumes import REST_BASE, _request

_DELETE_TEMPLATE = "mutation($name: String!) { deleteTemplate(templateName: $name) }"
# endpoint-bound templates are hidden from GET /templates unless asked for
_LIST_URL = f"{REST_BASE}/templates?includeEndpointBoundTemplates=true"


def is_flash_template_of(app_name: str, template_name: str) -> bool:
    """`<app>-<resource>-template`, optionally with the 2.2+ hex suffix."""
    pattern = rf"{re.escape(app_name)}-.+-template(-[0-9a-f]+)?"
    return re.fullmatch(pattern, template_name) is not None


def list_flash_templates(app_name: str, api_key: str,
                         opener=urllib.request.urlopen) -> list[str]:
    status, body = _request("GET", _LIST_URL, api_key, opener)
    if status != 200:
        raise RuntimeError(f"GET /templates -> {status}: {body[:200]!r}")
    names = (t.get("name", "") for t in json.loads(body or b"[]"))
    return sorted(n for n in names if is_flash_template_of(app_name, n))


def delete_flash_templates(app_name: str, api_key: str | None = None, *,
                           list_templates=list_flash_templates,
                           post=post_graphql) -> list[str]:
    """Delete every template of `app_name`; returns failures (a held template
    is one, named exactly) and never raises, so teardown keeps going."""
    api_key = api_key or os.environ["RUNPOD_API_KEY"]
    try:
        names = list_templates(app_name, api_key)
    except Exception as e:
        return [f"listing templates of {app_name}: {e}"]
    failures = []
    for name in names:
        try:
            post(_DELETE_TEMPLATE, {"name": name}, api_key)
        except Exception as e:
            failures.append(f"template {name}: {e}")
    return failures
