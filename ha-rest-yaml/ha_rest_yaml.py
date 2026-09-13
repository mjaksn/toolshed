#!/usr/bin/env python3
"""Generate Home Assistant REST YAML from an OpenAPI 3 JSON document.

    python ha_rest_yaml.py openapi.json > snippet.yaml
    python ha_rest_yaml.py http://device.local:8000/openapi.json > snippet.yaml

Asks what to generate (a RESTful sensor, a RESTful binary sensor or a RESTful
command), which authentication to leave placeholders for, and then which
endpoint and value or parameters to use. Questions go to stderr and the YAML
goes to stdout, so the snippet can be redirected into a file.

The document is trusted rather than validated. Only paths, parameters, request
bodies, response bodies and the first server are read, and whatever is missing
is worked around rather than reported. A document fetched from a URL supplies
the host when it names no server, as a FastAPI one usually does not.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import rest_binary_sensor
import rest_command
import rest_sensor

KINDS = [
    ("RESTful sensor", rest_sensor),
    ("RESTful binary sensor", rest_binary_sensor),
    ("RESTful command", rest_command),
]

AUTH_KINDS = ["Basic", "Digest", "HTTP header"]

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Names Jinja resolves to something other than the dictionary key when written
# after a dot: the methods of dict itself, and its literal keywords.
UNSAFE_ATTRIBUTES = set(dir(dict)) | {"true", "false", "none", "True", "False", "None"}

# Names Jinja reads as a literal or an operator rather than a variable.
JINJA_WORDS = {"true", "false", "none", "True", "False", "None", "and", "or", "not", "in", "is", "if", "else"}


@dataclass
class Param:
    name: str
    # path, query or header; body for a property of a JSON request body; raw
    # body for a request body whose properties are unknown.
    location: str
    required: bool

    @property
    def variable(self) -> str:
        """The name as a Jinja variable, for templates filled from service data."""
        slug = re.sub(r"\W", "_", self.name)
        if slug in JINJA_WORDS:
            return slug + "_"
        return f"_{slug}" if slug[:1].isdigit() else slug

    @property
    def placeholder(self) -> str:
        return "REPLACE_" + re.sub(r"\W", "_", self.name).upper()


@dataclass
class Endpoint:
    method: str
    path: str
    title: str
    slug: str
    params: list[Param] = field(default_factory=list)
    body_type: str | None = None  # the request body's media type, if it has one
    json_request: bool = False
    # Each value in a JSON response body: a label for the menu, and the Jinja
    # expression reaching it. Empty when the response is not JSON.
    values: list[tuple[str, str]] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.method.upper()} {self.path}  {self.title}".rstrip()


class Spec:
    def __init__(self, document: dict, source: str = ""):
        self.document = document
        self.source = source  # the URL the document was fetched from, if it was

    def resolve(self, node, seen=()):
        """Follow local $ref pointers until a real node is reached."""
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if ref in seen or not ref.startswith("#/"):
                return {}
            seen = (*seen, ref)
            node = self.document
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                node = node.get(part, {}) if isinstance(node, dict) else {}
        return node if isinstance(node, dict) else {}

    def base_url(self) -> str:
        """The first server's URL, made absolute against wherever the document came from."""
        servers = self.document.get("servers") or [{}]
        url = servers[0].get("url") or ""
        for name, variable in (servers[0].get("variables") or {}).items():
            url = url.replace("{" + name + "}", str(variable.get("default", "")))
        if "://" not in url:
            # Relative to where the document was served, as OpenAPI resolves it.
            url = urljoin(self.source or "http://HOST/", url or "/")
        return url.rstrip("/")

    def endpoints(self) -> list[Endpoint]:
        found = []
        for path, item in (self.document.get("paths") or {}).items():
            item = self.resolve(item)
            shared = item.get("parameters") or []
            for method in HTTP_METHODS:
                operation = item.get(method)
                if isinstance(operation, dict):
                    found.append(self.endpoint(method, path, operation, shared))
        return found

    def endpoint(self, method, path, operation, shared) -> Endpoint:
        operation_id = operation.get("operationId") or ""
        title = operation.get("summary") or operation_id
        endpoint = Endpoint(method, path, title, slug(operation_id or f"{method}_{path}"))
        params = {}
        for raw in [*shared, *(operation.get("parameters") or [])]:
            raw = self.resolve(raw)
            location = raw.get("in")
            if raw.get("name") and location in ("path", "query", "header"):
                required = location == "path" or bool(raw.get("required"))
                params[(raw["name"], location)] = Param(raw["name"], location, required)
        endpoint.params = list(params.values())

        body = self.resolve(operation.get("requestBody"))
        media_type, media = self.pick_media(body.get("content"))
        if media_type:
            endpoint.body_type = media_type
            endpoint.json_request = is_json(media_type)
            schema = self.merged(media.get("schema"))
            if endpoint.json_request and schema.get("properties"):
                required = set(schema.get("required") or [])
                endpoint.params += [
                    Param(name, "body", name in required) for name in schema["properties"]
                ]
            else:
                required = bool(body.get("required"))
                endpoint.params.append(Param("payload", "raw body", required))

        response = self.resolve(self.success_response(operation.get("responses")))
        media_type, media = self.pick_media(response.get("content"))
        if media_type and is_json(media_type):
            found = []
            self.walk_schema(media.get("schema"), [], found, 0)
            if not found:
                example = media.get("example")
                if example is None:
                    examples = list((media.get("examples") or {}).values())
                    example = self.resolve(examples[0]).get("value") if examples else None
                walk_example(example, [], found, 0)
            endpoint.values = [
                (f"{display_path(path)} ({kind})", jinja_path(path)) for path, kind in found
            ]
        return endpoint

    def success_response(self, responses):
        """The lowest 2xx response, else default, else whatever is there."""
        if not isinstance(responses, dict) or not responses:
            return None
        codes = sorted(code for code in responses if str(code).startswith("2"))
        if codes:
            return responses[codes[0]]
        return responses.get("default") or next(iter(responses.values()))

    def pick_media(self, content):
        if not isinstance(content, dict) or not content:
            return None, {}
        for media_type, media in content.items():
            if is_json(media_type):
                return media_type, self.resolve(media)
        media_type = next(iter(content))
        return media_type, self.resolve(content[media_type])

    def merged(self, schema, depth=0):
        """A schema with allOf folded in and the first oneOf or anyOf branch taken."""
        schema = self.resolve(schema)
        parts = [*schema.get("allOf", []), *(schema.get("oneOf") or schema.get("anyOf") or [])[:1]]
        if not parts or depth > 12:
            return schema
        result = {key: value for key, value in schema.items() if key not in ("allOf", "oneOf", "anyOf")}
        result["properties"] = dict(result.get("properties") or {})
        result["required"] = list(result.get("required") or [])
        for part in parts:
            part = self.merged(part, depth + 1)
            for key, value in part.items():
                if key == "properties":
                    result["properties"].update(value)
                elif key == "required":
                    result["required"] += value
                else:
                    result.setdefault(key, value)
        # Left out when empty, so a merge of scalar schemas is not taken for an object.
        return {key: value for key, value in result.items() if value or key not in ("properties", "required")}

    def walk_schema(self, schema, path, out, depth):
        if depth > 12:
            return
        schema = self.merged(schema)
        kind = schema.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), None)
        if kind == "array" or "items" in schema:
            if path:
                out.append((path, "array"))
            self.walk_schema(schema.get("items"), [*path, 0], out, depth + 1)
        elif kind == "object" or "properties" in schema:
            if path:
                out.append((path, "object"))
            for name, child in (schema.get("properties") or {}).items():
                self.walk_schema(child, [*path, name], out, depth + 1)
        elif path:
            out.append((path, kind or "value"))


