#!/usr/bin/env python3
"""CC0AC-Weather's boot loader: installs the station, runs it, and keeps it patched.
UPDATES.md says why each part is the way it is.

  bootloader.py install --platform P --root DIR --config FILE --home DIR [--user U]
                        [--from DIR | --tag TAG] [--image] [--no-hub-network | --hub-interface IF]
  bootloader.py ports                          the wired ports that could take the hub's cable
  bootloader.py outside [--except PORT]        the ports that reach the internet
  bootloader.py run [--root DIR]               the service: run the station, check every 5 min, update
  bootloader.py update [--root DIR] [--security-only]   look for updates now
  bootloader.py status [--root DIR]

The operating system always starts `run`, never the station itself. It needs only
the Python standard library, so it works before anything else is installed.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.util
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PLATFORMS = ("pi", "debian", "docker", "windows", "macos")
REPO = "akienm/CC0AC-Weather"
CHECK_EVERY_S = 300            # the reading-stopped check (--check)
HEALTH_WAIT_S = 15 * 60        # a new version has this long to prove itself
HEALTH_POLL_S = 10
ALIVE = "running"        # touched every minute by `run`
ASKED = "update-now"     # `update` by hand, while `run` is running
REPORTING_WINDOW_S = 30 * 60   # a sensor heard this recently counts as reporting
DEFAULT_TIME = "06:00"
ZONEINFO = Path("/usr/share/zoneinfo")
JITTER_MIN = 20                # a few random minutes past the update time
MARK_TEXT = "Written by CC0AC-Weather's boot loader; it rewrites this file when it runs again."
MARK = "# " + MARK_TEXT
VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SECURITY_TAG = re.compile(r"^s(\d+)$")
STEP = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.py$")

log = logging.getLogger("bootloader")


# ── Versions ──────────────────────────────────────────────────────────────────

def read_version(tree: Path) -> tuple[tuple[int, int, int], int]:
    """A release's VERSION file, "1.2.0/1" → ((1, 2, 0), 1)."""
    text = (tree / "VERSION").read_text().strip()
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:/(\d+))?", text)
    if not m:
        raise ValueError(f"{tree / 'VERSION'} says {text!r}, not <major>.<minor>.<revision>/<security>")
    return (int(m[1]), int(m[2]), int(m[3])), int(m[4] or 0)


def vname(v: tuple[int, int, int], security: int = 0) -> str:
    """The folder a version lives in: 1.2.0, or 1.2.0-s2 once a security fix is laid over it."""
    return ".".join(map(str, v)) + (f"-s{security}" if security else "")


# ── Where releases come from ──────────────────────────────────────────────────

