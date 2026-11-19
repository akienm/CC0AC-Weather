"""The alert rules: one row per alert, in [alert_rules] (the alerts page, /alerts, edits them).

    [alert_rules]
    ; name = kind | watching | trigger | stays on | flags | target
    lightning = lightning | any | 10 mi | 00:30:00 | replaces, over | all
    lightning close = lightning | any | 1 mi | 00:10:00 | replaces, over | ntfy

kind      what is watched (KINDS below).
watching  any, or one sensor's name or id (a receiver: radio, hub or console).
trigger   the threshold, in the station's own units (miles, °F, inHg, minutes);
          NWS: any, minor, moderate, severe or extreme (that or worse).
stays on  until clears: while it is so; hh:mm:ss (or minutes): that long after the
          last time it was so; once: one message each time it happens, never "over".
          A Station row is always once; its hh:mm:ss is how long the dashboard shows
          the message (once: an hour).
flags     replaces: rows of one kind take turns, one alert standing at a time (a
          stricter row says "closer", "colder" ...; one "over" when every row of
          the kind has ended); new (the default): a row on its own. over: a message
          when it ends. repeat:hh:mm:ss: say it again that often while it stands.
          off: the row is kept but does nothing.
target    all, or any of ntfy, email, dashboard (a banner on the dashboard).

Without an [alert_rules] section the rows come from the settings before it
(weather, frost_f, lightning_mi ..., notices), so an upgrade alerts as before.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field

SECTION = "alert_rules"
TARGETS = ("ntfy", "email", "dashboard")
SEVERITIES = ("minor", "moderate", "severe", "extreme")
CLEARS, ONCE = 0.0, None     # stays on: 0 s after it stops being so; None: once
SHOWN_ONCE_S = 3600.0        # a once-only message stays on the dashboard this long, unless its row says


@dataclass(frozen=True)
class Kind:
    label: str
    unit: str            # what the trigger is in; "" for a kind with no threshold
    direction: int       # -1: a smaller trigger is stricter (closer, colder); +1: larger; 0: none
    default: float | None
    stays: float | None  # the default stays-on
    help: str
    word: str = "worse"  # what a stricter row of a replaces group says


KINDS: dict[str, Kind] = {
    "lightning": Kind("Lightning", "mi", -1, 10, 30 * 60, "a strike within this many miles", "closer"),
    "frost": Kind("Frost", "°F", -1, 35, CLEARS, "outdoors at or below", "colder"),
    "heat": Kind("Heat", "°F", +1, 105, CLEARS, "feels like at or above", "hotter"),
    "storm": Kind("Storm", "inHg", +1, 0.06, CLEARS, "the barometer falls this much in 3 hours", "falling faster"),
    "nws": Kind("NWS alert", "", +1, None, CLEARS, "a National Weather Service alert this severe or worse", "more severe"),
    "readings": Kind("No readings", "min", +1, 30, CLEARS, "no reading from any sensor for", "still quiet"),
    "sensor": Kind("Sensor quiet", "min", +1, 30, CLEARS, "one sensor unheard while the others report, for", "still quiet"),
    "receiver": Kind("Receiver quiet", "min", +1, 5, CLEARS, "a receiver (radio, hub, console) hearing nothing for",
                     "still quiet"),
    "held_back": Kind("Held back", "", 0, None, CLEARS, "a reading held back as impossible"),
    "battery": Kind("Battery", "", 0, None, CLEARS, "a sensor's or the hub's battery reads low"),
    "upkeep": Kind("Upkeep", "", 0, None, CLEARS, "a disk or backup problem"),
    "service": Kind("Station", "", 0, None, ONCE, "the station started, stopped, or was updated"),
}


@dataclass
class Rule:
    name: str
    kind: str
    watching: str = "any"
    trigger: float | None = None      # NWS: the severity's rank, 1 (minor) .. 4 (extreme); 0 any
    stays: float | None = CLEARS
    replaces: bool = False
    over: bool = True
    repeat: float | None = None
    off: bool = False
    targets: frozenset = field(default_factory=lambda: frozenset(TARGETS))

    def watches(self, *names: object) -> bool:
        """Is this row about the sensor (or receiver) going by any of these names?"""
        w = self.watching.strip().lower()
        return w in ("", "any") or any(w == str(n).strip().lower() for n in names if n is not None)

    def stricter_than(self, other: "Rule") -> bool:
        d = KINDS[self.kind].direction
        a, b = self.trigger, other.trigger
        return bool(d) and a is not None and b is not None and (a - b) * d > 0


# ── Reading and writing a row ─────────────────────────────────────────────────

def duration(text: str) -> float:
    """hh:mm:ss, mm:ss, or plain minutes ("30", "30 min") → seconds."""
    t = text.strip().lower()
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", t)
    if m:
        a, b, c = int(m[1]), int(m[2]), m[3]
        return float(a * 3600 + b * 60 + int(c)) if c is not None else float(a * 60 + b)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?)?", t)
    if m:
        return float(m[1]) * 60
    raise ValueError(f"{text!r} isn't a time: write hh:mm:ss, or minutes")


def show_duration(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def say_duration(seconds: float) -> str:
    """For a message: 30 minutes, 1 hour, 1 hour 30 minutes."""
    m = round(seconds / 60)
    if m < 120 and m % 60:
        return f"{m:g} minute{'s' if m != 1 else ''}"
    h, m = divmod(m, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m else "")


def _trigger(kind: str, text: str) -> float | None:
    k, t = KINDS[kind], text.strip().lower()
    if kind == "nws":
        if t in ("", "any"):
            return 0
        if t not in SEVERITIES:
            raise ValueError(f"an NWS trigger is any, {', '.join(SEVERITIES)}, not {text!r}")
        return float(SEVERITIES.index(t) + 1)
    if not k.unit:
        if t not in ("", "any", "-"):
            raise ValueError(f"{k.label} has no threshold (write any), not {text!r}")
        return None
    if t in ("", "default"):
        return k.default
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*[a-z°%]*", t)
    if not m:
        raise ValueError(f"{k.label}'s trigger is a number of {k.unit}, not {text!r}")
    return float(m[1])


def parse(name: str, line: str) -> Rule:
    """One [alert_rules] line → a Rule; ValueError naming what is wrong."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) > 6:
        raise ValueError(f"{name}: more than six columns")
    parts += [""] * (6 - len(parts))
    kind_text, watching, trigger, stays, flags, target = parts
    kind = kind_text.lower().replace(" ", "_")
    if kind not in KINDS:
        raise ValueError(f"{name}: {kind_text!r} isn't a kind of alert ({', '.join(KINDS)})")
    k = KINDS[kind]
    rule = Rule(name=name, kind=kind, watching=watching or "any")
    try:
        rule.trigger = _trigger(kind, trigger)
        s = stays.lower()
        if kind == "service":
            # Said once each time, never "over": stays on is how long the dashboard shows it.
            if s in ("", "default", "once"):
                rule.stays = ONCE
            elif s in ("until clears", "clears", "until dropped", "until it clears"):
                raise ValueError("a Station message is said once, so it can't stay until it clears: "
                                 "write once (on the dashboard for an hour) or hh:mm:ss (that long)")
            else:
                rule.stays = duration(stays)
        elif s == "once":
            rule.stays = ONCE
        elif s in ("", "default"):
            rule.stays = k.stays
        elif s in ("until clears", "clears", "until dropped", "until it clears"):
            rule.stays = CLEARS
        else:
            rule.stays = duration(stays)
        words = [w for w in re.split(r"[,\s]+", flags.lower()) if w and w != "-"]
        if flags.strip():
            rule.over = "over" in words
        for w in words:
            if w == "replaces":
                rule.replaces = True
            elif w.startswith("repeat:"):
                rule.repeat = duration(w.split(":", 1)[1])
                if rule.repeat < 60:
                    raise ValueError("repeat is a minute or more")
            elif w == "off":
                rule.off = True
            elif w not in ("new", "over"):
                raise ValueError(f"{w!r} isn't a flag (new, replaces, over, repeat:hh:mm:ss, off)")
        t = [w for w in re.split(r"[,\s]+", target.lower()) if w]
        if not t or "all" in t:
            rule.targets = frozenset(TARGETS)
        else:
            bad = [w for w in t if w not in TARGETS]
            if bad:
                raise ValueError(f"{', '.join(bad)} isn't a target (all, {', '.join(TARGETS)})")
            rule.targets = frozenset(t)
    except ValueError as e:
        raise ValueError(f"{name}: {e}") from None
    if rule.stays is ONCE or kind == "service":
        rule.over = False
    return rule


