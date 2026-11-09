"""Private values kept out of config.ini: passwords, keys, alert addresses, the location.

A setting may point into a file instead of holding its value:

    smtp_password = file:secrets.env#ALERTS_SMTP_PASSWORD
    smtp_password = file:///home/pi/.cc0ac-secrets#SMTP_PASSWORD

The path is the file (``~`` is your home; a path that isn't absolute is beside config.ini)
and the part after ``#`` is the name of a ``NAME=value`` line in it. A plain value works as
it always has. So config.ini can be shown to someone, or sent with a problem report, and
carry nothing private: what is private is in the other file.

The setup page writes every private value it saves into secrets.env beside config.ini
(readable by you alone) and leaves the reference in config.ini.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("acurite")

SECRETS_FILE = "secrets.env"
# Settings whose values are someone's secret or identity, in any section.
PRIVATE_KEYS = frozenset((
    "station_id", "station_key", "password", "passcode", "key", "api_key", "user", "smtp_user",
    "smtp_password", "ntfy_topic", "email_to", "email_from", "healthcheck_url", "hub_id", "station",
    "token", "header", "username", "domain", "latitude", "longitude"))
# Not private, though the name says so: [web] key is where the TLS key file is.
NOT_PRIVATE = frozenset((("web", "key"),))
# Read without %(name)s by the code that uses them, so moved as written.
RAW_READ = frozenset((("backup", "password"),))
_REF = re.compile(r"file:\S*#[A-Za-z_][A-Za-z0-9_]*")


class Missing(ValueError):
    """A reference whose file or name isn't there."""


def is_reference(value: str | None) -> bool:
    return bool(value) and bool(_REF.fullmatch(value.strip()))


def _path(url: str, base: Path) -> tuple[Path, str]:
    u = urlsplit(url.strip())
    # file:///abs, file:rel and file:~/x are all paths; file://name/x (a "host") is read as a path too.
    raw = unquote((u.netloc if u.netloc not in ("", "localhost") else "") + u.path)
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else base / p), u.fragment


def read_file(path: Path) -> dict[str, str]:
    """The NAME=value lines of a secrets file. Blank lines and lines starting # are skipped;
    quotes around a value are taken off."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[name.strip().removeprefix("export ").strip()] = value
    return out


_warned: set[Path] = set()


def resolve(value: str, base: Path) -> str:
    """The value a setting stands for: itself, or what its reference points at."""
    if not is_reference(value):
        return value
    path, name = _path(value, base)
    try:
        values = read_file(path)
    except OSError as e:
        raise Missing(f"{value.strip()}: can't read {path} ({e.strerror or e})") from None
    if name not in values:
        raise Missing(f"{value.strip()}: {path} has no {name}= line")
    if os.name == "posix" and path not in _warned:
        try:
            if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                _warned.add(path)
                log.warning("%s can be read by others on this computer; chmod 600 %s", path, path)
        except OSError:
            pass
    return values[name]


class Interpolation(configparser.BasicInterpolation):
    """configparser's %(name)s, and a file: reference read from its file. What the file
    holds is used as it is (a % in a password is just a %)."""

    def __init__(self, base: Path):
        super().__init__()
        self.base = base

    def before_get(self, parser, section, option, value, defaults):  # type: ignore[override]
        if is_reference(value):
            try:
                return resolve(value, self.base)
            except Missing as e:
                log.error("[%s] %s = %s", section, option, e)
                return ""
        return super().before_get(parser, section, option, value, defaults)


def parser(config_path: Path | None = None, **kwargs) -> configparser.ConfigParser:
    """A ConfigParser that reads config.ini as the station does, references and all.
    ``config_path``: the config file, whose folder a relative reference starts from."""
    base = Path(config_path).expanduser().resolve().parent if config_path else Path.home() / ".cc0ac-weather"
    return configparser.ConfigParser(interpolation=Interpolation(base), **kwargs)


def get_raw(cfg: configparser.ConfigParser, section: str, option: str, fallback: str = "") -> str:
    """A setting read without %(name)s (a password may hold %), its reference still followed."""
    value = cfg.get(section, option, raw=True, fallback=fallback)
    interp = getattr(cfg, "_interpolation", None)
    if is_reference(value) and isinstance(interp, Interpolation):
        try:
            return resolve(value, interp.base)
        except Missing as e:
            log.error("[%s] %s = %s", section, option, e)
            return ""
    return value


def problems(cfg: configparser.ConfigParser) -> list[str]:
    """Every reference in the settings that doesn't resolve, said without its value."""
    interp = getattr(cfg, "_interpolation", None)
    base = interp.base if isinstance(interp, Interpolation) else Path.home() / ".cc0ac-weather"
    out = []
    for section in cfg.sections():
        for option, value in cfg.items(section, raw=True):
            if is_reference(value):
                try:
                    resolve(value, base)
                except Missing as e:
                    out.append(f"[{section}] {option}: {e}")
    return out


