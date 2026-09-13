"""The RESTful binary sensor: `platform: rest` under `binary_sensor:`.

Checked against homeassistant/components/rest/schema.py and binary_sensor.py
at the 2026.9.2 tag. It takes the same request keys as the sensor, GET or POST
only, and none of the sensor's unit or state class keys. The state is on when
the value, after any value_template, is a nonzero integer or one of true, on,
open or yes in any case, and off otherwise.
"""

from __future__ import annotations

import rest_sensor

HINT = "The binary sensor is on when the template renders a nonzero integer, or true, on, open or yes in any case, and off otherwise."


def generate(endpoints, base_url, extra, console):
    config = rest_sensor.entity(endpoints, base_url, extra, console, "binary sensor", HINT)
    return config and {"binary_sensor": [config]}
