"""The script written beside a RESTful command, an entry under `script:`.

Checked against homeassistant/components/script/config.py,
homeassistant/helpers/service.py and homeassistant/helpers/template at the
2026.9.2 tag, and the editor's src/components/ha-service-control.ts in
frontend 20260826.7, the release that tag pins. A script's fields are what the automation editor offers to fill
in when the script is run as an action, and each arrives in the script as a
variable of the same name, undefined when it was left blank. A field may carry
a name, a description, required, a default and a selector. Required only marks
it in the editor, and the default is filled in there when the script is added
as an action; Home Assistant applies neither when the script runs. A script may
not be called reload, turn_on, turn_off or toggle.

The action's data is one template rendering a dict, rather than a template per
key, for two reasons. A blank field can be left out of a dict, where a key of
its own would render as an empty string. And Home Assistant reads whatever a
template renders as a Python literal when it can, so text typed as 123 would
reach the command as a number; inside a rendered dict it keeps its quotes and
stays text. Values are converted to the type the schema asks for instead, where
the command puts them into JSON, and an array bound for the query is read as
JSON too, so the command can send each of its values. A value is passed on as
typed when the schema allows more than one type.
"""

from __future__ import annotations

import json

# Script object ids Home Assistant keeps for its own actions on scripts.
RESERVED = ("reload", "turn_on", "turn_off", "toggle")

LOCATIONS = {
    "path": "Path parameter",
    "query": "Query parameter",
    "header": "Header",
    "body": "Body property",
    "raw body": "Request body",
}


def entry(endpoint, command, params, needed):
    """The script's key, and its config, running rest_command.<command> with a field per parameter."""
    key = command + "_command" if command in RESERVED else command
    fields = {}
    for p in params:
        description = LOCATIONS[p.location] + (f", {p.type}." if p.type else ".")
        if p.description:
            description += " " + p.description
        fields[p.variable] = {
            "name": p.name,
            "description": description,
            "required": p in needed,
        }
        default = field_default(p)
        if default is not None:
            fields[p.variable]["default"] = default
        fields[p.variable]["selector"] = {"boolean" if p.type == "boolean" else "text": {}}
    action = {"action": f"rest_command.{command}"}
    if params:
        json_values = endpoint.json_request
        pairs = ", ".join(
            f'"{p.variable}": {value(p, json_values and p.location in ("body", "raw body"))}' for p in params
        )
        action["data"] = f'{{{{ dict({{{pairs}}}.items() | rejectattr(1, "undefined")) }}}}'
    config = {
        "alias": endpoint.title or endpoint.label(),
        "description": f"Calls rest_command.{command}, {endpoint.method.upper()} {endpoint.path}.",
    }
    if fields:
        config["fields"] = fields
    config["sequence"] = [action]
    return key, config


def field_default(param):
    """The schema's default as the field holds it: true or false for a boolean, else text as typed."""
    if param.type == "boolean":
        return param.default if isinstance(param.default, bool) else None
    if param.default is None or isinstance(param.default, str):
        return param.default
    return json.dumps(param.default)


def value(param, in_json):
    """The Jinja expression handing a field on to the command, as the type the command wants it.

    An expression on a blank field is guarded, so the field stays undefined and
    is left out rather than failing the conversion.
    """
    v = param.variable
    if param.type == "boolean":
        # Outside JSON a boolean is text, and Python would spell it True.
        return f"{v} | bool if {v} is defined" if in_json else f"{v} | bool | lower if {v} is defined"
    if param.type == "array" and param.location == "query":
        # Typed as JSON, as an array bound for JSON is, so each value reaches the url.
        return f"{v} | from_json if {v} is string else {v}"
    if not in_json:
        return v
    if param.type == "integer":
        return f"{v} | int if {v} is defined"
    if param.type == "number":
        return f"{v} | float if {v} is defined"
    if param.type in ("array", "object"):
        # Typed into a text field as JSON, or passed as a list or mapping already.
        return f"{v} | from_json if {v} is string else {v}"
    return v
