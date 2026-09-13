"""The RESTful command: a named entry under `rest_command:`.

Checked against homeassistant/components/rest_command/__init__.py at the
2026.9.2 tag. Each entry is keyed by a slug and becomes the action
rest_command.<slug>. Its url, payload and header values are templates rendered
with the data the action is called with, which is how parameters get in. There
is no params key, so query parameters are written into the url. The method is
one of get, patch, post, put or delete, and username and password must be
given together or not at all.
"""

from __future__ import annotations

import json
import re

METHODS = ("get", "patch", "post", "put", "delete")

# Names Jinja reads as a literal or an operator rather than a variable, kept in
# step with the copy in ha_rest_yaml.py.
JINJA_WORDS = {"true", "false", "none", "True", "False", "None", "and", "or", "not", "in", "is", "if", "else"}


def generate(endpoints, base_url, extra, console):
    endpoints = [endpoint for endpoint in endpoints if endpoint.method in METHODS]
    if not endpoints:
        console.say("The document has no GET, PATCH, POST, PUT or DELETE endpoints.")
        return None
    endpoint = endpoints[console.choose("Which endpoint?", [e.label() for e in endpoints])]

    optional = [p for p in endpoint.params if not p.required]
    picked = []
    if optional:
        labels = [f"{p.name} ({p.location})" for p in optional]
        picked = [optional[i] for i in console.choose_many("Which optional parameters should it include?", labels)]
    params = [p for p in endpoint.params if p.required or p in picked]

    url = base_url + re.sub(r"\{([^}]*)\}", lambda m: "{{ " + variable(m.group(1)) + " }}", endpoint.path)
    query = [f"{p.name}={{{{ {p.variable} }}}}" for p in params if p.location == "query"]
    if query:
        url += "?" + "&".join(query)
    config = {"url": url}
    if endpoint.method != "get":
        config["method"] = endpoint.method

    headers = {p.name: f"{{{{ {p.variable} }}}}" for p in params if p.location == "header"}
    for key, value in extra.items():
        if key == "headers":
            headers.update(value)
        else:
            config[key] = value
    if headers:
        config["headers"] = headers

    body = [p for p in params if p.location == "body"]
    if body:
        fields = ", ".join(f"{json.dumps(p.name)}: {{{{ {p.variable} | tojson }}}}" for p in body)
        config["payload"] = "{" + fields + "}"
    elif any(p.location == "raw body" for p in params):
        config["payload"] = "{{ payload }}"
    elif endpoint.body_required and endpoint.json_request:
        # A body that must be sent, with none of its properties chosen.
        config["payload"] = "{}"
    if "payload" in config:
        config["content_type"] = endpoint.body_type

    variables = sorted({p.variable for p in params} | set(re.findall(r"\{\{ (\w+) ", url)))
    if variables:
        console.say(f"Call rest_command.{endpoint.slug} with data for: {', '.join(variables)}")
    return {"rest_command": {endpoint.slug: config}}


def variable(name):
    """A parameter name as a Jinja variable, the way Param.variable spells it."""
    name = re.sub(r"\W", "_", name)
    if name in JINJA_WORDS:
        return name + "_"
    return f"_{name}" if name[:1].isdigit() else name