def shown_for(rule: Rule) -> float:
    """How long a once-only message (a Station row's) stays on the dashboard, in seconds."""
    return SHOWN_ONCE_S if rule.stays is ONCE else float(rule.stays)


def _number(v: float) -> str:
    return f"{v:g}"


def show(rule: Rule) -> str:
    """A Rule → its [alert_rules] line."""
    k = KINDS[rule.kind]
    if rule.kind == "nws":
        trigger = "any" if not rule.trigger else SEVERITIES[int(rule.trigger) - 1]
    elif not k.unit or rule.trigger is None:
        trigger = "any"
    else:
        trigger = f"{_number(rule.trigger)} {k.unit}"
    stays = "once" if rule.stays is ONCE else "until clears" if not rule.stays else show_duration(rule.stays)
    flags = ["replaces" if rule.replaces else "new"] + (["over"] if rule.over else [])
    flags += [f"repeat:{show_duration(rule.repeat)}"] if rule.repeat else []
    flags += ["off"] if rule.off else []
    target = "all" if rule.targets == frozenset(TARGETS) else ", ".join(t for t in TARGETS if t in rule.targets)
    return " | ".join((rule.kind, rule.watching or "any", trigger, stays, ", ".join(flags), target))


# ── The rows in force ─────────────────────────────────────────────────────────

