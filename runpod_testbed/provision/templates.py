"""Delete the endpoint templates Flash (runpod-python 2.1.0) leaves behind.

`rp flash deploy` creates one template per resource, named
`<app>-<resource>-template` (runpod.apps.deploy._deployed_endpoint_input), but
undeploy only DELETEs the endpoints (`/v2/serverless/<id>`), which leaves the
bound template. The platform then holds that template for the deleted endpoint
for up to ~90 min ("Template is still held by deleted endpoint <id>, which is
pending cleanup"), and any deploy that reuses the name fails with "endpoint
template names must be unique". Each run therefore deploys its own app
(provision/flash.py::flash_app_name) and teardown tries to delete the
templates by exact name; a "still held" refusal is reported, not raised.

GraphQL `deleteTemplate(templateName:)` is used because REST
`DELETE /v1/templates/<id>` answers 500 "template not found" for these
endpoint-bound templates even though `GET /v1/templates` lists them.
"""
from __future__ import annotations

import os

from runpod_testbed.provision.modelstore_api import post_graphql

_DELETE_TEMPLATE = "mutation($name: String!) { deleteTemplate(templateName: $name) }"


def flash_template_name(app_name: str, resource: str) -> str:
    return f"{app_name}-{resource}-template"


def delete_flash_templates(app_name: str, resources: list[str], api_key: str | None = None,
                           post=post_graphql) -> list[str]:
    """Delete `<app_name>-<resource>-template` for each resource; returns
    failures (never raises) so teardown keeps going."""
    api_key = api_key or os.environ["RUNPOD_API_KEY"]
    failures = []
    for resource in resources:
        name = flash_template_name(app_name, resource)
        try:
            post(_DELETE_TEMPLATE, {"name": name}, api_key)
        except Exception as e:
            failures.append(f"template {name}: {e}")
    return failures
