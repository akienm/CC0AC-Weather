"""
Tell someone when readings stop ([alerts]).

    python3 acurite-capture.py --check        # run every minute
    python3 acurite-capture.py --test-alert   # send a test message now

The check runs as a process of its own, every minute, started by the boot loader (or,
for a station started some other way, by the station), so under the boot loader it
still speaks up when the program that records has hung or died. It reads the time of the newest reading
from current.json. Once that is older than quiet_minutes it sends one message,
"no readings since ...", and when readings come back it sends one more, "back".
In between it says nothing.

Three ways to hear about it, any or all:

  * ntfy: a free push-notification app (ntfy.sh). Install the app, subscribe to
    a topic name nobody else would guess, put the same name in ntfy_topic.
  * email, through any mail server that takes a login (for Gmail: an app
    password, smtp.gmail.com, port 587).
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
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from cc0ac import units
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


def send(cfg: configparser.ConfigParser, title: str, message: str) -> list[str]:
    """Send one message every way set up. Returns what went wrong, one line each."""
    problems = []
    topic = _get(cfg, "ntfy_topic")
    if topic:
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
    if to:
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


def check(cfg: configparser.ConfigParser, write_path: Path, now: float | None = None,
          state_path: Path = STATE_FILE) -> str:
    """One look: alert on going quiet, alert on coming back. Returns what it did."""
    if not cfg.getboolean("alerts", "enabled", fallback=False):
        return "alerts off"
    now = time.time() if now is None else now
    quiet = float(_get(cfg, "quiet_minutes", str(DEFAULT_QUIET_MINUTES)) or DEFAULT_QUIET_MINUTES) * 60
    newest = newest_reading(write_path)
    state = _load_state(state_path)
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    health = _get(cfg, "healthcheck_url").rstrip("/")
    if newest is None:
        return "no reading recorded yet; nothing to compare"
    stale = now - newest > quiet
    since = datetime.fromtimestamp(newest).astimezone().strftime("%a %b %d %H:%M")
    if health:
        _ping(health + "/fail" if stale else health)
    did = "fresh" if not stale else "still quiet"
    if stale and not state.get("alerted"):
        problems = send(cfg, f"{name}: no readings",
                        f"No weather readings since {since} ({(now - newest) / 60:.0f} minutes). "
                        "Check that the hub has power and its cable is in the Pi, and that the Pi is on.")
        state = {**state, "alerted": not problems, "since": newest, "problems": problems}
        did = "alerted" if not problems else "alert failed: " + "; ".join(problems)
    elif not stale and state.get("alerted"):
        gap = (newest - float(state.get("since") or newest)) / 60
        problems = send(cfg, f"{name}: readings are back",
                        f"Readings are coming in again (newest {since}; quiet for about {gap:.0f} minutes).")
        state = {**state, "alerted": False} if not problems else state
        did = "all clear sent" if not problems else "all-clear failed: " + "; ".join(problems)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)
    return did


# ── Weather alerts ([alerts] weather) ─────────────────────────────────────────
#
# No thresholds to work out: frost, dangerous heat, and a barometer falling fast
# (a storm on its way). Each sends one message when it starts and one when it is
# over; "over" is a little past the threshold, so a value hovering at it doesn't
# send a message every check.

ONLINE_IDS = ("open-meteo", "nws-")    # sources/online.py: internet weather, not a station


def _setting(cfg: configparser.ConfigParser, key: str, default: float) -> float:
    try:
        return float(_get(cfg, key, str(default)) or default)
    except ValueError:
        return default


def weather_conditions(cfg: configparser.ConfigParser, snapshot: dict, now: float,
                       was: dict | None = None) -> dict[str, str | None]:
    """Each weather alert switched on → what is happening now, or None if it isn't.
    was: which were on at the last check (they end a little past the threshold)."""
    was = was or {}
    wanted = {w.strip().lower() for w in _get(cfg, "weather").split(",") if w.strip()}
    frost, heat = _setting(cfg, "frost_f", 35), _setting(cfg, "heat_f", 105)
    drop = _setting(cfg, "pressure_drop_inhg", 0.06)
    fresh = _setting(cfg, "quiet_minutes", DEFAULT_QUIET_MINUTES) * 60
    shown_in = units.chosen(cfg)   # the settings stay in °F, inHg and miles; the messages follow [units]
    outdoor = []
    for s in snapshot.get("sensors", []):
        try:
            age = now - datetime.fromisoformat(s["updated"].replace("Z", "+00:00")).timestamp()
        except (KeyError, AttributeError, ValueError):
            continue
        if s.get("type") in ("Atlas", "Iris", "5N1", "outdoor") and age <= fresh:
            outdoor.append(s)
    # A station's own sensors first; internet weather only for a house without one.
    own = [s for s in outdoor if not str(s.get("sensor_id", "")).startswith(ONLINE_IDS)]
    outdoor = own or outdoor
    out: dict[str, str | None] = {}
    if "frost" in wanted:
        cold = [(s["fields"]["temp_f"], s["name"]) for s in outdoor
                if isinstance(s["fields"].get("temp_f"), (int, float))]
        t, name = min(cold) if cold else (None, "")
        out["frost"] = (f"Frost risk: {units.show('temperature', t, shown_in)} at {name}." if t is not None
                        and (t <= frost or (was.get("frost") and t <= frost + 2)) else None)
    if "heat" in wanted:
        hot = [(s["fields"]["feels_like_f"], s["name"]) for s in outdoor
               if isinstance(s["fields"].get("feels_like_f"), (int, float))]
        t, name = max(hot) if hot else (None, "")
        out["heat"] = (f"Dangerous heat: feels like {units.show('temperature', t, shown_in, 0)} at {name}." if t is not None
                       and (t >= heat or (was.get("heat") and t >= heat - 3)) else None)
    if "lightning" in wanted:
        # A strike the station heard (its count rose: last_strike_heard) within
        # lightning_mi, in the last lightning_minutes; over once that long passes
        # with none so close. Any sensor with a lightning detector, room or outdoor.
        near, window = _setting(cfg, "lightning_mi", 10), _setting(cfg, "lightning_minutes", 30) * 60
        close = []
        for s in snapshot.get("sensors", []):
            f = s.get("fields") or {}
            try:
                heard = datetime.fromisoformat(f["last_strike_heard"].replace("Z", "+00:00")).timestamp()
            except (KeyError, AttributeError, ValueError):
                continue
            if isinstance(f.get("last_strike_mi"), (int, float)) and f["last_strike_mi"] <= near and now - heard <= window:
                close.append((f["last_strike_mi"], s.get("name", ""), f.get("strikes_today")))
        if close:
            mi, name, today = min(close, key=lambda c: c[0])
            out["lightning"] = (f"Lightning: a strike {units.show('distance', mi, shown_in)} away, heard at {name}"
                                + (f" ({today} today)." if isinstance(today, int) else "."))
        else:
            out["lightning"] = None
    if "storm" in wanted:
        change = snapshot.get("pressure_change_3h")
        out["storm"] = (f"The barometer has fallen {units.show('pressure', -change, shown_in)} in 3 hours: a storm may be coming."
                        if isinstance(change, (int, float)) and (change <= -drop or (was.get("storm") and change <= -drop / 2))
                        else None)
    return out


WEATHER_OVER = {"frost": "The frost risk is over.", "heat": "The dangerous heat is over.",
                "storm": "The barometer has stopped falling fast.",
                "lightning": "No lightning within {lightning_distance} for {lightning_minutes:g} minutes."}


def check_weather(cfg: configparser.ConfigParser, write_path: Path, now: float | None = None,
                  state_path: Path = STATE_FILE) -> str:
    """One message when frost, heat or a falling barometer starts, one when it's over."""
    if not cfg.getboolean("alerts", "enabled", fallback=False) or not _get(cfg, "weather"):
        return "weather alerts off"
    now = time.time() if now is None else now
    try:
        snapshot = json.loads((write_path / "current.json").read_text())
    except (OSError, ValueError):
        return "weather: no current.json yet"
    state = _load_state(state_path)
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    sent = dict(state.get("weather") or {})
    did = []
    for kind, now_says in weather_conditions(cfg, snapshot, now, sent).items():
        if now_says and not sent.get(kind):
            message, title = now_says, f"{name}: {now_says.split(':')[0].lower()}"
        elif not now_says and sent.get(kind):
            message = WEATHER_OVER[kind].format(
                lightning_distance=units.show("distance", _setting(cfg, "lightning_mi", 10), units.chosen(cfg)),
                lightning_minutes=_setting(cfg, "lightning_minutes", 30))
            title = f"{name}: {kind} over"
        else:
            continue
        problems = send(cfg, title, message)
        if problems:
            did.append(f"{kind} alert failed: " + "; ".join(problems))
            continue
        sent[kind] = bool(now_says)
        did.append(f"{kind} {'alerted' if now_says else 'over'}")
    state = {**_load_state(state_path), "weather": sent}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)
    return "weather: " + (", ".join(did) or "nothing new")


