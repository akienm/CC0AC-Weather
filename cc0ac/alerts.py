"""
Tell someone when readings stop ([alerts]).

    python3 acurite-capture.py --check        # run every minute
    python3 acurite-capture.py --test-alert   # send a test message now

The check runs as a process of its own, every minute, started by the boot loader (or,
for a station started some other way, by the station), so under the boot loader it
still speaks up when the program that records has hung or died. What it alerts on is
the rules table (alert_rules.py, [alert_rules], the /alerts page): no readings, a
sensor or receiver gone quiet, frost, heat, a falling barometer, lightning, Weather
Service alerts, batteries, disks and backups, the station starting and stopping. Each
row sends one message when it starts and, flagged over, one when it ends; in between
it says nothing (unless flagged repeat).

Ways to hear about it, any or all (a row's target picks among the first three):

  * ntfy: a free push-notification app (ntfy.sh). Install the app, subscribe to
    a topic name nobody else would guess, put the same name in ntfy_topic.
  * email, through any mail server that takes a login (for Gmail: an app
    password, smtp.gmail.com, port 587).
  * the dashboard: a banner, from <write_path>/alerts.js.
  * healthcheck_url: a dead-man's switch such as healthchecks.io. The check
    pings it while readings are fresh and pings <url>/fail when they stop. If the
    Pi itself loses power or its network, the pings stop and the service tells
    you, which nothing on the Pi can.
"""

from __future__ import annotations

import configparser
import json
import logging
import re
import smtplib
import socket
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from cc0ac import alert_rules, units
from cc0ac.iolog import report

log = logging.getLogger("acurite")

DEFAULT_QUIET_MINUTES = 30
STATE_FILE = Path.home() / ".cc0ac-weather" / "alert-state.json"


def _get(cfg: configparser.ConfigParser, key: str, fallback: str = "") -> str:
    return cfg.get("alerts", key, fallback=fallback).strip()


def newest_reading(write_path: Path) -> float | None:
    """Epoch of the newest reading recorded, from current.json; None if none yet."""
    try:
        written = json.loads((write_path / "current.json").read_text()).get("written") or ""
        return datetime.fromisoformat(written.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, AttributeError):
        return None


def addresses(cfg: configparser.ConfigParser, section: str = "alerts") -> str:
    """A section's email_to, keeping only what looks like an address (it has an @).
    Anything else, such as a note pasted after the "=", is left out with a warning:
    configparser keeps everything after the "=" as the value."""
    raw = cfg.get(section, "email_to", fallback="").strip()
    good, bad = [], []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            (good if re.fullmatch(r"[^@\s]+@[^@\s]+", part) else bad).append(part)
    if bad:
        log.warning("[%s] email_to: %s isn't an email address, so it's ignored%s", section,
                    ", ".join(repr(b) for b in bad), " (blank uses the [alerts] address)" if section != "alerts" else "")
    return ", ".join(good)


def send(cfg: configparser.ConfigParser, title: str, message: str, targets: frozenset | None = None) -> list[str]:
    """Send one message every way set up (of targets: ntfy, email; None: both).
    Returns what went wrong, one line each."""
    problems = []
    topic = _get(cfg, "ntfy_topic")
    if topic and (targets is None or "ntfy" in targets):
        server = _get(cfg, "ntfy_server", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(f"{server}/{topic}", data=message.encode(), method="POST",
                                     headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "cloud"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            report("ntfy", True, title)
        except OSError as e:
            problems.append(f"ntfy: {e}")
            report("ntfy", False, f"{title}: {e}")
    to = addresses(cfg)
    if to and (targets is None or "email" in targets):
        try:
            _email(cfg, to, title, message)
            report("email", True, title)
        except (OSError, smtplib.SMTPException, ValueError) as e:
            problems.append(f"email: {e}")
            report("email", False, f"{title}: {e}")
    return problems


def _email(cfg: configparser.ConfigParser, to: str, title: str, message: str, html: str | None = None) -> None:
    """One message, plain text; with html, an HTML version beside it, which mail
    programs show instead (the text stays for those that don't)."""
    host = _get(cfg, "smtp_host")
    if not host:
        raise ValueError("email_to is set but smtp_host isn't")
    port = int(_get(cfg, "smtp_port", "587") or 587)
    user, password = _get(cfg, "smtp_user"), _get(cfg, "smtp_password")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = _get(cfg, "email_from") or user or to
    msg["To"] = to
    msg.set_content(message)
    if html:
        msg.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=context)
    with server:
        if user:
            server.login(user, password)
        server.send_message(msg)


def _ping(url: str) -> None:
    kind = "fail" if url.endswith("/fail") else "ok"
    try:
        urllib.request.urlopen(url, timeout=20).read()
        report("healthcheck", True, f"pinged {kind}")
    except OSError as e:
        log.warning("healthcheck ping %s failed: %s", url, e)
        report("healthcheck", False, f"ping {kind}: {e}")


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}



