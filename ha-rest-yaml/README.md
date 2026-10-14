# ha-rest-yaml

Writes a Home Assistant YAML snippet for one endpoint of an HTTP API, working
from the API's OpenAPI 3 document in JSON. It asks a few numbered questions and
prints one of:

- a [RESTful sensor](https://www.home-assistant.io/integrations/sensor.rest),
  `platform: rest` under `sensor:`, whose state is one value from a response;
- a [RESTful binary sensor](https://www.home-assistant.io/integrations/binary_sensor.rest),
  the same under `binary_sensor:`, on or off from one value;
- a [RESTful command](https://www.home-assistant.io/integrations/rest_command),
  an entry under `rest_command:` that calls the endpoint as an action, and
  beside it a [script](https://www.home-assistant.io/integrations/script/)
  under `script:` that runs the command with a field for each parameter.

These are the per-platform integrations, not the combined
[RESTful integration](https://www.home-assistant.io/integrations/rest/) under a
top-level `rest:` key, which this does not write.

```
python ha_rest_yaml.py openapi.json > snippet.yaml
python ha_rest_yaml.py http://device.local:8000/openapi.json > snippet.yaml
```

Questions go to stderr and the snippet to stdout, so redirecting keeps only
the YAML. The document can be a file or an http or https URL to fetch it from.

The URL endpoints are written against is the document's first server. When
that is missing or only a path, as it usually is in a document FastAPI serves,
the host comes from the URL the document was fetched from, or is left as
`http://HOST` when it was read from a file.

| Option | Effect |
| --- | --- |
| `--base-url URL` | Use this instead of the URL worked out above. |
| `--no-verify-ssl` | Write `verify_ssl: false`, for a device with a self-signed certificate, and skip the certificate check when fetching the document. |
| `--auth-header NAME` | The header name HTTP header authentication uses, such as `X-API-Key`. `Authorization` otherwise. |

## What it asks

1. What to generate: sensor, binary sensor or command.
2. Which authentication: basic, digest or an HTTP header. Nothing is asked
   about credentials. The snippet carries `YOUR_USERNAME` and `YOUR_PASSWORD`,
   or an `Authorization` header set to `YOUR_HEADER_VALUE`, named otherwise
   with `--auth-header`.
3. Which endpoint, from a numbered list.

For a sensor or binary sensor, the list holds only GET and POST endpoints,
because those are the only methods the platform can poll with. When the success
response is JSON, it then lists every property in it with its path, such as
`zones[0].name`, and asks for one. Then it asks whether to use the value as
returned or through a template. As returned, a JSON value becomes a
`value_template` reaching that property, and a response that is not JSON is
used whole with no template at all. Through a template, whatever is typed is
used as the `value_template` unchanged. Home Assistant gives it the body as
`value`, and as `value_json` when the body is JSON, and the tool prints the
expression for the chosen property so it can be pasted in. Any required
parameters are filled with `REPLACE_` placeholders.

For a command, every endpoint using GET, PATCH, POST, PUT or DELETE is listed,
and when the endpoint has optional parameters a second list asks which to
include. Required ones are always included. Each parameter becomes a template
variable, filled from the data the action is called with, so a path parameter
`zone` turns the URL into `.../zones/{{ zone | urlencode }}/...`. Query
parameters go into the URL too, because `rest_command` has no `params` key.
Values in the URL pass through `urlencode`, so an `&` or `#` in one stays part
of the value. The properties of a JSON request body count as parameters as
well, and go into a JSON `payload`; once any of them is chosen, the ones the
body's schema requires come along too. Two parameters whose names would make
the same variable, such as a query and a header both called `id`, get the
location added to tell them apart. A parameter named like something every
template already has, such as `dict`, `min` or `now`, gets an underscore added,
as in `min_`, because a template would find the original name defined even
when the parameter was left out.

An optional parameter is left out of the request when the action is not given
it. When the query parameters or body properties included are all required,
each is written into the URL or the payload by itself, and a `/` in a path or
query value is left as it is. Once any of them is optional, the query string or
the payload is instead one template building a dict of only the variables the
action was given. Written that way, a query string encodes a `/` as well, and
is a bare `?` when none of its parameters is given. `rest_command` has no way
to leave a header out, so an optional header that is not given is sent empty.
The tool prints which variables the action needs and which it can also be
given.

## The script

Running the command from an automation means typing its data by hand in the
automation editor. Running the script instead puts a field in the editor for
each parameter, ready to fill in. The script is keyed like the command, with
`_command` added when that would be `turn_on`, `turn_off` or `toggle`, which
Home Assistant keeps for its own script actions.

Each parameter the command includes becomes a field, keyed by its variable,
named after the parameter, and described by where it goes, its type, and any
description the document gives it. A boolean gets a boolean selector and
anything else a text one. A field is required when the command must be given
it, which matches the document: required parameters, and the body properties
the schema requires once the body is sent. Home Assistant uses that only to
mark the field in the editor; it still runs the script without it.

The script hands its fields to the command as one template rendering a dict.
That leaves out the blank ones, and keeps text as text, where a template for
each key would turn a typed `123` into a number. A value bound for JSON is
converted to the type the schema gives it: `int` for an integer, `float` for a
number, `bool` for a boolean, and `from_json` for an array or object typed as
text. A boolean bound for the URL or a header becomes `true` or `false`, where
Python would write `True`. A value that does not convert stops the script with
an error rather than sending something else. A schema allowing more than one
type, through a list of types or a `oneOf` or `anyOf` whose branches differ,
gives no one type to convert to, so its value gets a text field and is passed
on as typed. A `null` alongside one other type does not count.

The default `configuration.yaml` already has `script: !include scripts.yaml`,
and a second `script:` key will not load. Either move the entry under
`script:` into `scripts.yaml`, without the `script:` line itself, or rename
the key to something like `script rest:`, which Home Assistant reads as more
scripts.

## What it trusts

The document, and the person answering. Only the paths, parameters, request
bodies, success responses and first server are read, and nothing is validated.
The success response is the lowest 2xx, else `default`, else whichever
response is there. A JSON schema is walked through local `$ref`s, `allOf`, the
first branch of `oneOf` or `anyOf`, and array `items`; when it yields nothing,
the response's example is walked instead. Templates are not checked.

The keys written were checked against the Home Assistant source at the
2026.9.2 tag, in `homeassistant/components/rest`,
`homeassistant/components/rest_command` and `homeassistant/components/script`,
rather than against the documentation pages. How the templates render, and
what a script's data becomes, was checked against
`homeassistant/helpers/template` and `homeassistant/helpers/service.py` at the
same tag.

## What it needs

Python 3.9 or later and nothing else, plus a route to the device when the
document is given as a URL.

## Files

`ha_rest_yaml.py` is the one to run. It reads the document, asks the questions
common to all three, and writes the YAML. `rest_sensor.py`,
`rest_binary_sensor.py` and `rest_command.py` each build the config for one
integration, and `command_script.py` builds the script beside a command.

## Tests

```
python3 -m unittest
```

7 tests, standard library only: one run of each kind against a small document,
comparing the whole snippet, a second command whose script has boolean fields,
one for the variable names kept clear of template globals, one for the type of
a value whose schema allows several, and one for where the base URL comes from.
Nothing is fetched. The tests compare the templates as written; they do not
render them. `check.sh` runs exactly that and is what CI runs, on
`ubuntu-latest`, per `ci.json`.