def _download(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"CC0AC-Weather boot loader (github.com/{REPO})"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _safe_extract(archive: Path, into: Path) -> Path:
    """Unpack a release archive, refusing anything that would land outside `into`."""
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="data")
        else:
            root = into.resolve()
            for m in tar.getmembers():
                if m.issym() or m.islnk() or not (root / m.name).resolve().is_relative_to(root):
                    raise ValueError(f"refusing {m.name} in {archive.name}")
            tar.extractall(into)
    tops = [p for p in into.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise ValueError(f"{archive.name} should hold one folder, not {len(tops)}")
    return tops[0]


class GitHub:
    """Releases are tags on GitHub: vX.Y.Z is a version, sN is security revision N.
    A tag is downloaded as a plain archive, so no git is needed."""

    def __init__(self, repo: str = REPO):
        self.repo = repo

    def tags(self) -> list[str]:
        body = _download(f"https://api.github.com/repos/{self.repo}/tags?per_page=100", timeout=30)
        return [t["name"] for t in json.loads(body)]

    def fetch(self, tag: str, dest: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / f"{tag}.tar.gz"
            archive.write_bytes(_download(f"https://github.com/{self.repo}/archive/refs/tags/{tag}.tar.gz"))
            (Path(tmp) / "x").mkdir()
            shutil.move(str(_safe_extract(archive, Path(tmp) / "x")), str(dest))


class Folder:
    """Releases in a folder, one subfolder per tag: for tests, and for a station with no internet."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def tags(self) -> list[str]:
        return sorted(p.name for p in self.path.iterdir() if p.is_dir())

    def fetch(self, tag: str, dest: Path) -> None:
        _copy_tree(self.path / tag, dest)


def _copy_tree(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "CLAUDE.md"))


def release_source(spec: str):
    return GitHub(spec[len("github:"):]) if spec.startswith("github:") else Folder(Path(spec))


def newest_version(tags: list[str], current: tuple[int, int, int]) -> str | None:
    """The newest vX.Y.Z past `current` in the same major version. A new minor version is
    taken as readily as a revision (UPDATES.md: Yes houses cross minor versions)."""
    found = [(tuple(map(int, m.groups())), t) for t in tags if (m := VERSION_TAG.match(t))]
    later = [(v, t) for v, t in found if v[0] == current[0] and v > current]
    return max(later)[1] if later else None


def newest_security(tags: list[str]) -> int:
    return max((int(m[1]) for t in tags if (m := SECURITY_TAG.match(t))), default=0)


# ── The boot loader's own files ───────────────────────────────────────────────

class Boot:
    """One install: ROOT/bootloader.json (how it was installed), ROOT/state.json (what it
    has done), ROOT/versions/<v>/ (the releases), ROOT/updates.log."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.settings = json.loads((self.root / "bootloader.json").read_text())
        self.state_path = self.root / "state.json"
        self.state = json.loads(self.state_path.read_text())
        self.lock = threading.Lock()   # one update at a time

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".saving")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    def version_dir(self, name: str | None = None) -> Path:
        return self.root / "versions" / (name or self.state["current"])

    @property
    def platform(self) -> str:
        return self.settings["platform"]

    @property
    def config(self) -> Path:
        return Path(self.settings["config"])

    def cfg(self) -> configparser.ConfigParser:
        c = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        try:
            c.read(self.config, encoding="utf-8")
        except (configparser.Error, OSError) as exc:
            log.warning("Couldn't read %s (%s); using the defaults", self.config, exc)
        station_timezone(c)
        return c

    def expand(self, path: str) -> Path:
        """A path from config.ini as the station sees it: ~ is its home (see station_env)."""
        if path.startswith("~"):
            return Path(self.settings["home"]) / path[1:].lstrip("/\\")
        return Path(path).expanduser()

    def as_station_user(self, cmd: list[str]) -> list[str]:
        """On Linux the boot loader runs as root and the station as its own user. It may
        open port 443 for the hub and 80 for the dashboard, and nothing else root can do."""
        user = self.settings.get("user")
        if not user or os.name != "posix" or os.geteuid() != 0:
            return cmd
        return ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups",
                "--inh-caps=+net_bind_service", "--ambient-caps=+net_bind_service", "--"] + cmd

    def station_env(self) -> dict:
        """The station's ~ is its home, so its own folder (~/.cc0ac-weather: raw logs, the hub's
        certificate, alert state) is kept with the readings, whoever starts it."""
        env = dict(os.environ, CC0AC_BOOTLOADER_ROOT=str(self.root), HOME=self.settings["home"])
        if os.name == "nt":
            env["USERPROFILE"] = self.settings["home"]
        if self.as_station_user([]):
            env["USER"] = env["LOGNAME"] = self.settings["user"]
        return env

    def station_cmd(self, *extra: str) -> list[str]:
        script = self.version_dir() / "acurite-capture.py"
        return self.as_station_user([self.settings["python"], str(script), "--config", str(self.config), *extra])

    def ctx(self, version_dir: Path, image: bool = False) -> SimpleNamespace:
        """What a migration or a security fix is handed."""
        return SimpleNamespace(
            platform=self.platform, root=self.root, config=self.config, home=Path(self.settings["home"]),
            user=self.settings.get("user"), python=self.settings["python"], version_dir=version_dir,
            hub_network=self.settings.get("hub_network", False),
            hub_interface=self.settings.get("hub_interface", "eth0"), image=image, mark=MARK, log=log,
            bootloader_cmd=[self.settings["python"], str(self.root / "bootloader.py"), "run"],
            run=_run, write_ours=write_ours, ours_or_absent=ours_or_absent)


