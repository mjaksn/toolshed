"""Tests for ha_rest_yaml.py. Standard library only, so nothing to install.

    python -m unittest

One small document and one run per kind of YAML, each answering the questions
from a script and comparing the whole snippet, plus a second command, the
naming of variables and the type of a value whose schema allows several.
Between them they cover a JSON response reached through $ref and allOf, a
plain text response, parameters in the path, query, header and body, optional
ones left out when not given, query arrays repeated or joined and objects
spread by property, the script beside a command with its field types, defaults
and conversions, all three kinds of authentication, and the command line
options.
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
                "parameters": [
                    {"name": "hold-for", "in": "query"},
                    {"name": "X-Trace", "in": "header"},
                    {"name": "zones", "in": "query", "explode": False, "schema": {"type": "array"}},
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["celsius"],
                                "properties": {"celsius": {"type": "number"}, "mode": {"type": "string", "default": "auto"}},
                            }
                        }
                    }
                },
            },
        },
        "/fan": {
            "post": {
                "operationId": "toggle",
                "parameters": [
                    {"name": "dry", "in": "query", "required": True, "schema": {"type": "boolean"}},
                    {"name": "tags", "in": "query", "required": True, "schema": {"type": "array"}},
                    {"name": "filter", "in": "query", "required": True, "style": "deepObject", "schema": {"type": "object"}},
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["speed", "on"],
                                "properties": {
                                    "speed": {"type": "integer", "description": "Percent.", "default": 50},
                                    "on": {"type": "boolean", "default": True},
                                    "label": {"type": "string"},
                                },
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
        # Digest auth, the PUT endpoint, with the query, header and body optionals,
        # which the query string and payload leave out when they are not given,
        # and a query array joined into one value.
        self.assertEqual(
            self.run_tool("3", "2", "3", "1, 2, 3, 4"),
            """\
rest_command:
  set_target:
    url: 'http://thermostat.local:8080/api/zones/{{ zone | urlencode }}/target?{{ dict({"hold-for": hold_for, "zones": (zones | join(",") if zones is defined and zones is iterable and zones is not string else zones)}.items() | rejectattr(1, "undefined")) | urlencode }}'
    method: put
    authentication: digest
    username: YOUR_USERNAME
    password: YOUR_PASSWORD
    headers:
      X-Trace: '{{ X_Trace }}'
    payload: '{{ dict({"celsius": celsius, "mode": mode}.items() | rejectattr(1, "undefined")) | tojson }}'
    content_type: application/json
script:
  set_target:
    alias: setTarget
    description: 'Calls rest_command.set_target, PUT /zones/{zone}/target.'
    fields:
      zone:
        name: zone
        description: 'Path parameter.'
        required: true
        selector:
          text: {}
      hold_for:
        name: hold-for
        description: 'Query parameter.'
        required: false
        selector:
          text: {}
      X_Trace:
        name: X-Trace
        description: Header.
        required: false
        selector:
          text: {}
      zones:
        name: zones
        description: 'Query parameter, array.'
        required: false
        selector:
          text: {}
      celsius:
        name: celsius
        description: 'Body property, number.'
        required: true
        selector:
          text: {}
      mode:
        name: mode
        description: 'Body property, string.'
        required: false
        default: auto
        selector:
          text: {}
    sequence:
      - action: rest_command.set_target
        data: '{{ dict({"zone": zone, "hold_for": hold_for, "X_Trace": X_Trace, "zones": zones | from_json if zones is string else zones, "celsius": celsius | float if celsius is defined, "mode": mode}.items() | rejectattr(1, "undefined")) }}'
""",
        )

    def test_command_script_with_boolean_fields(self):
        # Header auth, the POST endpoint called toggle, with no optionals. A
        # script cannot be called toggle, booleans get the boolean selector, the
        # query array repeats its name for each value, and the query object
        # sends each property as filter[property].
        self.assertEqual(
            self.run_tool("3", "3", "4", ""),
            """\
rest_command:
  toggle:
    url: 'http://thermostat.local:8080/api/fan?{% set query = [("dry", dry, ""), ("tags", tags, "each"), ("filter", filter, "deep")] %}{% set q = namespace(pairs=[]) %}{% for name, value, spread in query if value is defined %}{% if spread == "each" and value is iterable and value is not string and value is not mapping %}{% for item in value %}{% set q.pairs = q.pairs + [(name, item)] %}{% endfor %}{% elif spread in ("props", "deep") and value is mapping %}{% for key, item in value.items() %}{% set q.pairs = q.pairs + [(name ~ "[" ~ key ~ "]" if spread == "deep" else key, item)] %}{% endfor %}{% else %}{% set q.pairs = q.pairs + [(name, value)] %}{% endif %}{% endfor %}{{ q.pairs | urlencode }}'
    method: post
    headers:
      Authorization: YOUR_HEADER_VALUE
    payload: '{"speed": {{ speed | tojson }}, "on": {{ on | tojson }}}'
    content_type: application/json
script:
  toggle_command:
    alias: toggle
    description: 'Calls rest_command.toggle, POST /fan.'
    fields:
      dry:
        name: dry
        description: 'Query parameter, boolean.'
        required: true
        selector:
          boolean: {}
      tags:
        name: tags
        description: 'Query parameter, array.'
        required: true
        selector:
          text: {}
      filter:
        name: filter
        description: 'Query parameter, object.'
        required: true
        selector:
          text: {}
      speed:
        name: speed
        description: 'Body property, integer. Percent.'
        required: true
        default: '50'
        selector:
          text: {}
      'on':
        name: 'on'
        description: 'Body property, boolean.'
        required: true
        default: true
        selector:
          boolean: {}
    sequence:
      - action: rest_command.toggle
        data: '{{ dict({"dry": dry | bool | lower if dry is defined, "tags": tags | from_json if tags is string else tags, "filter": filter | from_json if filter is string else filter, "speed": speed | int if speed is defined, "on": on | bool if on is defined}.items() | rejectattr(1, "undefined")) }}'
""",
        )

    def test_variables_avoid_names_a_template_already_has(self):
        # Left out, a variable called dict or now would still be defined, as
        # the global, and so could not be omitted.
        params = [ha_rest_yaml.Param(name, "body", False) for name in ("dict", "now", "this", "if", "speed")]
        ha_rest_yaml.name_variables(params)
        self.assertEqual([p.variable for p in params], ["dict_", "now_", "this_", "if_", "speed"])

    def test_a_value_that_could_take_several_types_has_none(self):
        # With no one type, the script passes the value on as typed rather
        # than converting it to the first type the schema allows.
        spec = ha_rest_yaml.Spec({"components": {"schemas": {"Count": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}})
        cases = [
            ({"type": ["integer", "string"]}, ""),
            ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, ""),
            ({"anyOf": [{"type": "string"}, {}]}, ""),
            ({"allOf": [{"oneOf": [{"type": "integer"}, {"type": "string"}]}]}, ""),
            ({"allOf": [{"type": ["integer", "string"]}, {"type": "number"}]}, "integer"),
            ({"anyOf": [{"type": "number"}, {"type": "null"}]}, "number"),
            ({"type": ["boolean", "null"]}, "boolean"),
            ({"$ref": "#/components/schemas/Count"}, "integer"),
            ({"items": {"type": "string"}}, "array"),
        ]
        for schema, expected in cases:
            self.assertEqual(spec.value_type(schema), expected, schema)

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