def walk_example(value, path, out, depth):
    if depth > 12:
        return
    if isinstance(value, dict):
        if path:
            out.append((path, "object"))
        for name, child in value.items():
            walk_example(child, [*path, name], out, depth + 1)
    elif isinstance(value, list):
        if path:
            out.append((path, "array"))
        if value:
            walk_example(value[0], [*path, 0], out, depth + 1)
    elif path:
        kinds = {bool: "boolean", int: "integer", float: "number", str: "string"}
        out.append((path, kinds.get(type(value), "value")))


def is_json(media_type: str) -> bool:
    media_type = media_type.split(";")[0].strip().lower()
    return media_type.endswith(("/json", "+json"))


def display_path(path: list) -> str:
    text = ""
    for part in path:
        text += f"[{part}]" if isinstance(part, int) else ("." if text else "") + part
    return text


def jinja_path(path: list) -> str:
    """value_json followed by the path, in a form Jinja reads as dictionary access."""
    text = "value_json"
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        elif part.isidentifier() and part not in UNSAFE_ATTRIBUTES:
            text += "." + part
        else:
            text += "[" + json.dumps(part) + "]"
    return text


def slug(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "command"


def auth_fields(kind: int, header: str = "Authorization") -> dict:
    if kind == 2:
        return {"headers": {header: "YOUR_HEADER_VALUE"}}
    return {
        "authentication": "basic" if kind == 0 else "digest",
        "username": "YOUR_USERNAME",
        "password": "YOUR_PASSWORD",
    }


class Console:
    """Questions on stderr, answers from stdin."""

    def say(self, text: str = "") -> None:
        print(text, file=sys.stderr)

    def ask(self, text: str) -> str:
        print(text, end=" ", file=sys.stderr, flush=True)
        return input().strip()

    def choose(self, title: str, options: list[str]) -> int:
        self.say(title)
        for number, option in enumerate(options, 1):
            self.say(f"  {number}) {option}")
        while True:
            answer = self.ask(f"Choose 1-{len(options)}:")
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.say("Not one of the numbers listed.")

    def choose_many(self, title: str, options: list[str]) -> list[int]:
        self.say(title)
        for number, option in enumerate(options, 1):
            self.say(f"  {number}) {option}")
        while True:
            answer = self.ask("Numbers separated by commas or spaces, blank for none:")
            picks = [n for n in re.split(r"[,\s]+", answer) if n]
            if all(n.isdigit() and 1 <= int(n) <= len(options) for n in picks):
                return sorted({int(n) - 1 for n in picks})
            self.say("Not one of the numbers listed.")


def yaml_scalar(value) -> str:
    if isinstance(value, (dict, list)):
        return "{}" if isinstance(value, dict) else "[]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    plain = re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]*", text)
    if plain and text.lower() not in ("y", "yes", "n", "no", "on", "off", "true", "false", "null"):
        return text
    if "\n" in text or any(ord(c) < 32 for c in text):
        return json.dumps(text)
    return "'" + text.replace("'", "''") + "'"