def check_upkeep(cfg: configparser.ConfigParser, problems: list[str], state_path: Path = STATE_FILE) -> str:
    """One message when a disk, backup or battery problem appears or gets worse, one when they're all gone."""
    if not cfg.getboolean("alerts", "enabled", fallback=False):
        return "alerts off"
    state = _load_state(state_path)
    sent = state.get("upkeep", [])
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    new = [p for p in problems if p not in sent]
    if new:
        trouble = send(cfg, f"{name}: needs looking after", "\n".join(new))
        if trouble:
            return "upkeep alert failed: " + "; ".join(trouble)
        did = f"upkeep alerted ({len(new)})"
    elif sent and not problems:
        if send(cfg, f"{name}: looked after", "Those problems are gone."):
            return "upkeep all-clear failed"
        did = "upkeep all clear sent"
    else:
        did = "upkeep: " + ("same problems" if problems else "fine")
    state["upkeep"] = problems
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_path)
    return did


# ── Notices about the station itself ([alerts] notices) ───────────────────────
#
# service: the station started (and with which version, if it changed), stopped,
#   or started again after stopping without saying so (a crash, or the power).
#   A restart for saved settings says nothing.
# sensors: one sensor quiet for quiet_minutes while others still report (when
#   they all stop, that is the "no readings" message), or one receiver (radio,
#   hub, console) hearing nothing while another still does, and back.
# held_back: a reading held back as impossible (sanity.py), and believed again.