def _legacy_number(cfg: configparser.ConfigParser, key: str, default: float) -> float:
    try:
        return float(cfg.get("alerts", key, fallback="").strip() or default)
    except ValueError:
        return default


def from_settings(cfg: configparser.ConfigParser) -> list[Rule]:
    """The rows the settings before [alert_rules] mean, so an upgrade alerts as it did."""
    quiet = _legacy_number(cfg, "quiet_minutes", 30)
    rows = [Rule("no readings", "readings", trigger=quiet)]
    weather = {w.strip().lower() for w in cfg.get("alerts", "weather", fallback="").split(",") if w.strip()}
    if "frost" in weather:
        rows.append(Rule("frost", "frost", trigger=_legacy_number(cfg, "frost_f", 35)))
    if "heat" in weather:
        rows.append(Rule("heat", "heat", trigger=_legacy_number(cfg, "heat_f", 105)))
    if "storm" in weather:
        rows.append(Rule("storm", "storm", trigger=_legacy_number(cfg, "pressure_drop_inhg", 0.06)))
    if "lightning" in weather:
        rows.append(Rule("lightning", "lightning", trigger=_legacy_number(cfg, "lightning_mi", 10),
                         stays=_legacy_number(cfg, "lightning_minutes", 30) * 60))
    raw = cfg.get("alerts", "notices", fallback="service, sensors, held_back")
    notices = {w.strip().lower() for w in raw.split(",") if w.strip()}
    if "service" in notices:
        rows.append(Rule("station", "service", stays=ONCE, over=False))
    if "sensors" in notices:
        rows.append(Rule("sensor quiet", "sensor", trigger=quiet))
        rows.append(Rule("receiver quiet", "receiver", trigger=_legacy_number(cfg, "receiver_quiet_minutes", 5)))
    if "held_back" in notices:
        rows.append(Rule("held back", "held_back"))
    rows.append(Rule("battery", "battery"))
    rows.append(Rule("upkeep", "upkeep"))
    return rows


