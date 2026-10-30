"""
What each part of the station sent to other services and how it went, for the
status page.

Every send (an upload, the relay to AcuRite, a forecast fetch, an alert) is one
line appended to sent/<name>/<UTC date>.log beside config.ini: the time, ok or
FAILED, and the answer; never the keys or passwords sent. The status page reads
these files, so it shows sends from every process (the alert timer's too) and
across restarts. A month of them is kept; the raw logs of what came in are the
record and are kept for good, these are not.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_DIR = Path.home() / ".cc0ac-weather"
# Set by the program at work (the service, the alert timer) with keep(); until
# then nothing is written, so a test or a one-off command leaves no trace.
SENT_DIR: Path | None = None
KEEP_DAYS = 31
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
LOG_FILE = re.compile(r"\d{4}-\d{2}-\d{2}\.log")


def keep(folder: Path | None = None) -> None:
    global SENT_DIR
    SENT_DIR = folder or CONFIG_DIR / "sent"


def report(name: str, ok: bool, detail: str = "", now: float | None = None) -> None:
    """One send to `name` happened; ok or not, and what came back."""
    if not NAME.fullmatch(name):
        raise ValueError(f"not a log name: {name!r}")
    if SENT_DIR is None:
        return
    stamp = datetime.fromtimestamp(now or time.time(), timezone.utc)
    detail = " ".join(str(detail).split())[:300]
    folder = SENT_DIR / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stamp:%Y-%m-%d}.log"
        if not path.exists():
            _prune(folder, stamp)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp:%Y-%m-%dT%H:%M:%SZ}\t{'ok' if ok else 'FAILED'}\t{detail}\n")
    except OSError:
        pass   # a full or broken disk is reported on the status page by itself


def _prune(folder: Path, stamp: datetime) -> None:
    oldest = f"{stamp - timedelta(days=KEEP_DAYS):%Y-%m-%d}"
    for f in folder.glob("*.log"):
        if LOG_FILE.fullmatch(f.name) and f.stem < oldest:
            f.unlink(missing_ok=True)


def latest(name: str) -> dict | None:
    """The newest day's sends to `name`: the last one, the last ok one, and how many of each."""
    if SENT_DIR is None:
        return None
    files = sorted(f for f in (SENT_DIR / name).glob("*.log") if LOG_FILE.fullmatch(f.name))
    if not files:
        return None
    out: dict = {"name": name, "day": files[-1].stem, "ok_count": 0, "failed_count": 0, "last": None, "last_ok": None}
    for line in files[-1].read_text(errors="replace").splitlines():
        at, _, rest = line.partition("\t")
        result, _, detail = rest.partition("\t")
        entry = {"at": at, "ok": result == "ok", "detail": detail}
        out["last"] = entry
        if entry["ok"]:
            out["ok_count"] += 1
            out["last_ok"] = at
        else:
            out["failed_count"] += 1
    return out


def names() -> list[str]:
    if SENT_DIR is None or not SENT_DIR.is_dir():
        return []
    return sorted(d.name for d in SENT_DIR.iterdir() if d.is_dir() and NAME.fullmatch(d.name))