NOTICES = ("service", "sensors", "held_back")


def notices(cfg: configparser.ConfigParser) -> set[str]:
    if not cfg.getboolean("alerts", "enabled", fallback=False):
        return set()
    raw = cfg.get("alerts", "notices", fallback=", ".join(NOTICES))
    return {w.strip().lower() for w in raw.split(",") if w.strip().lower() in NOTICES}


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
    if "service" not in notices(cfg):
        return "service notices off"
    if settings:
        return "restarted for saved settings"
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    if was.get("version") and was["version"] != version:
        message = f"The station started, now version {version.split('/')[0]} (it was {was['version'].split('/')[0]})."
    elif was.get("running"):
        message = ("The station started again. It had stopped without saying so: it crashed, "
                   "or the computer lost power or was switched off.")
    else:
        message = f"The station started (version {version.split('/')[0]})."
    problems = send(cfg, f"{name}: started", message)
    return "started: " + ("told" if not problems else "; ".join(problems))


def service_stopping(cfg: configparser.ConfigParser, state_path: Path = STATE_FILE) -> str:
    """When asked to stop (not for saved settings): say so, and remember it stopped cleanly."""
    _save_state(state_path, "service", {**(_load_state(state_path).get("service") or {}), "running": False})
    if "service" not in notices(cfg) or state_path.with_name(RESTART_MARK).exists():
        return "stopping"
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    problems = send(cfg, f"{name}: stopped", "The station was stopped. It records nothing until it starts again.")
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
                     state_path: Path) -> dict[str, tuple[str, str]]:
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
    quiet = _setting(cfg, "receiver_quiet_minutes", RECEIVER_QUIET_MINUTES) * 60
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