def _lines(cfg: configparser.ConfigParser) -> list[tuple[str, str]]:
    """The [alert_rules] rows as written. A row pointed into secrets.env (the setup page's
    private-settings move matched a row named station, key, user ... until 1.5.1 skipped
    the section) is read back through its reference; the alerts page's next save writes
    it out plainly again."""
    from cc0ac import private
    return [(name, private.get_raw(cfg, SECTION, name)) for name in cfg.options(SECTION)
            if name not in cfg.defaults()]


def rules(cfg: configparser.ConfigParser) -> list[Rule]:
    """Every row in force. A row that doesn't read is left out, and said so in problems()."""
    if not cfg.has_section(SECTION):
        return from_settings(cfg)
    out = []
    for name, line in _lines(cfg):
        try:
            out.append(parse(name, line))
        except ValueError:
            pass
    return out


def problems(cfg: configparser.ConfigParser) -> list[str]:
    """What is wrong with the [alert_rules] rows, one line each."""
    if not cfg.has_section(SECTION):
        return []
    out = []
    for name, line in _lines(cfg):
        try:
            parse(name, line)
        except ValueError as e:
            out.append(str(e))
    return out


def for_kind(cfg: configparser.ConfigParser, kind: str) -> list[Rule]:
    return [r for r in rules(cfg) if r.kind == kind and not r.off]


# ── The alerts page (/alerts) ─────────────────────────────────────────────────

NAME = re.compile(r"[a-z0-9][a-z0-9 _-]{0,39}")
COLUMNS = ("kind", "watching", "trigger", "stays", "flags", "target")
# [alerts] settings the rows take over: once [alert_rules] is saved they mean nothing,
# so they go (quiet_minutes stays: it is also how fresh a reading must be to count).
LEGACY = ("weather", "frost_f", "heat_f", "pressure_drop_inhg", "lightning_mi", "lightning_minutes",
          "notices", "receiver_quiet_minutes")


def _columns(name: str, line: str) -> dict:
    parts = [p.strip() for p in line.split("|")] + [""] * 6
    return {"name": name, **dict(zip(COLUMNS, parts))}


def page_state(cfg: configparser.ConfigParser) -> dict:
    """What the alerts page shows: the rows (as their columns), the kinds, and what is wrong."""
    if cfg.has_section(SECTION):
        rows = []
        for name, line in _lines(cfg):
            try:
                rows.append(_columns(name, show(parse(name, line))))
            except ValueError as e:
                rows.append({**_columns(name, line), "problem": str(e)})
    else:
        rows = [_columns(r.name, show(r)) for r in from_settings(cfg)]
    kinds = {k: {"label": v.label, "unit": v.unit, "help": v.help,
                 "trigger": ("any" if k == "nws" or not v.unit else f"{v.default:g} {v.unit}"),
                 "stays": "once" if v.stays is ONCE else "until clears" if not v.stays else show_duration(v.stays),
                 "stricter": v.word if v.direction else None}
             for k, v in KINDS.items()}
    return {"rows": rows, "from_settings": not cfg.has_section(SECTION), "kinds": kinds,
            "targets": list(TARGETS), "severities": list(SEVERITIES)}


def apply_rows(text: str, rows: list) -> str:
    """config.ini text with [alert_rules] made exactly these rows (each as the page
    sends it: name and the six columns). ValueError naming the first row wrong."""
    from cc0ac import wizard
    lines: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each row is an object")
        name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip().lower()
        if not NAME.fullmatch(name):
            raise ValueError(f"{name or 'a row'!r}: a name is letters, digits, spaces, - or _, up to 40")
        if name in lines:
            raise ValueError(f"two rows are called {name!r}")
        cells = [re.sub(r"[|\r\n]", " ", str(row.get(c) or "")).strip() for c in COLUMNS]
        lines[name] = show(parse(name, " | ".join(cells)))
    text = wizard.replace_entries(text, SECTION, lines)
    return wizard.set_values(text, "alerts", {k: None for k in LEGACY})