def files(cfg: configparser.ConfigParser) -> list[Path]:
    """The secrets files the settings point into, each once (for the backup)."""
    interp = getattr(cfg, "_interpolation", None)
    base = interp.base if isinstance(interp, Interpolation) else Path.home() / ".cc0ac-weather"
    seen: list[Path] = []
    for section in cfg.sections():
        for _, value in cfg.items(section, raw=True):
            if is_reference(value):
                p = _path(value, base)[0]
                if p.exists() and p not in seen:
                    seen.append(p)
    return seen


def secret_name(section: str, option: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{section}_{option}").strip("_").upper()


def write_values(path: Path, values: dict[str, str]) -> None:
    """Set NAME=value lines in a secrets file, keeping its other lines; made readable by
    its owner alone, and replaced in one step."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else [
        "# Private settings for CC0AC-Weather, pointed at from config.ini (file:secrets.env#NAME).",
        "# Keep this file to yourself: don't send it with a problem report."]
    todo = dict(values)
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if name in todo:
            lines[i] = f"{name}={todo.pop(name)}"
    lines += [f"{k}={v}" for k, v in todo.items()]
    tmp = path.with_name(path.name + ".saving")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    if hasattr(os, "geteuid") and os.geteuid() == 0:    # the boot loader, as root: the station's user must read it
        owner = path.parent.stat()
        os.chown(tmp, owner.st_uid, owner.st_gid)
    tmp.replace(path)


def move_private(text: str, config_path: Path) -> str:
    """config.ini's text with every plain private value moved into secrets.env beside it
    and a reference left in its place. References and blank values are left as they are."""
    from cc0ac import wizard   # wizard imports nothing of this module's at load
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str  # type: ignore[assignment,method-assign]   # keep the keys' case
    cfg.read_string(text)
    plain = configparser.ConfigParser()   # what the station reads: %% is one %
    plain.optionxform = str  # type: ignore[assignment,method-assign]
    plain.read_string(text)
    secrets_path = Path(config_path).expanduser().resolve().parent / SECRETS_FILE
    moved: dict[str, str] = {}
    for section in cfg.sections():
        refs: dict[str, str | None] = {}
        for option, value in cfg.items(section):
            v = value.strip()
            where = (section.lower(), option.lower())
            if option.lower() in PRIVATE_KEYS and where not in NOT_PRIVATE and v and not is_reference(v):
                if where not in RAW_READ:
                    try:
                        v = plain.get(section, option).strip()
                    except configparser.Error:
                        pass        # a % the station couldn't read either: kept as written
                name = secret_name(section, option)
                moved[name] = v
                refs[option] = f"file:{SECRETS_FILE}#{name}"
        if refs:
            text = wizard.set_values(text, section, refs)
    if moved:
        write_values(secrets_path, moved)
    return text