def _run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    log.info("  $ %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, **kw)


def routed_ports() -> set[str]:
    """The ports a default route leaves by: this machine's ways to your network."""
    routed = set()
    for family in ("-4", "-6"):
        out = subprocess.run(["ip", family, "route", "show", "default"], capture_output=True, text=True).stdout
        routed |= set(re.findall(r"\bdev (\S+)", out))
    return routed


def reaches_outside(port: str, run=subprocess.run) -> bool:
    """Does this port actually get out? A route only says it is set up. Google's DNS
    answering a ping says it works; where pings are blocked, GitHub answering (the
    installer has just downloaded from it) says the same."""
    for cmd in (["ping", "-c", "1", "-W", "3", "-I", port, "8.8.8.8"],
                ["curl", "-fsS", "-o", "/dev/null", "--max-time", "10", "--interface", port, "https://github.com"]):
        try:
            if run(cmd, capture_output=True).returncode == 0:
                return True
        except OSError:   # no ping on this machine: the next way
            continue
    return False


def hub_ports(sys_net: Path = Path("/sys/class/net"), routed: set[str] | None = None) -> list[dict]:
    """The wired ports that could take the hub's cable: real Ethernet hardware (a USB
    adapter counts), not Wi-Fi, and not the port this machine reaches your network by.
    A port names itself after its place on the board (enp1s0) or, a USB adapter, after
    its own address (enx...), so a laptop's port is seldom eth0."""
    if routed is None:
        routed = routed_ports()
    ports = []
    for d in sorted(Path(sys_net).iterdir()):
        try:
            wired = (d / "type").read_text().strip() == "1" and (d / "device").exists()
        except OSError:
            continue
        if not wired or (d / "wireless").exists() or (d / "phy80211").exists() or d.name in routed:
            continue
        try:
            cable = (d / "carrier").read_text().strip() == "1"
        except OSError:   # a port that is down can't say
            cable = False
        ports.append({"name": d.name, "usb": "/usb" in os.path.realpath(d / "device"), "cable": cable})
    return ports


def ours_or_absent(path: Path) -> bool:
    """A file the boot loader wrote, or none at all, may be rewritten. A file someone wrote
    by hand is kept, so a machine set up by hand is left alone."""
    path = Path(path)
    try:
        return not path.exists() or any(MARK_TEXT in line
                                        for line in path.read_text(errors="replace").splitlines()[:3])
    except OSError:
        return False


def write_ours(path: Path, text: str, mode: int = 0o644, comment: tuple[str, str] = ("#", "")) -> None:
    """Write a file with the mark on its first line, or its second after a #! or <?xml line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mark = f"{comment[0]} {MARK_TEXT}{(' ' + comment[1]) if comment[1] else ''}"
    lines = text.splitlines()
    at = 1 if lines and (lines[0].startswith("#!") or lines[0].startswith("<?xml")) else 0
    body = "\n".join(lines[:at] + [mark] + lines[at:]) + "\n"
    tmp = path.with_name(path.name + ".cc0ac-new")
    tmp.write_text(body)
    os.chmod(tmp, mode)
    tmp.replace(path)


# ── Migrations and security fixes ─────────────────────────────────────────────

def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"cc0ac_step_{path.stem}", path)
    assert spec and spec.loader   # a path to a .py file
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def steps(version_dir: Path, kind: str = "migrations") -> list[Path]:
    folder = version_dir / "updates" / kind
    return sorted(p for p in folder.iterdir() if STEP.match(p.name)) if folder.is_dir() else []


def run_migrations(boot: Boot, version_dir: Path, image: bool = False) -> list[str]:
    """Run the release's migrations this install hasn't run yet, in number order. Each is
    safe to run twice and says which platforms it is for. Returns the names it ran."""
    ran = []
    for path in steps(version_dir):
        name = path.stem
        if name in boot.state["migrations"]:
            continue
        module = _load(path)
        if boot.platform in getattr(module, "PLATFORMS", PLATFORMS):
            log.info("Migration %s", name)
            module.apply(boot.ctx(version_dir, image))
            ran.append(name)
            if getattr(module, "NO_ROLLBACK", False):
                boot.state.setdefault("no_rollback", []).append(name)
        boot.state["migrations"].append(name)
        boot.save()
    return ran


def apply_security(boot: Boot, version_dir: Path, fixes_tree: Path, upto: int) -> list[int]:
    """Lay the security fixes from `fixes_tree` numbered past this install's security
    revision, up to `upto`, over `version_dir`. A fix only adds or overlays files."""
    done = []
    for path in steps(fixes_tree, "security"):
        n = int(path.name[:4])
        if n <= boot.state["security"] or n > upto:
            continue
        module = _load(path)
        applies = getattr(module, "applies", None)
        if applies is None or applies(boot.state["current"]):
            log.info("Security fix %s", path.stem)
            module.apply(boot.ctx(version_dir))
        else:
            log.info("Security fix %s isn't needed by %s", path.stem, boot.state["current"])
        done.append(n)
    return done


# ── The station, as a child process ───────────────────────────────────────────

class Station:
    def __init__(self, boot: Boot):
        self.boot = boot
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.wanted = True

    def _output(self):
        """systemd and Docker keep the station's output; on Windows and macOS it goes to station.log."""
        if self.boot.platform not in ("windows", "macos"):
            return None
        path = self.boot.root / "station.log"
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(path.with_suffix(".log.1"))
        return open(path, "ab")

    def start(self) -> None:
        env = self.boot.station_env()
        out = self._output()
        cmd = self.boot.station_cmd()
        log.info("Starting the station: %s", self.boot.state["current"])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT if out else None,
                                     cwd=self.boot.version_dir(), creationflags=flags)
        self.started = time.time()
        if out:
            out.close()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# ── Health check ──────────────────────────────────────────────────────────────