def check_sensors(cfg: configparser.ConfigParser, write_path: Path, now: float | None = None,
                  state_path: Path = STATE_FILE) -> str:
    """One message when a sensor goes quiet while the rest report, or a reading is held
    back; one when it's back."""
    wanted = notices(cfg) & {"sensors", "held_back"}
    if not wanted:
        return "sensor notices off"
    now = time.time() if now is None else now
    try:
        snapshot = json.loads((write_path / "current.json").read_text())
    except (OSError, ValueError):
        return "sensors: no current.json yet"
    quiet = _setting(cfg, "quiet_minutes", DEFAULT_QUIET_MINUTES) * 60
    newest = newest_reading(write_path)
    station_fresh = newest is not None and now - newest <= quiet
    found: dict[str, tuple[str, str]] = {}      # what is wrong now: key -> (sensor's name, message)
    for s in snapshot.get("sensors", []):
        sid = str(s.get("sensor_id", ""))
        if not sid or sid.startswith(ONLINE_IDS):
            continue
        label = str(s.get("name") or sid)
        updated = _stamp(str(s.get("updated") or ""))
        if "sensors" in wanted and station_fresh and updated is not None and now - updated > quiet:
            since = datetime.fromtimestamp(updated).astimezone().strftime("%a %b %d %H:%M")
            found[f"quiet:{sid}"] = (label, f"{label} hasn't been heard since {since} ({(now - updated) / 60:.0f} minutes), "
                                            "though the other sensors still are. Its batteries, or something in the way?")
        held = [(f, h.get("why", "")) for f, h in (s.get("suspect") or {}).items()
                if isinstance(h, dict) and not str(h.get("why", "")).startswith("worked out from")]
        if "held_back" in wanted and held:
            said = "; ".join(f"{_field_words(f)} {why}" for f, why in held)
            found[f"held:{sid}"] = (label, f"{label}: a reading was held back as impossible ({said}). "
                                           "The last believed value is shown until it reads true again.")
    state = _load_state(state_path)
    if "sensors" in wanted:
        found.update(_quiet_receivers(cfg, snapshot, now, state, state_path))
    sent: dict = dict(state.get("sensors") or {})      # key -> the sensor's name
    name = cfg.get("station", "name", fallback="").strip() or "Weather station"
    did = []
    new = {k: v for k, v in found.items() if k not in sent}
    gone = [k for k in sent if k not in found]
    if new:
        problems = send(cfg, f"{name}: a sensor needs a look", "\n".join(m for _, m in new.values()))
        if problems:
            did.append("sensor notice failed: " + "; ".join(problems))
        else:
            sent.update({k: label for k, (label, _) in new.items()})
            did.append(f"told ({len(new)})")
    if gone:
        lines = [f"{sent[k]} is being heard again." if k.startswith("quiet:")
                 else f"{sent[k]} is hearing the sensors again." if k.startswith("receiver:")
                 else f"{sent[k]}: its readings are believed again." for k in gone]
        problems = send(cfg, f"{name}: sensors fine again", "\n".join(lines))
        if problems:
            did.append("sensor all-clear failed: " + "; ".join(problems))
        else:
            for k in gone:
                sent.pop(k)
            did.append(f"back ({len(gone)})")
    _save_state(state_path, "sensors", sent)
    return "sensors: " + (", ".join(did) or ("fine" if not found else "same as before"))
