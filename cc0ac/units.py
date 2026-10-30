"""
How readings are shown: [units] in config.ini.

The station keeps every reading in one set of units, the ones sources/__init__.py
FIELDS names (°F, mph, inHg, inches, miles), so the database, the uploads and the
records never change with this setting. It only changes what people read: the
pages (units.js, from the choice put in current.json and /history.json) and the
alert messages (show() here).

    [units]
    system = metric          ; us (the default), metric, or uk (°C, mph, hPa, mm, miles)
    pressure = inhg          ; any one kind can differ from the system
"""

from __future__ import annotations

import configparser
import logging

log = logging.getLogger("acurite")

# Each kind: its choices, and for each the factor from the stored unit (temperature
# has an offset too), the label, and how many decimal places to show.
KINDS = {
    "temperature": {"f": (1, 0, "°F", 1), "c": (5 / 9, -32 * 5 / 9, "°C", 1)},
    "wind": {"mph": (1, 0, " mph", 0), "kmh": (1.609344, 0, " km/h", 0), "ms": (0.44704, 0, " m/s", 1),
             "knots": (0.868976, 0, " kn", 0)},
    "pressure": {"inhg": (1, 0, " inHg", 2), "hpa": (33.8639, 0, " hPa", 1), "mmhg": (25.4, 0, " mmHg", 1)},
    "rain": {"in": (1, 0, " in", 2), "mm": (25.4, 0, " mm", 1)},
    "distance": {"mi": (1, 0, " mi", 0), "km": (1.609344, 0, " km", 0)},
}
SYSTEMS = {
    "us": {"temperature": "f", "wind": "mph", "pressure": "inhg", "rain": "in", "distance": "mi"},
    "metric": {"temperature": "c", "wind": "kmh", "pressure": "hpa", "rain": "mm", "distance": "km"},
    "uk": {"temperature": "c", "wind": "mph", "pressure": "hpa", "rain": "mm", "distance": "mi"},
}


def chosen(cfg: configparser.ConfigParser) -> dict[str, str]:
    """{kind: choice} from [units]; anything it doesn't know is named in the log and left as the system's."""
    system = cfg.get("units", "system", fallback="us").strip().lower() or "us"
    if system not in SYSTEMS:
        log.warning("[units] system = %s isn't one of %s; showing us units", system, ", ".join(SYSTEMS))
        system = "us"
    out = dict(SYSTEMS[system])
    for kind, choices in KINDS.items():
        want = cfg.get("units", kind, fallback="").strip().lower()
        if want and want not in choices:
            log.warning("[units] %s = %s isn't one of %s; left as %s", kind, want, ", ".join(choices), out[kind])
        elif want:
            out[kind] = want
    return out


def convert(kind: str, value: float, units: dict, change: bool = False) -> float:
    """A stored value in the chosen unit; change=True for a difference, which takes no offset."""
    factor, offset, _, _ = KINDS[kind][units[kind]]
    return value * factor + (0 if change else offset)


def show(kind: str, value: float, units: dict, places: int | None = None, change: bool = False) -> str:
    """e.g. show("temperature", 31.46, {...}) is "-0.3°C"; change=True signs it, as "+0.05 inHg"."""
    _, _, label, default = KINDS[kind][units[kind]]
    v = convert(kind, value, units, change)
    text = f"{v:+.{default if places is None else places}f}" if change else f"{v:.{default if places is None else places}f}"
    return text + label