def yaml_key(key: str) -> str:
    """A mapping key, through the same quoting as a value, so on or 123 stays a string."""
    return yaml_scalar(str(key))


def to_yaml(data, indent: int = 0) -> list[str]:
    """Block style YAML for nested dicts and lists of strings, numbers and booleans."""
    pad = " " * indent
    lines = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{yaml_key(key)}:")
                lines += to_yaml(value, indent + 2)
            else:
                lines.append(f"{pad}{yaml_key(key)}: {yaml_scalar(value)}")
    else:
        for item in data:
            if isinstance(item, (dict, list)) and item:
                nested = to_yaml(item, indent + 2)
                nested[0] = f"{pad}- {nested[0].lstrip()}"
                lines += nested
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")
    return lines


def load(source: str, verify: bool = True) -> Spec:
    """Read the document from a file, or fetch it when given a URL."""
    if not re.match(r"https?://", source, re.IGNORECASE):
        return Spec(json.loads(Path(source).read_text(encoding="utf-8-sig")))
    context = None if verify else ssl._create_unverified_context()
    request = Request(source, headers={"Accept": "application/json"})
    with urlopen(request, context=context, timeout=30) as response:
        return Spec(json.loads(response.read().decode("utf-8-sig")), response.geturl())


def main(argv: list[str] | None = None, console: Console | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Home Assistant RESTful sensor, binary sensor or"
        " command YAML snippet from an OpenAPI 3 JSON document."
    )
    parser.add_argument(
        "spec", help="the OpenAPI document, as JSON: a file, or an http or https URL to fetch it from"
    )
    parser.add_argument(
        "--base-url",
        help="the URL the endpoint paths are relative to; defaults to the"
        " document's first server, else the host the document was fetched"
        " from, else http://HOST",
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="write verify_ssl: false, for a device with a self-signed"
        " certificate, and skip the certificate check when fetching the document",
    )
    parser.add_argument(
        "--auth-header",
        default="Authorization",
        metavar="NAME",
        help="the header name HTTP header authentication uses, such as"
        " X-API-Key; defaults to Authorization",
    )
    args = parser.parse_args(argv)
    console = console or Console()

    spec = load(args.spec, verify=not args.no_verify_ssl)
    base_url = (args.base_url or spec.base_url()).rstrip("/")

    kind = console.choose("What should be generated?", [name for name, _ in KINDS])
    # Fields every config gets, whichever kind it is.
    extra = auth_fields(console.choose("Which authentication should it use?", AUTH_KINDS), args.auth_header)
    if args.no_verify_ssl:
        extra["verify_ssl"] = False
    config = KINDS[kind][1].generate(spec.endpoints(), base_url, extra, console)
    if config is None:
        return 1
    print("\n".join(to_yaml(config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
