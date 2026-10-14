"""The RESTful command: a named entry under `rest_command:`.

Checked against homeassistant/components/rest_command/__init__.py at the
2026.9.2 tag. Each entry is keyed by a slug and becomes the action
rest_command.<slug>. Its url, payload and header values are templates rendered
with the data the action is called with, which is how parameters get in. There
is no params key, so query parameters are written into the url. The method is
one of get, patch, post, put or delete, and username and password must be
given together or not at all.

A variable the action is not given is undefined in those templates. It renders
as an empty string, or fails tojson outright, so an optional parameter is only
optional once the template leaves it out when it is missing.
"""

from __future__ import annotations

import json
import re

import command_script

METHODS = ("get", "patch", "post", "put", "delete")


def generate(endpoints, base_url, extra, console):
    endpoints = [endpoint for endpoint in endpoints if endpoint.method in METHODS]
    if not endpoints:
        console.say("The document has no GET, PATCH, POST, PUT or DELETE endpoints.")
        return None
    endpoint = endpoints[console.choose("Which endpoint?", [e.label() for e in endpoints])]

    optional = [p for p in endpoint.params if not p.required]
    picked = []
    if optional:
        labels = [f"{p.name} ({p.location}{', included with any body field' if p.needed_with_body else ''})" for p in optional]
        picked = [optional[i] for i in console.choose_many("Which optional parameters should it include?", labels)]
    body_sent = any(p.location == "body" and (p.required or p in picked) for p in endpoint.params)
    params = [p for p in endpoint.params if p.required or p in picked or (body_sent and p.needed_with_body)]
    # The ones every call must supply. The rest are left out when the action is not given them.
    needed = [p for p in params if p.required or (body_sent and p.needed_with_body)]

    path_variables = {p.name: p.variable for p in endpoint.params if p.location == "path"}
    # Values reaching the url are encoded, so an & or # in one cannot add a query
    # parameter or start a fragment once Home Assistant parses what it rendered.
    url = base_url + re.sub(
        r"\{([^}]*)\}", lambda m: "{{ " + path_variables[m.group(1)] + " | urlencode }}", endpoint.path
    )
    query = [p for p in params if p.location == "query"]
    if any(p.repeated for p in query):
        url += "?" + query_pairs(query)
    elif query and all(p in needed for p in query):
        url += "?" + "&".join(f"{p.name}={{{{ {query_value(p)} | urlencode }}}}" for p in query)
    elif query:
        url += "?{{ " + given(query, query_value) + " | urlencode }}"
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
    if body and all(p in needed for p in body):
        fields = ", ".join(f"{json.dumps(p.name)}: {{{{ {p.variable} | tojson }}}}" for p in body)
        config["payload"] = "{" + fields + "}"
    elif body:
        config["payload"] = "{{ " + given(body) + " | tojson }}"
    elif raw := [p for p in params if p.location == "raw body"]:
        # A JSON body is serialised, or a list or mapping would render as Python.
        encode = " | tojson" if endpoint.json_request else ""
        config["payload"] = f"{{{{ {raw[0].variable}{encode} }}}}"
        if raw[0] not in needed:
            config["payload"] = f"{{% if {raw[0].variable} is defined %}}{config['payload']}{{% endif %}}"
    elif endpoint.body_required and endpoint.json_request:
        # A body that must be sent, with none of its properties chosen.
        config["payload"] = "{}"
    if "payload" in config:
        config["content_type"] = endpoint.body_type

    # Home Assistant registers its own rest_command.reload after the configured
    # commands, which would replace a command of that name.
    key = "reload_command" if endpoint.slug == "reload" else endpoint.slug
    script_key, script = command_script.entry(endpoint, key, params, needed)
    if needed:
        console.say(f"Call rest_command.{key} with data for: {', '.join(sorted(p.variable for p in needed))}")
    if optional := sorted(p.variable for p in params if p not in needed):
        console.say(f"It can also be given data for: {', '.join(optional)}")
    console.say(f"Or run script.{script_key}" + (", which asks for each as a field." if params else "."))
    return {"rest_command": {key: config}, "script": {script_key: script}}


def given(params, value=lambda p: p.variable):
    """A Jinja dict of each parameter's name and value, leaving out any the action was not given."""
    pairs = ", ".join(f"{json.dumps(p.name)}: {value(p)}" for p in params)
    return f'dict({{{pairs}}}.items() | rejectattr(1, "undefined"))'


def query_value(p):
    """The Jinja expression for a query parameter's value, joining up an array sent as one parameter."""
    v = p.variable
    if not p.delimiter:
        return v
    return f"({v} | join({json.dumps(p.delimiter)}) if {v} is defined and {v} is iterable and {v} is not string else {v})"


def query_pairs(query):
    """Jinja writing the query string from name and value pairs, one pair for each value of a repeated array.

    A dict cannot hold the same name twice, so the pairs are gathered in a
    namespace instead. Every variable is read in the first statement, before
    the template sets a name of its own, so a parameter called q or name is not
    hidden by one. A single value given for a repeated array is sent as is.
    """
    rows = ", ".join(f"({json.dumps(p.name)}, {query_value(p)}, {'true' if p.repeated else 'false'})" for p in query)
    return (
        f"{{% set query = [{rows}] %}}{{% set q = namespace(pairs=[]) %}}"
        "{% for name, value, many in query if value is defined %}"
        "{% for item in (value if many and value is iterable and value is not string else [value]) %}"
        "{% set q.pairs = q.pairs + [(name, item)] %}"
        "{% endfor %}{% endfor %}{{ q.pairs | urlencode }}"
    )