def _current_json(boot: Boot) -> dict:
    write_path = boot.expand(boot.cfg().get("device", "write_path", fallback="~/AcuRite"))
    try:
        return json.loads((write_path / "current.json").read_text())
    except (OSError, ValueError):
        return {}


def _utc(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def reporting(boot: Boot) -> list[str]:
    """The sensors heard in the last half hour: the ones a new version must hear again."""
    now = time.time()
    out = []
    for s in _current_json(boot).get("sensors", []):
        try:
            if now - _utc(s["updated"]) < REPORTING_WINDOW_S:
                out.append(s["sensor_id"])
        except (KeyError, ValueError):
            pass
    return out


def dashboard_port(boot: Boot) -> int | None:
    c = boot.cfg()
    if not c.getboolean("web", "enabled", fallback=False):
        return None
    ports = [p.strip() for p in c.get("web", "port", fallback="12345").split(",") if p.strip()]
    return int(ports[-1]) if ports else None


def dashboard_answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/current.json", timeout=10) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except OSError:
        return False


def healthy(boot: Boot, station: Station, before: list[str], since: float,
            wait_s: float = HEALTH_WAIT_S) -> tuple[bool, str]:
    """Within `wait_s`: the station is running, the dashboard answers, and a new reading has
    come from a sensor that was reporting before the switch. Returns (ok, what was missing)."""
    port = dashboard_port(boot)
    deadline = time.time() + wait_s
    while True:
        running = station.alive()
        answers = port is None or (running and dashboard_answers(port))
        heard = not before or any(
            s["sensor_id"] in before and _utc(s["updated"]) > since
            for s in _current_json(boot).get("sensors", []) if "updated" in s)
        if running and answers and heard:
            return True, ""
        if time.time() >= deadline:
            missing = [w for w, ok in (("the station isn't running", running),
                                       (f"the dashboard (port {port}) doesn't answer", answers),
                                       (f"no new reading from {', '.join(before)}", heard)) if not ok]
            return False, "; ".join(missing)
        time.sleep(HEALTH_POLL_S)


def station_timezone(c: configparser.ConfigParser) -> None:
    """[station] timezone, so the update time is the station's own wherever the
    computer's clock is set (a container's is usually UTC). The program it starts
    inherits it, and sets it again itself."""
    tz = c.get("station", "timezone", fallback="").strip()
    if not tz or os.environ.get("TZ") == tz:
        return
    if not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", tz) or not (ZONEINFO / tz).is_file():
        log.error("[station] timezone = %s is not a time zone this computer has (%s); "
                  "using the computer's own", tz, ZONEINFO)
        return
    os.environ["TZ"] = tz
    time.tzset()
    log.info("Time zone: %s", tz)


# ── Updating ──────────────────────────────────────────────────────────────────

def policy(boot: Boot) -> tuple[bool, str]:
    c = boot.cfg()
    automatic = c.getboolean("updates", "automatic", fallback=True)
    when = c.get("updates", "time", fallback=DEFAULT_TIME).strip() or DEFAULT_TIME
    if not re.fullmatch(r"\d{1,2}:\d{2}", when):
        log.warning("[updates] time = %s isn't HH:MM; using %s", when, DEFAULT_TIME)
        when = DEFAULT_TIME
    return automatic, when


def due(boot: Boot, now: datetime) -> str | None:
    """"security", "all" (on the station's own weekday) or None: what to check now."""
    automatic, when = policy(boot)
    if not automatic or boot.state.get("last_check") == now.date().isoformat():
        return None
    h, m = map(int, when.split(":"))
    at = now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(minutes=m + boot.state["jitter_min"])
    if now < at:
        return None
    return "all" if now.weekday() == boot.state["weekday"] else "security"


def upkeep_due(boot: Boot, now: datetime) -> str | None:
    """"disks", "disks+backup" (on the station's weekday) or None. Daily, half an hour after
    the update time, so it never lands in a new version's trial; with updates off too."""
    if boot.state.get("upkeep_day") == now.date().isoformat():
        return None
    _, when = policy(boot)
    h, m = map(int, when.split(":"))
    at = now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(minutes=m + boot.state["jitter_min"] + 30)
    if now < at:
        return None
    return "disks+backup" if now.weekday() == boot.state["weekday"] else "disks"


def upkeep(boot: Boot, what: str) -> list[tuple[str, bool]]:
    """Run upkeep.py from the current version: the disks as root, the backup as the station's
    user (it owns the files). Returns (line, worked) per job; a version without it does nothing."""
    script = boot.version_dir() / "upkeep.py"
    if not script.exists():
        return []
    jobs: list[tuple[str, list[str], dict | None]] = []
    if "disks" in what:
        jobs.append(("disks", [boot.settings["python"], str(script), "disks", "--out", str(boot.root / "disks.json")], None))
    if "backup" in what and boot.cfg().get("backup", "path", fallback="").strip():
        cmd = boot.as_station_user([boot.settings["python"], str(script), "backup", "--config", str(boot.config)])
        jobs.append(("backup", cmd, boot.station_env()))
    said = []
    for name, cmd, env in jobs:
        try:
            out = subprocess.run(cmd, env=env, cwd=boot.version_dir(), capture_output=True, text=True, timeout=1800)
            line = (out.stdout.strip() or out.stderr.strip()[-300:] or f"exit {out.returncode}")
            worked = out.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            line, worked = f"didn't run: {exc}", False
        log.log(logging.INFO if worked else logging.WARNING, "Upkeep, %s: %s", name, line)
        said.append((f"{name}: {line}", worked))
    return said


def switch(boot: Boot, station: Station | None, new: str, why: str, ran: list[str],
           wait_s: float = HEALTH_WAIT_S) -> bool:
    """Move the station to versions/<new>, prove it, and roll back if it doesn't prove itself."""
    old = boot.state["current"]
    before = reporting(boot)
    if station is None:   # `update` run by hand: the service picks it up when it next starts
        boot.state.update(previous=old, current=new, trial={"before": before, "ran": ran, "why": why})
        boot.save()
        log.info("%s ready in versions/%s; restart the service to use it", why, new)
        return True
    log.info("%s: switching %s → %s (must hear again: %s)", why, old, new, ", ".join(before) or "no sensor yet")
    station.stop()
    boot.state.update(previous=old, current=new, trial={"before": before, "ran": ran, "why": why})
    boot.save()
    since = time.time()
    station.start()
    return judge(boot, station, since, wait_s)


def judge(boot: Boot, station: Station, since: float, wait_s: float = HEALTH_WAIT_S) -> bool:
    """The health check on the version on trial, and the rollback if it fails."""
    trial, new, old = boot.state["trial"], boot.state["current"], boot.state["previous"]
    ok, missing = healthy(boot, station, trial["before"], since, wait_s)
    if ok:
        log.info("%s is healthy", new)
        boot.state["trial"] = None
        boot.save()
        _prune(boot)
        return True
    blocked = [m for m in trial["ran"] if m in boot.state.get("no_rollback", [])]
    if blocked or not old:
        log.error("%s is NOT healthy (%s), and %s, so it stays", new, missing,
                  f"migration {', '.join(blocked)} can't be undone" if blocked else "there is nothing to go back to")
        boot.state["trial"] = None
        boot.save()
        return False
    log.error("%s is NOT healthy (%s); rolling back to %s", new, missing, old)
    station.stop()
    boot.state.update(current=old, previous=None, trial=None, failed=boot.state.get("failed", []) + [new])
    boot.save()
    station.start()
    return False


def _prune(boot: Boot) -> None:
    keep = {boot.state["current"], boot.state.get("previous")}
    for d in (boot.root / "versions").iterdir():
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def check_for_updates(boot: Boot, station: Station | None, what: str = "all",
                      wait_s: float = HEALTH_WAIT_S) -> str:
    """One scheduled check. Returns a line for the log."""
    with boot.lock:
        source = release_source(boot.settings["releases"])
        tags = source.tags()
        current_v, _ = read_version(boot.version_dir())
        report = []
        if what == "all":
            tag = newest_version(tags, current_v)
            if tag:   # one that failed before is tried again: UPDATES.md, "Health check and rollback"
                staging = boot.root / "versions" / f".{tag}.new"
                shutil.rmtree(staging, ignore_errors=True)
                source.fetch(tag, staging)
                v, sec = read_version(staging)
                name = vname(v)
                target = boot.version_dir(name)
                shutil.rmtree(target, ignore_errors=True)
                staging.rename(target)
                ran = run_migrations(boot, target)
                ok = switch(boot, station, name, f"Version {tag}", ran, wait_s)
                if ok:
                    boot.state["security"] = max(boot.state["security"], sec)
                    boot.save()
                    _refresh_self(boot, station)
                report.append(f"{tag}: {'installed' if ok else 'rolled back'}")
        top = newest_security(tags)
        if top > boot.state["security"]:
            with tempfile.TemporaryDirectory() as tmp:
                fixes = Path(tmp) / "fixes"
                source.fetch(f"s{top}", fixes)
                v, _ = read_version(boot.version_dir())
                name = vname(v, top)
                target = boot.version_dir(name)
                shutil.rmtree(target, ignore_errors=True)
                _copy_tree(boot.version_dir(), target)   # the fix goes on a copy, so rolling back is a switch
                done = apply_security(boot, target, fixes, top)
            ok = switch(boot, station, name, f"Security revision {top}", [], wait_s)
            if ok:
                boot.state["security"] = top
                boot.save()
            report.append(f"s{top} ({len(done)} fix(es)): {'installed' if ok else 'rolled back'}")
        return "; ".join(report) or "up to date"


def _refresh_self(boot: Boot, station: Station | None) -> None:
    """A new version may carry a new boot loader: put it in place and start it."""
    new = boot.version_dir() / "updates" / "bootloader.py"
    mine = boot.root / "bootloader.py"
    if not new.exists() or (mine.exists() and _sha(new) == _sha(mine)):
        return
    shutil.copy2(new, mine)
    log.info("The boot loader itself was updated")
    if station is not None:
        station.wanted = False
        station.stop()
        os.execv(sys.executable, [sys.executable, str(mine), "run", "--root", str(boot.root)])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── Commands ──────────────────────────────────────────────────────────────────

def setup_logging(root: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(root / "updates.log", maxBytes=1_000_000,
                                                             backupCount=1, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers, force=True)


def install(args) -> Boot:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "versions").mkdir(exist_ok=True)
    settings_path = root / "bootloader.json"
    settings = {"platform": args.platform, "config": str(Path(args.config)), "home": str(Path(args.home)),
                "user": args.user, "python": args.python or sys.executable,
                "releases": args.releases, "hub_network": args.hub_network,
                "hub_interface": getattr(args, "hub_interface", None) or "eth0"}
    settings_path.write_text(json.dumps(settings, indent=1))
    if not (root / "state.json").exists():
        (root / "state.json").write_text(json.dumps({
            "current": None, "previous": None, "migrations": [], "security": 0,
            "weekday": random.randrange(7), "jitter_min": random.randrange(JITTER_MIN),
            "last_check": date.today().isoformat(), "installed": datetime.now().isoformat(timespec="seconds")}))
    boot = Boot(root)
    if boot.state["current"] and boot.version_dir().is_dir() and not args.source and not args.tag:
        log.info("Already installed (%s); it keeps itself up to date", boot.state["current"])
        tree = boot.version_dir()
    else:
        staging = root / "versions" / ".install.new"
        shutil.rmtree(staging, ignore_errors=True)
        if args.source:
            _copy_tree(Path(args.source), staging)
        else:
            source = release_source(args.releases)
            tag = args.tag or newest_version(source.tags(), (1, 0, 0)) or ""
            if not tag:
                raise SystemExit("No release is published yet; install with --from a copy of the program")
            source.fetch(tag, staging)
        v, sec = read_version(staging)
        tree = boot.version_dir(vname(v))
        if tree.exists() and boot.state["current"] == tree.name:
            shutil.rmtree(staging)   # the running version stays; the copy given is the same version
        else:
            shutil.rmtree(tree, ignore_errors=True)
            staging.rename(tree)
        boot.state.update(current=tree.name, security=max(boot.state["security"], sec))
        boot.save()
    shutil.copy2(tree / "updates" / "bootloader.py", root / "bootloader.py")
    ran = run_migrations(boot, tree, image=args.image)
    log.info("Installed %s in %s (ran %d migration(s))", boot.state["current"], root, len(ran))
    return boot


def run(boot: Boot) -> None:
    station = Station(boot)
    stopping = threading.Event()

    def on_signal(*_):
        stopping.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)

    station.start()
    if boot.state.get("trial"):   # stopped part way through proving a version: prove it from now
        threading.Thread(target=_reprove, args=(boot, station, time.time()), daemon=True).start()
    last_check = time.time()
    last_alert_out = None
    alive_at = 0.0
    while not stopping.wait(1):
        if time.time() - alive_at >= 60:   # so `update` by hand knows to ask this loop instead
            alive_at = time.time()
            (boot.root / ALIVE).write_text(str(os.getpid()))
        if station.wanted and not station.alive():
            rc = station.proc.returncode if station.proc else None
            if rc == 0:
                log.info("The station stopped to restart (settings saved)")
                time.sleep(1)
            else:
                log.warning("The station stopped (exit %s); starting it again in 10 s", rc)
                if stopping.wait(10):
                    break
            if station.wanted and not station.alive():
                station.start()
        if time.time() - last_check >= CHECK_EVERY_S:
            last_check = time.time()
            try:
                out = subprocess.run(boot.station_cmd("--check"), env=boot.station_env(),
                                     capture_output=True, text=True, timeout=120)
                line = (out.stdout or out.stderr).strip()
                if line != last_alert_out:
                    log.info("Readings check: %s", line)
                    last_alert_out = line
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("Readings check didn't run: %s", exc)
        asked = boot.root / ASKED
        if asked.exists() and not boot.lock.locked():
            what = asked.read_text().strip() or "all"
            asked.unlink()
            log.info("Asked by hand to check for updates (%s)", what)
            threading.Thread(target=_scheduled, args=(boot, station, what), daemon=True).start()
            continue
        chore = upkeep_due(boot, datetime.now())
        if chore and not boot.lock.locked():
            boot.state["upkeep_day"] = date.today().isoformat()
            boot.save()
            threading.Thread(target=upkeep, args=(boot, chore), daemon=True).start()
        check = due(boot, datetime.now())
        if check and not boot.lock.locked():
            boot.state["last_check"] = date.today().isoformat()
            boot.save()
            threading.Thread(target=_scheduled, args=(boot, station, check), daemon=True).start()
    log.info("Stopping")
    station.wanted = False
    station.stop()
    (boot.root / ALIVE).unlink(missing_ok=True)


