"""The RESTful sensor: `platform: rest` under `sensor:`.

Checked against homeassistant/components/rest/schema.py and sensor.py at the
2026.9.2 tag. The platform polls with GET or POST only, spelled in capitals,
and a sensor's state is the whole response body unless a value_template picks
something out of it. Templates see the body as `value`, and as `value_json`
when it parses as JSON.

The value selection lives here and the binary sensor borrows it, because the
two platforms read a response the same way and differ only in what the state
means.
"""

from __future__ import annotations

import json
import re

METHODS = ("get", "post")


def generate(endpoints, base_url, extra, console):
    config = entity(endpoints, base_url, extra, console, "sensor")
    return config and {"sensor": [config]}


def entity(endpoints, base_url, extra, console, kind, hint=""):
    endpoints = [endpoint for endpoint in endpoints if endpoint.method in METHODS]
    if not endpoints:
        console.say(f"The document has no GET or POST endpoints, and a RESTful {kind} can call nothing else.")
        return None
    endpoint = endpoints[console.choose("Which endpoint?", [e.label() for e in endpoints])]

    expression = label = None
    if endpoint.values:
        labels = [label for label, _ in endpoint.values]
        label, expression = endpoint.values[console.choose("Which value?", labels)]
        label = label.rsplit(" (", 1)[0]

    how = console.choose("Use the value as returned, or through a template?", ["As returned", "Through a template"])
    if how == 0:
        template = f"{{{{ {expression} }}}}" if expression else None
    else:
        console.say("The response body is available to the template as value, and as value_json when it is JSON.")
        if expression:
            console.say(f"The value chosen is {expression}")
        if hint:
            console.say(hint)
        template = console.ask("Template:")

    config = {
        "platform": "rest",
        "name": " ".join(part for part in (endpoint.title or endpoint.label(), label) if part),
        "resource": base_url + placeholders(endpoint.path),
    }
    if endpoint.method == "post":
        config["method"] = "POST"
    required = [p for p in endpoint.params if p.required]
    params = {p.name: p.placeholder for p in required if p.location == "query"}
    headers = {p.name: p.placeholder for p in required if p.location == "header"}
    if params:
        config["params"] = params
    if endpoint.method == "post" and endpoint.body_type:
        body = [p for p in required if p.location == "body"]
        if endpoint.json_request:
            config["payload"] = json.dumps({p.name: p.placeholder for p in body})
        else:
            config["payload"] = "REPLACE_PAYLOAD"
        headers["Content-Type"] = endpoint.body_type
    if headers:
        config["headers"] = headers
    for key, value in extra.items():
        config[key] = {**config.get(key, {}), **value} if key == "headers" else value
    if template:
        config["value_template"] = template
    return config


def placeholders(path):
    """The path with each {parameter} replaced by a token to overwrite by hand."""
    return re.sub(r"\{([^}]*)\}", lambda m: "REPLACE_" + re.sub(r"\W", "_", m.group(1)).upper(), path)
