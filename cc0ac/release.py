"""
Is there a newer version? For the setup page's "Update available" and its
Update now button.

The boot loader (updates/bootloader.py) installs updates; this only asks the same
place it does (GitHub's tags, or the folder it was installed from) and compares
with this copy's VERSION. Update now writes ~/.cc0ac-weather/update-now, which
the running boot loader picks up within a second and handles like `update` by
hand: it ignores [updates] automatic, since pressing the button is asking.
Without a boot loader (a copy run by hand, or from git) there is nothing to
install with, and the page says how instead.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from pathlib import Path

from cc0ac.config import station_version
from cc0ac.diagnostics import BOOT_ROOT

log = logging.getLogger("acurite")

REPO = "akienm/CC0AC-Weather"
VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
ASK_FILE = Path.home() / ".cc0ac-weather" / "update-now"
CACHE_S = 3600
_cache: dict = {}


def _tags(spec: str) -> list[str]:
    if spec.startswith("github:"):
        req = urllib.request.Request(f"https://api.github.com/repos/{spec[7:]}/tags?per_page=100",
                                     headers={"User-Agent": f"CC0AC-Weather station (github.com/{REPO})"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return [t["name"] for t in json.loads(r.read())]
    return [p.name for p in Path(spec).iterdir() if p.is_dir()]


def _version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(map(int, m.groups())) if m else None


def boot_loader_running(root: Path | None = BOOT_ROOT, now: float | None = None) -> bool:
    try:
        return root is not None and (time.time() if now is None else now) - (root / "running").stat().st_mtime < 150
    except OSError:
        return False


def newest(root: Path | None = BOOT_ROOT, current: str | None = None, now: float | None = None) -> dict:
    """{"current", "newest", "available", "can_update", "problem"}; asked at most once an hour."""
    now = time.time() if now is None else now
    current = current or station_version()
    spec = f"github:{REPO}"
    if root is not None:
        try:
            spec = json.loads((root / "bootloader.json").read_text()).get("releases") or spec
        except (OSError, ValueError):
            pass
    out = {"current": current.split("/")[0], "newest": None, "available": False,
           "can_update": boot_loader_running(root, now), "problem": None}
    cached = _cache.get(spec)
    if cached and now - cached[0] < CACHE_S:
        tags = cached[1]
    else:
        try:
            tags = _tags(spec)
        except (OSError, ValueError, KeyError, TypeError) as e:
            out["problem"] = f"couldn't ask for the newest version ({e})"
            return out
        _cache[spec] = (now, tags)
    found = [(v, t) for t in tags if VERSION_TAG.match(t) and (v := _version(t))]
    mine = _version(current)
    if found:
        top_v, top = max(found)
        out["newest"] = top[1:]
        out["available"] = mine is not None and top_v > mine
    return out


def ask(what: str = "all", path: Path = ASK_FILE) -> None:
    """Ask the running boot loader to update now."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(what)
    _cache.clear()     # the next look sees what it installed
    log.info("Update now: asked the boot loader (%s)", path)
