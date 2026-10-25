"""
Calibration: a fixed correction for a sensor that reads a little off.

[calibration] in config.ini has one line per sensor, by the ID [sensors] uses:

    [calibration]
    00001111 = temp_f -1.5, humidity_pct +3
    00001234 = wind_mph x1.1, rain_day_in x0.95, wind_dir_deg +20

Each part names a field and either an amount to add (+ or -) or a number to
multiply by (x). A field may be named in the unit you measured the error in:
temp_c, pressure_hpa, pressure_mmhg, rain_day_mm, wind_kmh, wind_ms; the amount
is turned into the station's own units (sources/__init__.py FIELDS). Rain takes
only x, since an amount added would rain on every reading.

It is applied as each reading arrives, before it is stored, shown or sent on, so
the database, the uploads and the records all hold the corrected value; what the
sensor sent stays as it was in the raw logs and the database's raw column. Dew
point, heat index, wind chill and feels-like are worked out again from the
corrected temperature, humidity and wind. Changing a line corrects readings from
then on; the ones already stored stay as they were.
"""

from __future__ import annotations

import configparser
import logging
import re

import sensor_config
from sources import derive

log = logging.getLogger("acurite")

# Fields worked out from others: never corrected directly, worked out again instead.
DERIVED = ("dew_point_f", "heat_index_f", "wind_chill_f", "feels_like_f")
# Fields that are numbers a sensor measures, and so can be off.
MEASURED = ("temp_f", "humidity_pct", "wind_mph", "wind_avg_mph", "wind_gust_mph", "wind_dir_deg",
            "wind_gust_dir_deg", "rain_hour_in", "rain_day_in", "pressure_inhg", "uv_index", "light_lux",
            "last_strike_mi")
# A field named in another unit: its ending, the station's ending, and what one of
# it is in the station's unit (an amount is multiplied by this; a factor is not).
OTHER_UNITS = (("_c", "_f", 9 / 5), ("_hpa", "_inhg", 1 / 33.8639), ("_mmhg", "_inhg", 1 / 25.4),
               ("_mm", "_in", 1 / 25.4), ("_kmh", "_mph", 1 / 1.609344), ("_ms", "_mph", 1 / 0.44704),
               ("_km", "_mi", 1 / 1.609344))
PART = re.compile(r"([a-z_]+)\s*([+-]|x)\s*(\d+(?:\.\d+)?)")
NEVER_BELOW_ZERO = ("wind_mph", "wind_avg_mph", "wind_gust_mph", "rain_hour_in", "rain_day_in", "uv_index",
                    "light_lux", "last_strike_mi")


def _field(name: str) -> tuple[str, float] | None:
    """The station's field for a name, and what one of the name's unit is in it."""
    if name in MEASURED:
        return name, 1.0
    for other, own, one in OTHER_UNITS:
        if name.endswith(other) and name[: -len(other)] + own in MEASURED:
            return name[: -len(other)] + own, one
    return None


def parse(text: str) -> tuple[dict[str, tuple[str, float]], list[str]]:
    """One sensor's line → ({field: ("+", amount) or ("x", factor)}, problems)."""
    rules, problems = {}, []
    for part in (p.strip().lower() for p in text.split(",")):
        if not part:
            continue
        m = PART.fullmatch(part)
        named = m and _field(m.group(1))
        if not m:
            problems.append(f"'{part}' isn't a field and +, - or x with a number, like temp_f -1.5")
        elif not named:
            problems.append(f"'{m.group(1)}' isn't a field a sensor measures (fields: {', '.join(MEASURED)})")
        elif m.group(2) != "x" and named[0].startswith("rain_"):
            problems.append(f"'{part}': rain can only be multiplied, like {m.group(1)} x1.05")
        elif m.group(2) == "x":
            rules[named[0]] = ("x", float(m.group(3)))
        else:
            rules[named[0]] = ("+", float(m.group(2) + m.group(3)) * named[1])
    return rules, problems


def rules(cfg: configparser.ConfigParser) -> dict[str, dict]:
    """{sensor_id: its rules} from each sensor's calibration = (or the older [calibration]);
    a part that can't be read is named in the log and left out."""
    out = {}
    for sensor_id, text in sensor_config.calibration(cfg).items():
        found, problems = parse(text)
        for p in problems:
            log.warning("calibration for %s: %s; left out", sensor_id, p)
        if found:
            out[sensor_id.strip().lower()] = found
            log.info("Calibration for %s: %s", sensor_id, text.strip())
    return out


def apply(fields: dict, sensor_rules: dict | None) -> dict:
    """The reading's fields corrected by one sensor's rules (the same dict when it has none)."""
    if not sensor_rules:
        return fields
    out = dict(fields)
    changed = set()
    for name, (how, amount) in sensor_rules.items():
        v = out.get(name)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        v = v * amount if how == "x" else v + amount
        if name in ("wind_dir_deg", "wind_gust_dir_deg"):
            v %= 360
        elif name == "humidity_pct":
            v = min(100.0, max(0.0, v))
        elif name in NEVER_BELOW_ZERO:
            v = max(0.0, v)
        out[name] = round(v, 4)
        changed.add(name)
    # Worked out again only with the temperature to work from; a message without
    # it (a 5-in-1 sends wind and temperature by turns) keeps what came with it.
    if changed & {"temp_f", "humidity_pct", "wind_mph"} and isinstance(out.get("temp_f"), (int, float)):
        for name in DERIVED:
            out.pop(name, None)
        out = derive(out)
    return out