def _scheduled(boot: Boot, station: Station, what: str) -> None:
    try:
        log.info("Scheduled check (%s): %s", what, check_for_updates(boot, station, what))
    except Exception as exc:   # the next scheduled check tries again
        log.error("Scheduled check (%s) failed: %s", what, exc)


def _reprove(boot: Boot, station: Station, since: float) -> None:
    with boot.lock:
        judge(boot, station, since)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="CC0AC-Weather's boot loader (UPDATES.md)")
    sub = p.add_subparsers(dest="cmd", required=True)
    here = Path(__file__).resolve().parent
    i = sub.add_parser("install", help="install the station, or finish an install that stopped")
    i.add_argument("--platform", required=True, choices=PLATFORMS)
    i.add_argument("--root", required=True, help="where the boot loader and the versions live")
    i.add_argument("--config", required=True, help="config.ini")
    i.add_argument("--home", required=True, help="the station's own folder (readings and settings)")
    i.add_argument("--user", help="Linux: the user the station runs as")
    i.add_argument("--python", help="the Python that runs the station (default: this one)")
    i.add_argument("--from", dest="source", help="install this copy of the program, not a download")
    i.add_argument("--tag", help="install this release tag, not the newest")
    i.add_argument("--releases", default=f"github:{REPO}", help="github:<owner>/<repo>, or a folder of releases")
    i.add_argument("--image", action="store_true", help="building an SD card image: write, start nothing")
    i.add_argument("--no-hub-network", dest="hub_network", action="store_false",
                   help="Pi/Debian: leave the network port alone (no AcuRite hub on the cable)")
    i.add_argument("--hub-interface", help="Pi/Debian: the port the hub's cable plugs into (default eth0)")
    i.add_argument("--then-run", action="store_true", help="Docker: install if needed, then run")
    for name, text in (("run", "the service"), ("update", "look for updates now"), ("status", "say what's installed"),
                       ("diagnostics", "make the file to attach to a problem report"),
                       ("backup", "back up now"), ("disks", "check the disks now")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--root", default=str(here))
        if name == "update":
            s.add_argument("--security-only", action="store_true")
    sub.add_parser("ports", help="the wired ports that could take the hub's cable, for the installer")
    o = sub.add_parser("outside", help="the ports that reach the internet, besides the hub's; for the installer")
    o.add_argument("--except", dest="hub", help="the hub's port, never counted")
    args = p.parse_args(argv)
    if args.cmd == "outside":   # one per line; none at all exits 1
        found = [port for port in sorted(routed_ports() - {args.hub}) if reaches_outside(port)]
        print("\n".join(found))
        sys.exit(0 if found else 1)
    if args.cmd == "ports":   # one per line: name, usb or board, cable or none
        for port in hub_ports():
            print(port["name"], "usb" if port["usb"] else "board", "cable" if port["cable"] else "none")
        return
    root = Path(args.root).resolve()
    setup_logging(root)
    if args.cmd == "install":
        if args.platform != "windows" and sys.platform == "win32":
            raise SystemExit("On Windows use --platform windows")
        boot = install(args)
        if args.then_run:
            run(boot)
        return
    boot = Boot(root)
    if args.cmd == "run":
        run(boot)
    elif args.cmd == "update":
        what = "security" if args.security_only else "all"
        alive = boot.root / ALIVE
        if alive.exists() and time.time() - alive.stat().st_mtime < 150:
            # The running boot loader owns state.json and the station, so it does the work.
            (boot.root / ASKED).write_text(what)
            print(f"Asked the running boot loader to check; see {root / 'updates.log'}")
        else:
            print(check_for_updates(boot, None, what))
    elif args.cmd in ("backup", "disks"):
        said = upkeep(boot, args.cmd)
        if not said:
            said = [("this version has no upkeep.py" if not (boot.version_dir() / "upkeep.py").exists()
                     else "no [backup] path in config.ini", False)]
        print("\n".join(line for line, _ in said))
        sys.exit(0 if all(worked for _, worked in said) else 1)
    elif args.cmd == "diagnostics":
        out = subprocess.run(boot.station_cmd("--diagnostics"), env=boot.station_env(),
                             cwd=boot.version_dir(), capture_output=True, text=True)
        print((out.stdout + out.stderr).strip())
        raise SystemExit(out.returncode)
    elif args.cmd == "status":
        s = boot.state
        automatic, when = policy(boot)
        day = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][s["weekday"]]
        print(f"version {s['current']} (security revision {s['security']}), previous {s['previous'] or 'none'}\n" +
              (f"updates: yes, daily at {when} + {s['jitter_min']} min, new versions on {day}s; "
               f"last checked {s.get('last_check')}\n" if automatic else "updates: no\n") +
              f"migrations run: {len(s['migrations'])}; platform {boot.platform}; log {root / 'updates.log'}")


if __name__ == "__main__":
    main()
