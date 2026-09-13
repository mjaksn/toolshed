"""Tests for ha_rest_yaml.py. Standard library only, so nothing to install.

    python -m unittest

One small document and one run per kind of YAML, each answering the questions
from a script and comparing the whole snippet. Between them they cover a JSON
response reached through $ref and allOf, a plain text response, parameters in
the path, query, header and body, all three kinds of authentication, and the
command line options.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import ha_rest_yaml

SPEC = {
    "openapi": "3.1.0",
    "servers": [{"url": "http://{host}:8080/api/", "variables": {"host": {"default": "thermostat.local"}}}],
    "paths": {
        "/status": {
            "get": {
                "summary": "Status",
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Status"}}}}
                },
            }
        },
        "/door": {"get": {"operationId": "doorState", "responses": {"default": {"content": {"text/plain": {}}}}}},
        "/zones/{zone}/target": {
            "parameters": [{"name": "zone", "in": "path"}],
            "put": {
                "operationId": "setTarget",
                "parameters": [{"name": "hold-for", "in": "query"}, {"name": "X-Trace", "in": "header"}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["celsius"],
                                "properties": {"celsius": {"type": "number"}, "mode": {"type": "string"}},
                            }
                        }
                    }
                },
            },
        },
    },
    "components": {
        "schemas": {
            "Status": {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {
                            "zones": {
                                "type": "array",
                                "items": {"type": "object", "properties": {"items": {"type": "integer"}}},
                            }
                        },
                    },
                    {"properties": {"online": {"type": ["boolean", "null"]}}},
                ]
            }
        }
    },
}


class Scripted(ha_rest_yaml.Console):
    """Answers each question from a list, in order, and keeps quiet."""

    def __init__(self, answers):
        self.answers = list(answers)

    def say(self, text=""):
        pass

    def ask(self, text):
        return self.answers.pop(0)


class GenerateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.spec = Path(cls.tmp.name, "openapi.json")
        cls.spec.write_text(json.dumps(SPEC), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_tool(self, *answers, options=()):
        console = Scripted(answers)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ha_rest_yaml.main([str(self.spec), *options], console), 0)
        self.assertEqual(console.answers, [], "not every answer was asked for")
        return out.getvalue()

    def test_sensor_from_a_json_value(self):
        # Header auth, the /status endpoint, zones[0].items, as returned.
        self.assertEqual(
            self.run_tool("1", "3", "1", "3", "1", options=["--auth-header", "X-API-Key", "--no-verify-ssl"]),
            """\
sensor:
  - platform: rest
    name: 'Status zones[0].items'
    resource: 'http://thermostat.local:8080/api/status'
    headers:
      X-API-Key: YOUR_HEADER_VALUE
    verify_ssl: false
    value_template: '{{ value_json.zones[0]["items"] }}'
""",
        )

    def test_binary_sensor_from_a_raw_response_through_a_template(self):
        # Basic auth, the /door endpoint, which is plain text, with a template.
        self.assertEqual(
            self.run_tool("2", "1", "2", "2", "{{ value == 'open' }}"),
            """\
binary_sensor:
  - platform: rest
    name: doorState
    resource: 'http://thermostat.local:8080/api/door'
    authentication: basic
    username: YOUR_USERNAME
    password: YOUR_PASSWORD
    value_template: '{{ value == ''open'' }}'
""",
        )

    def test_command_with_chosen_optional_parameters(self):
        # Digest auth, the PUT endpoint, with the query and body optionals.
        self.assertEqual(
            self.run_tool("3", "2", "3", "1, 3"),
            """\
rest_command:
  set_target:
    url: 'http://thermostat.local:8080/api/zones/{{ zone | urlencode }}/target?hold-for={{ hold_for | urlencode }}'
    method: put
    authentication: digest
    username: YOUR_USERNAME
    password: YOUR_PASSWORD
    payload: '{"celsius": {{ celsius | tojson }}, "mode": {{ mode | tojson }}}'
    content_type: application/json
""",
        )

    def test_base_url_falls_back_to_where_the_document_came_from(self):
        # A FastAPI document names no server, or only a path.
        source = "http://device.local:8000/openapi.json"
        self.assertEqual(ha_rest_yaml.Spec({}, source).base_url(), "http://device.local:8000")
        relative = {"servers": [{"url": "/api/v1/"}]}
        self.assertEqual(ha_rest_yaml.Spec(relative, source).base_url(), "http://device.local:8000/api/v1")
        nested = "http://device.local:8000/spec/openapi.json"
        self.assertEqual(ha_rest_yaml.Spec({"servers": [{"url": "v1"}]}, nested).base_url(), "http://device.local:8000/spec/v1")
        self.assertEqual(ha_rest_yaml.Spec({}).base_url(), "http://HOST")


if __name__ == "__main__":
    unittest.main()