# ── The alert rules (alert_rules.py: one row per alert) ───────────────────────
#
# Each check, once a minute: every row of the kinds being checked looks at what the
# station sees (a detector per kind, below), finds what is so now, one instance per
# thing it is about (a sensor, a receiver, an NWS alert; "" for the station's weather),
# and keeps for each when it was last so and whether it has been told. Then:
#   it is so and not told  → its message;      told and no longer so → "over" (flag over);
#   told and still so      → said again, on the row's repeat;
#   a row's stays-on       → how long after it was last so it still counts as so.
# Rows flagged replaces take turns within their kind: one message when the first
# comes on, one when a stricter one comes on ("closer"), one "over" when the last ends.
# Messages with the same title and targets in one check go out as one, a line each.
# A message that fails to send is tried again next check: nothing is marked told.

ONLINE_IDS = ("open-meteo", "nws-")    # sources/online.py: internet weather, not a station
GRACE_S = 120.0                        # a strike heard this recently counts, whatever the stays-on
DASHBOARD_FILE = "alerts"              # <write_path>/alerts.json and alerts.js: the dashboard's banners
ONCE_SHOWN_S = 3600.0                  # a once-only message stays on the dashboard this long


def _setting(cfg: configparser.ConfigParser, key: str, default: float) -> float:
    try:
        return float(_get(cfg, key, str(default)) or default)
    except ValueError:
        return default


@dataclass
class Hit:
    """One thing a row finds so now."""
    message: str
    title: str
    at: float               # when it was last so (a strike: when it was heard)
    label: str = ""         # what it is about, for its "over" line
    value: float | None = None


class Look:
    """What one check sees."""

    def __init__(self, cfg: configparser.ConfigParser, write_path: Path | None, now: float, state: dict,
                 state_path: Path, upkeep: list[str] | None = None) -> None:
        self.cfg, self.write_path, self.now, self.state, self.state_path = cfg, write_path, now, state, state_path
        self.upkeep = upkeep
        self.shown_in = units.chosen(cfg)
        self.snapshot = None
        if write_path is not None:
            try:
                self.snapshot = json.loads((write_path / "current.json").read_text())
            except (OSError, ValueError):
                pass
        self.newest = newest_reading(write_path) if write_path is not None else None
        self._forecast: dict | None = None

    @property
    def sensors(self) -> list[dict]:
        return list((self.snapshot or {}).get("sensors", []))

    def outdoor(self) -> list[dict]:
        """Outdoor sensors heard lately: the station's own, internet weather only without one."""
        fresh = _setting(self.cfg, "quiet_minutes", DEFAULT_QUIET_MINUTES) * 60
        out = []
        for s in self.sensors:
            at = _stamp(str(s.get("updated") or ""))
            if at is not None and s.get("type") in ("Atlas", "Iris", "5N1", "outdoor") and self.now - at <= fresh:
                out.append(s)
        own = [s for s in out if not str(s.get("sensor_id", "")).startswith(ONLINE_IDS)]
        return own or out

    def forecast(self) -> dict:
        if self._forecast is None:
            try:
                self._forecast = json.loads((self.write_path / "forecast.json").read_text())
            except (OSError, ValueError, TypeError):
                self._forecast = {}
        return self._forecast

    def since(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch).astimezone().strftime("%a %b %d %H:%M")


