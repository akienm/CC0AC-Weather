"""The web server: the dashboard's pages and data, /config.cgi, /status, /history.json and the rest."""

from __future__ import annotations

import configparser
import ipaddress
import json
import logging
import re
import sqlite3
import tempfile
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from cc0ac import alerts, diagnostics, mcp_server, secure_web, units, upkeep, wizard
from cc0ac.config import (
    CONFIG_MAX_BYTES,
    DEFAULT_TITLE,
    ROOT,
    Query,
    SensorNames,
    check_config,
    database_path,
    load_config,
    page_title,
    restart_soon,
    save_config,
    sensor_map,
    station_version,
)
from cc0ac.status import LOG_PATH, log_folder, system_status
from cc0ac.store import history, noaa_summary, summaries

log = logging.getLogger("acurite")


SHIPPED_PAGES = ("weather.html", "charts.html", "theme.css", "units.js")

PAGE_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
              ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
              ".ico": "image/x-icon", ".webp": "image/webp"}

PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

DATA_FILES = {"/current.js": "application/javascript", "/current.json": "application/json",
              "/forecast.js": "application/javascript", "/forecast.json": "application/json",
              "/climate.js": "application/javascript", "/climate.json": "application/json",
              "/history.csv": "text/csv"}

# From outside your network (anything through the router's port forward) only the
# dashboard and the charts are served, with what those two pages load; everything
# else answers 404. Your own network gets every page, the data files and /config.cgi.
OUTSIDE_PATHS = {"/", "/weather.html", "/charts.html", "/theme.css", "/units.js",
                 "/current.js", "/forecast.js", "/climate.js", "/history.json"}


def _local_client(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _local_host(host: str) -> bool:
    """The address the browser asked for: an address on your network, localhost,
    or a bare or .local machine name. Another site's name pointed at this machine
    (DNS rebinding) is refused."""
    if host.startswith("["):                       # [IPv6]:port
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".").lower()
    if not name:
        return False
    if name == "localhost" or name.endswith(".local") or "." not in name and ":" not in name:
        return True
    return _local_client(name)


def _find_page(name: str, own_dir: Path | None, page_dir: Path) -> Path | None:
    """The file to serve for /<name>: yours first, then the shipped one."""
    if not PAGE_NAME.fullmatch(name) or Path(name).suffix.lower() not in PAGE_TYPES:
        return None
    if own_dir is not None and (own_dir / name).is_file():
        return own_dir / name
    return page_dir / name if name in SHIPPED_PAGES else None


