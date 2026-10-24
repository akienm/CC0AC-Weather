"""
Tell someone when readings stop ([alerts]).

    python3 acurite-capture.py --check        # run every few minutes by a timer
    python3 acurite-capture.py --test-alert   # send a test message now

The check runs apart from the program that records, from its own systemd timer
(cc0ac-weather-check.timer), so it still speaks up when that program has hung or
died, not only when the hub goes quiet. It reads the time of the newest reading
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
import smtplib
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from iolog import report

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
    to = _get(cfg, "email_to")
    if to:
        try:
            _email(cfg, to, title, message)
            report("email", True, title)
        except (OSError, smtplib.SMTPException, ValueError) as e:
            problems.append(f"email: {e}")
            report("email", False, f"{title}: {e}")
    return problems


def _email(cfg: configparser.ConfigParser, to: str, title: str, message: str) -> None:
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
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
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
        out["frost"] = (f"Frost risk: {t:.1f}°F at {name}." if t is not None
                        and (t <= frost or (was.get("frost") and t <= frost + 2)) else None)
    if "heat" in wanted:
        hot = [(s["fields"]["feels_like_f"], s["name"]) for s in outdoor
               if isinstance(s["fields"].get("feels_like_f"), (int, float))]
        t, name = max(hot) if hot else (None, "")
        out["heat"] = (f"Dangerous heat: feels like {t:.0f}°F at {name}." if t is not None
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
        mi, name, today = min(close, key=lambda c: c[0]) if close else (None, "", None)
        out["lightning"] = (f"Lightning: a strike {mi:g} mi away, heard at {name}"
                            + (f" ({today} today)." if isinstance(today, int) else ".")
                            if close else None)
    if "storm" in wanted:
        change = snapshot.get("pressure_change_3h")
        out["storm"] = (f"The barometer has fallen {-change:.2f} inHg in 3 hours: a storm may be coming."
                        if isinstance(change, (int, float)) and (change <= -drop or (was.get("storm") and change <= -drop / 2))
                        else None)
    return out


WEATHER_OVER = {"frost": "The frost risk is over.", "heat": "The dangerous heat is over.",
                "storm": "The barometer has stopped falling fast.",
                "lightning": "No lightning within {lightning_mi:g} mi for {lightning_minutes:g} minutes."}


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
            message = WEATHER_OVER[kind].format(lightning_mi=_setting(cfg, "lightning_mi", 10),
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
    """One message when a disk or backup problem appears or gets worse, one when they're all gone."""
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
        if send(cfg, f"{name}: looked after", "The disk and backup problems are gone."):
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