def _frost(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    cold = [(s["fields"]["temp_f"], s.get("name", "")) for s in look.outdoor()
            if isinstance((s.get("fields") or {}).get("temp_f"), (int, float)) and rule.watches(s.get("name"), s.get("sensor_id"))]
    if not cold or rule.trigger is None:
        return {}
    t, name = min(cold)
    if t <= rule.trigger or (was.get("") and t <= rule.trigger + 2):
        return {"": Hit(f"Frost risk: {units.show('temperature', t, look.shown_in)} at {name}.", "frost risk", look.now)}
    return {}


def _heat(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    hot = [(s["fields"]["feels_like_f"], s.get("name", "")) for s in look.outdoor()
           if isinstance((s.get("fields") or {}).get("feels_like_f"), (int, float)) and rule.watches(s.get("name"), s.get("sensor_id"))]
    if not hot or rule.trigger is None:
        return {}
    t, name = max(hot)
    if t >= rule.trigger or (was.get("") and t >= rule.trigger - 3):
        return {"": Hit(f"Dangerous heat: feels like {units.show('temperature', t, look.shown_in, 0)} at {name}.",
                        "dangerous heat", look.now)}
    return {}


def _storm(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    change, drop = (look.snapshot or {}).get("pressure_change_3h"), rule.trigger
    if isinstance(change, (int, float)) and drop is not None and (change <= -drop or (was.get("") and change <= -drop / 2)):
        return {"": Hit(f"The barometer has fallen {units.show('pressure', -change, look.shown_in)} in 3 hours: "
                        "a storm may be coming.", "storm coming", look.now)}
    return {}


def _lightning(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    # A sensor sends the distance of its latest strike only: a far strike after a close
    # one finds nothing here, and the close one's time, kept by the engine, holds it on.
    close = []
    for s in look.sensors:
        f = s.get("fields") or {}
        heard = _stamp(str(f.get("last_strike_heard") or ""))
        mi = f.get("last_strike_mi")
        if heard is None or not isinstance(mi, (int, float)) or rule.trigger is None or mi > rule.trigger:
            continue
        if rule.watches(s.get("name"), s.get("sensor_id")):
            close.append((heard, mi, s.get("name", ""), f.get("strikes_today")))
    if not close:
        return {}
    heard, mi, name, today = max(close, key=lambda c: c[0])
    return {"": Hit(f"Lightning: a strike {units.show('distance', mi, look.shown_in)} away, heard at {name}"
                    + (f" ({today} today)." if isinstance(today, int) else "."), "lightning", heard, value=mi)}


def _nws(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    out = {}
    for a in look.forecast().get("alerts") or []:
        sev = str(a.get("severity") or "").lower()
        rank = alert_rules.SEVERITIES.index(sev) + 1 if sev in alert_rules.SEVERITIES else 0
        if rank < (rule.trigger or 0):
            continue
        event = str(a.get("event") or "Weather alert")
        until = ""
        ends = _stamp(str(a.get("ends") or ""))
        if ends is not None:
            until = " until " + look.since(ends)
        said = a.get("headline") or a.get("description") or ""
        out[f"{event}|{a.get('onset') or ''}"] = Hit(f"{event}{until}." + (f" {said}" if said else ""),
                                                     event.lower(), look.now, label=event, value=rank)
    return out


def _readings(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    if look.newest is None or rule.trigger is None or look.now - look.newest <= rule.trigger * 60:
        return {}
    return {"": Hit(f"No weather readings since {look.since(look.newest)} ({(look.now - look.newest) / 60:.0f} minutes). "
                    "Check that the hub has power and its cable is in the Pi, and that the Pi is on.",
                    "no readings", look.now, value=look.newest)}


def _sensor(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    quiet = (rule.trigger or DEFAULT_QUIET_MINUTES) * 60
    if look.newest is None or look.now - look.newest > quiet:
        return {}                # all quiet: that is the "no readings" message
    out = {}
    for s in look.sensors:
        sid = str(s.get("sensor_id", ""))
        updated = _stamp(str(s.get("updated") or ""))
        label = str(s.get("name") or sid)
        if not sid or sid.startswith(ONLINE_IDS) or updated is None or look.now - updated <= quiet or not rule.watches(label, sid):
            continue
        out[f"quiet:{sid}"] = Hit(f"{label} hasn't been heard since {look.since(updated)} ({(look.now - updated) / 60:.0f} minutes), "
                                  "though the other sensors still are. Its batteries, or something in the way?",
                                  "a sensor needs a look", look.now, label=label)
    return out


def _receiver(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    if look.snapshot is None:
        return {}
    found = _quiet_receivers(look.cfg, look.snapshot, look.now, look.state, look.state_path,
                             (rule.trigger or RECEIVER_QUIET_MINUTES) * 60)
    return {k: Hit(message, "a sensor needs a look", look.now, label=label)
            for k, (label, message) in found.items() if rule.watches(k.split(":", 1)[1], label)}


def _held_back(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    out = {}
    for s in look.sensors:
        sid = str(s.get("sensor_id", ""))
        label = str(s.get("name") or sid)
        if not sid or sid.startswith(ONLINE_IDS) or not rule.watches(label, sid):
            continue
        held = [(f, h.get("why", "")) for f, h in (s.get("suspect") or {}).items()
                if isinstance(h, dict) and not str(h.get("why", "")).startswith("worked out from")]
        if held:
            said = "; ".join(f"{_field_words(f)} {why}" for f, why in held)
            out[f"held:{sid}"] = Hit(f"{label}: a reading was held back as impossible ({said}). "
                                     "The last believed value is shown until it reads true again.",
                                     "a sensor needs a look", look.now, label=label)
    return out


BATTERY = re.compile(r"^(The battery in .+ is|The hub's batteries are) low\.$")   # upkeep.low_batteries


def _battery(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    out = {}
    for p in look.upkeep or []:
        m = BATTERY.match(p)
        if m and (rule.watches("any") or rule.watching.lower() in p.lower()):
            label = m[1].rsplit(" ", 1)[0]
            out["battery:" + label] = Hit(p, "needs looking after", look.now, label=label)
    return out


def _upkeep(look: Look, rule: alert_rules.Rule, was: dict) -> dict[str, Hit]:
    # Keyed without its numbers: a disk going from 91% to 92% full is the same problem.
    return {"upkeep:" + re.sub(r"\d+(?:\.\d+)?", "#", p): Hit(p, "needs looking after", look.now, label=p)
            for p in look.upkeep or [] if not BATTERY.match(p)}


DETECT = {"frost": _frost, "heat": _heat, "storm": _storm, "lightning": _lightning, "nws": _nws,
          "readings": _readings, "sensor": _sensor, "receiver": _receiver, "held_back": _held_back,
          "battery": _battery, "upkeep": _upkeep}
WEATHER_KINDS = ("frost", "heat", "storm", "lightning", "nws")
SENSOR_KINDS = ("sensor", "receiver", "held_back")
UPKEEP_KINDS = ("battery", "upkeep")


def _over(look: Look, rule: alert_rules.Rule, s: dict) -> tuple[str, str]:
    """(title, line) for the end of one instance."""
    k, label = rule.kind, s.get("label") or ""
    if k == "lightning":
        near = units.show("distance", rule.trigger, look.shown_in)
        return "lightning over", (f"No lightning within {near} for {alert_rules.say_duration(rule.stays)}."
                                  if rule.stays else f"No lightning within {near} now.")
    if k in ("frost", "heat", "storm"):
        return f"{k} over", {"frost": "The frost risk is over.", "heat": "The dangerous heat is over.",
                             "storm": "The barometer has stopped falling fast."}[k]
    if k == "nws":
        return f"{label.lower()} over", f"The Weather Service has ended the {label}."
    if k == "readings":
        newest = look.newest or look.now
        gap = (newest - float(s.get("value") or newest)) / 60
        return "readings are back", (f"Readings are coming in again (newest {look.since(newest)}; "
                                     f"quiet for about {gap:.0f} minutes).")
    if k in SENSOR_KINDS:
        return "sensors fine again", {"sensor": f"{label} is being heard again.",
                                      "receiver": f"{label} is hearing the sensors again.",
                                      "held_back": f"{label}: its readings are believed again."}[k]
    if k == "battery":
        return "looked after", f"{label} {'are' if label.endswith('batteries') else 'is'} fine again."
    return "looked after", f"Looked after: {label}"


@dataclass
class _Message:
    targets: frozenset
    title: str
    lines: list[str]
    done: list          # what to mark once it is sent
    what: list[tuple[str, str]]   # (row, "alerted" | "over" | "closer" | "again")


def run_rules(cfg: configparser.ConfigParser, write_path: Path | None, kinds: tuple[str, ...], now: float | None = None,
              state_path: Path = STATE_FILE, upkeep: list[str] | None = None) -> list[tuple[str, str]]:
    """One check of every row of these kinds. Returns [(row, what happened)]: what
    happened is alerted, closer (a stricter row of a replaces group), again (its
    repeat), over, or failed: <why>."""
    now = time.time() if now is None else now
    state = _load_state(state_path)
    look = Look(cfg, write_path, now, state, state_path, upkeep)
    rows = [r for r in alert_rules.rules(cfg) if r.kind in kinds and r.kind in DETECT and not r.off]
    book: dict = state.setdefault("rules", {})
    for gone in [n for n, b in book.items() if b.get("kind") in kinds and n not in {r.name for r in rows}]:
        del book[gone]
    # 1. What each row finds so now.
    for r in rows:
        rb = book.get(r.name)
        if not rb or rb.get("kind") != r.kind:
            rb = book[r.name] = {"kind": r.kind, "on": {}}
        insts: dict = rb["on"]
        hits = DETECT[r.kind](look, r, {i: bool(s.get("so")) for i, s in insts.items()})
        hold = r.stays or 0.0
        for i in set(hits) | set(insts):
            s, h = insts.setdefault(i, {}), hits.get(i)
            fresh = h is not None and now - h.at <= max(hold, GRACE_S)
            if fresh:
                s["last"] = max(float(s.get("last") or 0), h.at)
                s.update(message=h.message, title=h.title, label=h.label or s.get("label", ""))
                if h.value is not None and not s.get("told"):
                    s["value"] = h.value
            s["so"] = bool(fresh or (s.get("told") and hold and now - float(s.get("last") or 0) <= hold))
            if not s["so"] and not s.get("told"):
                del insts[i]
    # 2. What to say.
    out: list[_Message] = []

    def say(rule: alert_rules.Rule, title: str, line: str, what: str, marks: list) -> None:
        for m in out:
            if m.title == title and m.targets == rule.targets:
                m.lines.append(line)
                m.done += marks
                m.what.append((rule.name, what))
                return
        out.append(_Message(rule.targets, title, [line], list(marks), [(rule.name, what)]))

    def told(s: dict, yes: bool) -> callable:
        def mark() -> None:
            if yes:
                s["told"], s["sent"] = True, now
                s.setdefault("since", now)
            else:
                s.pop("told", None)
                s.pop("since", None)
        return mark

    def inst(rule: alert_rules.Rule, i: str) -> dict:
        return book[rule.name]["on"].get(i) or {}

    grouped = [r for r in rows if r.replaces and r.stays is not None]
    for r in rows:
        if r in grouped:
            continue
        for i, s in book[r.name]["on"].items():
            if s["so"] and not s.get("told"):
                say(r, s["title"], s["message"], "alerted", [told(s, True)])
            elif s["so"] and r.repeat and now - float(s.get("sent") or 0) >= r.repeat:
                say(r, s["title"], s["message"], "again", [lambda s=s: s.update(sent=now)])
            elif not s["so"] and s.get("told"):
                if r.over and r.stays is not None:
                    title, line = _over(look, r, s)
                    say(r, title, line, "over", [told(s, False)])
                else:
                    told(s, False)()
    for kind in {r.kind for r in grouped}:
        group = [r for r in grouped if r.kind == kind]
        for i in {i for r in group for i in book[r.name]["on"]}:
            on = [r for r in group if inst(r, i).get("so")]
            before = [r for r in group if inst(r, i).get("told")]
            strictest = lambda rs: next((a for a in rs if not any(b.stricter_than(a) for b in rs)), rs[0])
            if on and not before:
                lead = strictest(on)
                s = inst(lead, i)
                say(lead, s["title"], s["message"], "alerted", [told(inst(r, i), True) for r in on])
            elif on:
                new = [r for r in on if not inst(r, i).get("told") and all(r.stricter_than(b) for b in before)]
                if new:
                    lead = strictest(new)
                    s = inst(lead, i)
                    say(lead, f"{s['title']}, {alert_rules.KINDS[kind].word}", s["message"], "closer",
                        [told(inst(r, i), True) for r in on if not inst(r, i).get("told")])
                else:
                    for r in on:
                        if not inst(r, i).get("told"):
                            told(inst(r, i), True)()
                    lead = next((r for r in before if r in on and r.repeat), None)
                    if lead and now - float(inst(lead, i).get("sent") or 0) >= lead.repeat:
                        s = inst(lead, i)
                        say(lead, s["title"], s["message"], "again", [lambda s=s: s.update(sent=now)])
                for r in before:
                    if r not in on:
                        told(inst(r, i), False)()
            elif before:
                widest = next((a for a in before if not any(a.stricter_than(b) for b in before)), before[0])
                if any(r.over for r in before):
                    title, line = _over(look, widest, inst(widest, i))
                    say(widest, title, line, "over", [told(inst(r, i), False) for r in before])
                else:
                    for r in before:
                        told(inst(r, i), False)()
    # 3. Send, and keep what went.
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    did: list[tuple[str, str]] = []
    for m in out:
        problems = send(cfg, f"{name}: {m.title}", "\n".join(m.lines), m.targets)
        if problems:
            did += [(row, "failed: " + "; ".join(problems)) for row, _ in m.what]
            continue
        for mark in m.done:
            mark()
        did += m.what
    for rb in book.values():
        rb["on"] = {i: s for i, s in rb["on"].items() if s.get("so") or s.get("told")}
    state = {**_load_state(state_path), "rules": book}     # as written since: receivers_heard
    _write_state(state_path, state)
    if write_path is not None:
        write_dashboard(cfg, write_path, state, now)
    return did


def _write_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)


def write_dashboard(cfg: configparser.ConfigParser, write_path: Path, state: dict, now: float | None = None) -> None:
    """The alerts standing now whose row has the dashboard as a target, for the
    dashboard's banners: <write_path>/alerts.json, and alerts.js for the page."""
    now = time.time() if now is None else now
    rows = {r.name: r for r in alert_rules.rules(cfg) if "dashboard" in r.targets and not r.off}
    shown = []
    for row, rb in (state.get("rules") or {}).items():
        if row in rows:
            for s in rb.get("on", {}).values():
                if s.get("told") and s.get("so"):
                    shown.append({"row": row, "kind": rb.get("kind"), "title": s.get("title", ""),
                                  "message": s.get("message", ""), "since": s.get("since")})
    for e in state.get("events") or []:
        if e.get("row") in rows and now - float(e.get("at") or 0) <= ONCE_SHOWN_S:
            shown.append({"row": e["row"], "kind": "service", "title": e.get("title", ""),
                          "message": e.get("message", ""), "since": e.get("at")})
    shown.sort(key=lambda a: a.get("since") or 0)
    text = json.dumps({"written": now, "alerts": shown})
    try:
        old = (write_path / f"{DASHBOARD_FILE}.json").read_text()
        if json.loads(old).get("alerts") == shown:
            return
    except (OSError, ValueError, AttributeError):
        pass
    try:
        write_path.mkdir(parents=True, exist_ok=True)
        for suffix, body in ((".json", text), (".js", f"window.ACURITE_ALERTS = {text};\n")):
            tmp = write_path / f"{DASHBOARD_FILE}{suffix}.tmp"
            tmp.write_text(body)
            tmp.replace(write_path / f"{DASHBOARD_FILE}{suffix}")
    except OSError as e:
        log.warning("Couldn't write the dashboard's alerts: %s", e)


def _enabled(cfg: configparser.ConfigParser) -> bool:
    return cfg.getboolean("alerts", "enabled", fallback=False)


def check(cfg: configparser.ConfigParser, write_path: Path, now: float | None = None,
          state_path: Path = STATE_FILE) -> str:
    """No readings: alert on going quiet, alert on coming back; and the dead-man's
    switch (healthcheck_url). Returns what it did."""
    if not _enabled(cfg):
        return "alerts off"
    now = time.time() if now is None else now
    newest = newest_reading(write_path)
    if newest is None:
        return "no reading recorded yet; nothing to compare"
    rows = alert_rules.for_kind(cfg, "readings")
    quiet = min((r.trigger for r in rows if r.trigger), default=_setting(cfg, "quiet_minutes", DEFAULT_QUIET_MINUTES)) * 60
    stale = now - newest > quiet
    health = _get(cfg, "healthcheck_url").rstrip("/")
    if health:
        _ping(health + "/fail" if stale else health)
    did = run_rules(cfg, write_path, ("readings",), now, state_path)
    for _, what in did:
        if what == "alerted":
            return "alerted"
        if what == "over":
            return "all clear sent"
        if what.startswith("failed"):
            return ("alert failed: " if stale else "all-clear failed: ") + what.split(": ", 1)[1]
    return "still quiet" if stale else "fresh"


def check_weather(cfg: configparser.ConfigParser, write_path: Path, now: float | None = None,
                  state_path: Path = STATE_FILE) -> str:
    """Frost, heat, a falling barometer, lightning, NWS alerts: each row's message when
    it starts, and when it is over."""
    if not _enabled(cfg) or not any(r.kind in WEATHER_KINDS for r in alert_rules.rules(cfg)):
        return "weather alerts off"
    if not (write_path / "current.json").exists():
        return "weather: no current.json yet"
    did = run_rules(cfg, write_path, WEATHER_KINDS, now, state_path)
    return "weather: " + (", ".join(f"{row} {what}" for row, what in did) or "nothing new")


def check_sensors(cfg: configparser.ConfigParser, write_path: Path, now: float | None = None,
                  state_path: Path = STATE_FILE) -> str:
    """One message when a sensor goes quiet while the rest report, a receiver hears
    nothing, or a reading is held back; one when it's back."""
    if not _enabled(cfg) or not any(r.kind in SENSOR_KINDS and not r.off for r in alert_rules.rules(cfg)):
        return "sensor notices off"
    if not (write_path / "current.json").exists():
        return "sensors: no current.json yet"
    did = run_rules(cfg, write_path, SENSOR_KINDS, now, state_path)
    told = sum(1 for _, w in did if w == "alerted")
    back = sum(1 for _, w in did if w == "over")
    failed = [w for _, w in did if w.startswith("failed")]
    parts = ([f"told ({told})"] if told else []) + ([f"back ({back})"] if back else []) + failed[:1]
    if parts:
        return "sensors: " + ", ".join(parts)
    standing = any(s.get("told") for n, b in (_load_state(state_path).get("rules") or {}).items()
                   if b.get("kind") in SENSOR_KINDS for s in b.get("on", {}).values())
    return "sensors: " + ("same as before" if standing else "fine")


def check_upkeep(cfg: configparser.ConfigParser, problems: list[str], state_path: Path = STATE_FILE,
                 write_path: Path | None = None, now: float | None = None) -> str:
    """One message when a disk, backup or battery problem appears, one when it's gone.
    write_path: where the dashboard's banners go (none: not written)."""
    if not _enabled(cfg):
        return "alerts off"
    did = run_rules(cfg, write_path, UPKEEP_KINDS, now, state_path, upkeep=problems)
    told = sum(1 for _, w in did if w == "alerted")
    if told:
        return f"upkeep alerted ({told})"
    if any(w == "over" for _, w in did):
        return "upkeep all clear sent"
    failed = [w for _, w in did if w.startswith("failed")]
    if failed:
        return "upkeep alert failed: " + failed[0].split(": ", 1)[1]
    standing = any(s.get("told") for b in (_load_state(state_path).get("rules") or {}).values()
                   if b.get("kind") in UPKEEP_KINDS for s in b.get("on", {}).values())
    return "upkeep: " + ("same problems" if standing else "fine")



# ── The station itself (kind service: started, stopped, updated) ──────────────
#
# service: the station started (and with which version, if it changed), stopped,
#   or started again after stopping without saying so (a crash, or the power).
#   A restart for saved settings says nothing. Its rows are once-only: no "over".


def _service_rows(cfg: configparser.ConfigParser) -> list[alert_rules.Rule]:
    return alert_rules.for_kind(cfg, "service") if _enabled(cfg) else []


def _say_service(cfg: configparser.ConfigParser, title: str, message: str, state_path: Path,
                 now: float) -> list[str]:
    """Through every service row's targets; the dashboard keeps it an hour."""
    rows = _service_rows(cfg)
    targets = frozenset().union(*(r.targets for r in rows))
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    problems = send(cfg, f"{name}: {title}", message, targets) if targets & {"ntfy", "email"} else []
    shown = [r for r in rows if "dashboard" in r.targets]
    if shown:
        events = [e for e in _load_state(state_path).get("events") or [] if now - float(e.get("at") or 0) <= ONCE_SHOWN_S]
        events.append({"row": shown[0].name, "title": title, "message": message, "at": now})
        _save_state(state_path, "events", events[-10:])
        write_path = cfg.get("device", "write_path", fallback="").strip()
        if write_path:
            write_dashboard(cfg, Path(write_path).expanduser(), _load_state(state_path), now)
    return problems


def _save_state(state_path: Path, key: str, value: object) -> None:
    state = {**_load_state(state_path), key: value}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)


RESTART_MARK = "restarting"    # beside STATE_FILE: config.restart_soon is restarting for saved settings


def mark_restart(state_path: Path = STATE_FILE) -> None:
    """Saved settings: the stop and start that follow are not news."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.with_name(RESTART_MARK).write_text(str(time.time()))
    except OSError as e:
        log.warning("Couldn't mark the restart (%s); it may send a started message", e)


def service_started(cfg: configparser.ConfigParser, version: str, state_path: Path = STATE_FILE,
                    now: float | None = None) -> str:
    """At start: say so, unless it is a restart for saved settings."""
    now = time.time() if now is None else now
    was = _load_state(state_path).get("service") or {}
    mark = state_path.with_name(RESTART_MARK)
    settings = mark.exists() and now - mark.stat().st_mtime < 600
    mark.unlink(missing_ok=True)
    _save_state(state_path, "service", {"running": True, "version": version, "since": now})
    if not _service_rows(cfg):
        return "service notices off"
    if settings:
        return "restarted for saved settings"
    if was.get("version") and was["version"] != version:
        message = f"The station started, now version {version.split('/')[0]} (it was {was['version'].split('/')[0]})."
    elif was.get("running"):
        message = ("The station started again. It had stopped without saying so: it crashed, "
                   "or the computer lost power or was switched off.")
    else:
        message = f"The station started (version {version.split('/')[0]})."
    problems = _say_service(cfg, "started", message, state_path, now)
    return "started: " + ("told" if not problems else "; ".join(problems))


def service_stopping(cfg: configparser.ConfigParser, state_path: Path = STATE_FILE) -> str:
    """When asked to stop (not for saved settings): say so, and remember it stopped cleanly."""
    _save_state(state_path, "service", {**(_load_state(state_path).get("service") or {}), "running": False})
    if not _service_rows(cfg) or state_path.with_name(RESTART_MARK).exists():
        return "stopping"
    problems = _say_service(cfg, "stopped", "The station was stopped. It records nothing until it starts again.",
                            state_path, time.time())
    return "stopped: " + ("told" if not problems else "; ".join(problems))


def _stamp(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _field_words(field: str) -> str:
    """temp_f -> temperature, wind_gust_mph -> wind gust."""
    words = {"temp": "temperature", "pct": "", "f": "", "mph": "", "inhg": "", "in": "", "mi": "", "deg": "", "lux": ""}
    return " ".join(w for w in (words.get(p, p) for p in field.split("_")) if w)


# The receivers: what hears the sensors. Each sensor's reception (recorder.py) says
# which heard it lately, but only for the last hour, so the time each receiver last
# heard anything is kept in the alert state, and its raw log's last write counts too
# (a restarted station has no reception yet). One switched on that has heard nothing
# for receiver_quiet_minutes (5) is a short notice pointing at the status page, whether
# or not another receiver still hears: two receivers hearing the same sensors hide
# each other's silence from the per-sensor notice. Its name, and what to try.
RECEIVER_QUIET_MINUTES = 5
RECEIVER_WORDS = {
    "radio": ("The radio (rtl_433)", "Check the radio dongle is plugged in; if rtl_433 keeps failing to start, "
                                     "unplug the dongle and plug it back in."),
    "hub": ("The hub", "Check the hub has power and its cable is in."),
    "console": ("The console", "Check the console is on and still sending to the station."),
}
RTL_433_LAST_EXIT = Path.home() / ".cc0ac-weather" / "rtl_433-last-exit.txt"   # sources/rtl_433.py writes it


def _receivers_on(cfg: configparser.ConfigParser) -> list[str]:
    from cc0ac.sources import acurite_access, console_upload, rtl_433
    on = []
    if rtl_433.enabled(cfg) and not cfg.get("capture", "read", fallback="").strip():
        on.append("radio")
    if acurite_access.enabled(cfg):
        on.append("hub")
    if console_upload.enabled(cfg):
        on.append("console")
    return on


def _raw_log_written(cfg: configparser.ConfigParser) -> dict[str, float]:
    """{receiver: when its newest non-empty raw log was last written}."""
    from cc0ac import status     # status imports the web side; only the check needs it
    written = {}
    for who, folder in status._raw_dirs(cfg).items():
        days = status._log_days(folder)
        if days and (folder / days[0]).stat().st_size:
            written[who] = (folder / days[0]).stat().st_mtime
    return written


def status_url(cfg: configparser.ConfigParser) -> str:
    """The status page as this station's own network reaches it (it is not served outside)."""
    port = (cfg.get("web", "port", fallback="12345").split(",")[0].strip() or "12345")
    return f"http://{socket.gethostname()}.local:{port}/status"


def _quiet_receivers(cfg: configparser.ConfigParser, snapshot: dict, now: float, state: dict,
                     state_path: Path, quiet: float) -> dict[str, tuple[str, str]]:
    heard: dict = dict(state.get("receivers_heard") or {})    # receiver -> when it last heard anything
    for s in snapshot.get("sensors", []):
        for who, r in (s.get("reception") or {}).items():
            at = _stamp(str((r or {}).get("last") or ""))
            if at is not None and at > float(heard.get(who) or 0):
                heard[who] = at
    try:
        for who, at in _raw_log_written(cfg).items():
            if at > float(heard.get(who) or 0):
                heard[who] = at
    except OSError:
        pass
    _save_state(state_path, "receivers_heard", heard)
    found = {}
    for who in _receivers_on(cfg):
        last = heard.get(who)
        if last is None or now - float(last) <= quiet:
            continue
        label, hint = RECEIVER_WORDS[who]
        since = datetime.fromtimestamp(float(last)).astimezone().strftime("%a %b %d %H:%M")
        message = (f"{label} hasn't heard any sensor since {since} ({(now - float(last)) / 60:.0f} minutes). "
                   f"{hint} Status: {status_url(cfg)}")
        if who == "radio":
            try:
                said = RTL_433_LAST_EXIT.read_text().strip()
                if said and RTL_433_LAST_EXIT.stat().st_mtime > float(last):
                    message += f" rtl_433's last words: {said}"
            except OSError:
                pass
        found[f"receiver:{who}"] = (label, message)
    return found


