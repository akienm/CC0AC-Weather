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
        except OSError as e:
            problems.append(f"ntfy: {e}")
    to = _get(cfg, "email_to")
    if to:
        try:
            _email(cfg, to, title, message)
        except (OSError, smtplib.SMTPException, ValueError) as e:
            problems.append(f"email: {e}")
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
    try:
        urllib.request.urlopen(url, timeout=20).read()
    except OSError as e:
        log.warning("healthcheck ping %s failed: %s", url, e)


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