def _make_web_handler(write_path: Path, db_path: Path | None = None, sensors: SensorNames | None = None,
                      own_dir: Path | None = None, title: str = DEFAULT_TITLE,
                      config_path: Path | None = None, mcp: bool = True,
                      cfg: configparser.ConfigParser | None = None) -> type[BaseHTTPRequestHandler]:
    page_dir = ROOT
    # The charts read the [units] choice from /history.json.
    shown_in = units.chosen(cfg) if cfg is not None else units.SYSTEMS["us"]

    def read_data(name: str) -> dict | None:
        try:
            return json.loads((write_path / name).read_text())
        except (OSError, ValueError):
            return None

    def query(answer: Callable[[Path, dict, dict], dict]) -> Callable[[dict], dict]:
        def run(q: Query) -> dict:
            if db_path is None or not db_path.exists():
                raise ValueError("no readings stored yet")
            return answer(db_path, sensors or {}, q)
        return run

    mcp_server_ = mcp_server.Server(mcp_server.Station(read_data, query(history), query(summaries)),
                                    station_version()) if mcp else None

    class WebHandler(BaseHTTPRequestHandler):
        # A visitor that connects and then says nothing would hold its thread forever.
        timeout = 30
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path not in OUTSIDE_PATHS and not _local_client(self.client_address[0]):
                self.send_error(404)
                return
            if path == "/config.cgi":
                self._config_page(dict(urllib.parse.parse_qsl(url.query)))
                return
            if path == "/mcp" and mcp_server_ is not None:
                # No server-sent stream to open: every answer comes back on its POST.
                self.send_response(405)
                self.send_header("Allow", "POST")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ("/setup", "/setup.json"):
                self._setup_page(path)
                return
            if path in ("/status", "/status.json", "/log", "/diagnostics") and cfg is not None:
                self._status(path, dict(urllib.parse.parse_qsl(url.query)))
                return
            # Not set up yet: a visit from home goes to the setup page first.
            if (path in ("/", "/weather.html") and config_path is not None
                    and _local_host(self.headers.get("Host", "")) and self._unconfigured()):
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ("/noaa", "/noaa.json", "/noaa.txt") and cfg is not None:
                self._noaa(path, dict(urllib.parse.parse_qsl(url.query)))
                return
            if path in ("/history.json", "/summaries.json"):
                self._query(history if path == "/history.json" else summaries,
                            dict(urllib.parse.parse_qsl(url.query)))
                return
            if path in DATA_FILES:
                target, ctype = write_path / path.lstrip("/"), DATA_FILES[path]
            else:
                found = _find_page(path.lstrip("/") or "weather.html", own_dir, page_dir)
                if found is None:
                    self.send_error(404)
                    return
                target = found
                ctype = PAGE_TYPES[target.suffix.lower()]
            try:
                body = target.read_bytes()
            except FileNotFoundError:
                self.send_error(404, "No data yet")
                return
            self._send(body, ctype)

        def _config_allowed(self) -> bool:
            if (config_path is not None and _local_client(self.client_address[0])
                    and _local_host(self.headers.get("Host", ""))):
                return True
            self.send_error(404)
            return False

        def _config_page(self, q: Query) -> None:
            if not self._config_allowed():
                return
            assert config_path is not None   # _config_allowed
            if q.get("raw"):
                self._send(config_path.read_bytes(), "text/plain; charset=utf-8")
            else:
                self._send((page_dir / "config.html").read_bytes(), PAGE_TYPES[".html"])

        def _status(self, path: str, q: Query) -> None:
            if not self._config_allowed():
                return
            assert cfg is not None   # do_GET routes /status here only with a config
            if path == "/status":
                self._send((page_dir / "status.html").read_bytes(), PAGE_TYPES[".html"])
            elif path == "/status.json":
                self._send(json.dumps(system_status(cfg, write_path)).encode(), "application/json")
            elif path == "/diagnostics":
                with tempfile.TemporaryDirectory() as d:
                    made = diagnostics.bundle(cfg, Path(d))
                    body = made.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", f'attachment; filename="{made.name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif q.get("f") == "service":
                self._send(diagnostics.service_log().encode(), "text/plain; charset=utf-8")
            else:
                m = LOG_PATH.fullmatch(q.get("f", ""))
                folder = log_folder(cfg, m.group(1)) if m else None
                target = folder / m.group(2) if m and folder else None
                if target is None or not target.is_file():
                    self.send_error(404)
                    return
                text = target.read_text(errors="replace")
                find = q.get("find", "")
                if find:
                    text = "".join(line for line in text.splitlines(keepends=True) if find in line)
                self._send(text.encode(), "text/plain; charset=utf-8")

        def _unconfigured(self) -> bool:
            assert config_path is not None   # do_GET checks before asking
            try:
                return wizard.needs_setup(load_config(config_path))
            except SystemExit:
                return False

        def _setup_page(self, path: str) -> None:
            if not self._config_allowed():
                return
            if path == "/setup":
                self._send((page_dir / "setup.html").read_bytes(), PAGE_TYPES[".html"])
                return
            assert config_path is not None   # _config_allowed
            state = wizard.setup_state(load_config(config_path))
            self._send(json.dumps(state).encode(), "application/json")

        def do_POST(self) -> None:
            target = urllib.parse.urlparse(self.path).path
            if target == "/mcp" and mcp_server_ is not None:
                self._mcp()
                return
            if target not in ("/config.cgi", "/setup.cgi"):
                self.send_error(404)
                return
            if not self._config_allowed():
                return
            # Only the page's own script sends this header; a form on another
            # site can't, so it can't save over your config from your browser.
            if self.headers.get("X-CC0AC-Config") != "save":
                self.send_error(403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= CONFIG_MAX_BYTES:
                self._answer(400, "Not saved: empty, or too big for a config file.")
                return
            assert config_path is not None   # _config_allowed
            try:
                text = self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError:
                self._answer(400, "Not saved: the text isn't UTF-8.")
                return
            if target == "/setup.cgi":
                answered = self._setup_answers(text)
                if answered is None:
                    return
                text = answered
            problem = check_config(text)
            if problem:
                self._answer(400, problem)
                return
            try:
                save_config(config_path, text)
            except OSError as e:
                log.error("Saving %s failed: %s", config_path, e)
                self._answer(500, f"Not saved: {e}")
                return
            log.info("config.ini saved from %s; restarting", self.client_address[0])
            self._answer(200, "Saved; restarting.")
            restart_soon()

        def _setup_answers(self, body: str) -> str | None:
            """The setup page's answers as the new config.ini text; or None,
            the reply already sent (a test message, or a refusal)."""
            assert config_path is not None   # do_POST checked _config_allowed
            try:
                answers = json.loads(body)
                if not isinstance(answers, dict):
                    raise ValueError("expected an object")
                text = wizard.apply_setup(config_path.read_text(), answers)
            except (ValueError, TypeError, KeyError, OSError) as e:
                self._answer(400, f"Not saved: {e}")
                return None
            if answers.get("test_alert"):
                cfg = configparser.ConfigParser()
                cfg.read_string(text)
                problems = alerts.send(cfg, f"{page_title(cfg)}: test", "A test message from the weather station's setup page.")
                if not cfg.get("alerts", "ntfy_topic", fallback="").strip() and not cfg.get("alerts", "email_to", fallback="").strip():
                    problems.append("no ntfy topic or email address to send to")
                self._answer(200 if not problems else 502, "Sent." if not problems else "Not sent: " + "; ".join(problems))
                return None
            if answers.get("backup_now"):
                # To the place on the page, of the settings as saved now.
                cfg = configparser.ConfigParser()
                cfg.read_string(text)
                result = upkeep.backup(cfg, config_path)
                if result["ok"] is not None:
                    upkeep.write_record(result)
                self._answer(200 if result["ok"] else 502,
                             f"Backed up: {result['file']}." if result["ok"] else f"Not backed up: {result['problem']}")
                return None
            return text

        def _mcp(self) -> None:
            """/mcp (mcp_server.py): your own network only; a page from another
            site, which a browser marks with its Origin, is refused."""
            origin = self.headers.get("Origin")
            if not _local_client(self.client_address[0]) or (
                    origin and not _local_host(urllib.parse.urlparse(origin).netloc)):
                self.send_error(403 if origin else 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= CONFIG_MAX_BYTES:
                self.send_error(400, "Empty or too big")
                return
            assert mcp_server_ is not None   # do_POST routes /mcp here only when it is on
            code, reply = mcp_server_.handle(self.rfile.read(length))
            if reply is None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(reply, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _answer(self, code: int, message: str) -> None:
            body = message.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _query(self, answer: Any, q: Query) -> None:
            if db_path is None or not db_path.exists():
                self.send_error(404, "No database yet")
                return
            try:
                data = {"title": title, "units": shown_in, **answer(db_path, sensors or {}, q)}
            except ValueError as e:
                self.send_error(400, str(e))
                return
            except sqlite3.Error as e:
                log.error("%s query failed: %s", answer.__name__, e)
                self.send_error(500, "Database error")
                return
            self._send(json.dumps(data, separators=(",", ":")).encode(), "application/json")

        def _noaa(self, path: str, q: Query) -> None:
            if path == "/noaa":
                self._send((page_dir / "noaa.html").read_bytes(), PAGE_TYPES[".html"])
                return
            if db_path is None or not db_path.exists():
                self.send_error(404, "No database yet")
                return
            assert cfg is not None   # do_GET routes /noaa here only with a config
            try:
                got = noaa_summary(cfg, db_path, sensors or {}, q)
            except ValueError as e:
                self.send_error(400, str(e))
                return
            except sqlite3.Error as e:
                log.error("NOAA summary failed: %s", e)
                self.send_error(500, "Database error")
                return
            if path == "/noaa.json":
                self._send(json.dumps(got).encode(), "application/json")
                return
            body = got["text"].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'inline; filename="NOAA-{got["period"]}.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return WebHandler


def run_web_server(cfg: configparser.ConfigParser, write_path: Path, config_path: Path | None = None) -> None:
    # One or more ports, comma-separated: e.g. "12345, 80" when a router can only
    # forward 80 to 80 but the inside address should stay easy to remember.
    ports = [int(p) for p in cfg.get("web", "port", fallback="12345").split(",") if p.strip()]
    own = cfg.get("web", "pages", fallback="").strip()
    own_dir = Path(own).expanduser() if own else None
    if own_dir is not None and not own_dir.is_dir():
        log.warning("[web] pages folder %s not found; serving the shipped pages only", own_dir)
    handler = _make_web_handler(write_path, database_path(cfg), sensor_map(cfg), own_dir, page_title(cfg), config_path,
                                cfg.getboolean("web", "mcp", fallback=True), cfg)
    servers = [secure_web.WebServer(("", port), handler) for port in ports]
    cert = secure_web.certificate(cfg)
    for server in servers:
        server.certificate = cert       # https on the same port(s) as http (secure_web.py)
    for port, server in zip(ports[1:], servers[1:]):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Web server on port(s) %s%s", ", ".join(map(str, ports)), ", http and https" if cert else "")
    servers[0].serve_forever()
