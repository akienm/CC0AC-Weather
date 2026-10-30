"""
The station answers MCP (the Model Context Protocol) at /mcp, so an AI
assistant can read it: Claude, ChatGPT's connectors, or any program that
speaks MCP. Ask it "how cold did it get last night?" or "is this October
wetter than usual?" and it looks.

    claude mcp add --transport http weather http://<pi>:12345/mcp

It is the "Streamable HTTP" transport in its simplest form: each request is one
POST of a JSON-RPC message, answered with one JSON reply. No sessions, no
server-sent events, nothing kept between requests.

Read-only: every tool reads what the dashboard already shows. Like the data
files, it answers only addresses on your own network, and a browser page from
another site is refused (its Origin header), so a web page you visit can't use
your browser to read it. [web] mcp = false turns it off.
"""

from __future__ import annotations

import json
from typing import Any, Callable

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")   # newest first
OUTDOOR_FIELDS = ("temp_f", "humidity_pct", "dew_point_f", "feels_like_f", "wind_mph", "wind_gust_mph",
                  "wind_dir_deg", "pressure_inhg", "uv_index", "light_lux")
HOURS_OF_FORECAST = 24

INSTRUCTIONS = (
    "A home weather station. Temperatures are °F, wind mph, pressure inHg (sea level), rain inches; "
    "times are ISO 8601 UTC unless a field says local. Readings come every few minutes. Sensor names "
    "are the owner's (e.g. 'Back yard', 'Garage'); outdoor sensors have 'outdoor': true. Sensors whose "
    "id starts with 'open-meteo' or 'nws-' are internet weather for the location, not the station.")


def _schema(props: dict | None = None, required: list | None = None) -> dict:
    out = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


class Station:
    """What the tools read. read(name) → a data file's parsed JSON or None;
    history(q) and summaries(q) as the web server's /history.json and /summaries.json."""

    def __init__(self, read: Callable[[str], dict | None], history: Callable[[dict], dict],
                 summaries: Callable[[dict], dict]):
        self._read, self._history, self._summaries = read, history, summaries

    def _need(self, name: str) -> dict:
        doc = self._read(name)
        if doc is None:
            raise ToolError(f"{name} hasn't been written yet (no readings, or that part is switched off)")
        return doc

    # ── Tools ────────────────────────────────────────────────────────────────

    def current_conditions(self) -> dict:
        cur = self._need("current.json")
        return {"written": cur.get("written"),
                "pressure_change_3h_inhg": cur.get("pressure_change_3h"),
                "outlook": cur.get("zambretti"),
                "sensors": [{"id": s.get("sensor_id"), "name": s.get("name"), "type": s.get("type"),
                             "outdoor": s.get("type") in ("Atlas", "Iris", "5N1", "outdoor"),
                             "updated": s.get("updated"), "readings": s.get("fields")}
                            for s in cur.get("sensors", [])],
                "warnings": cur.get("warnings") or []}

    def list_sensors(self) -> dict:
        return {"sensors": [{k: s[k] for k in ("id", "name", "type", "outdoor", "updated")}
                            for s in self.current_conditions()["sensors"]]}

    def forecast(self) -> dict:
        fc = self._need("forecast.json")
        out = {k: fc.get(k) for k in ("source", "alerts", "observation", "errors") if fc.get(k)}
        if fc.get("daily"):
            out["daily"] = fc["daily"]
        if fc.get("hourly"):
            out["hourly"] = {**fc["hourly"], "periods": fc["hourly"].get("periods", [])[:HOURS_OF_FORECAST]}
        return out

    def outlook(self) -> dict:
        cur = self._need("current.json")
        return {"outlook": cur.get("zambretti"),
                "pressure_change_3h_inhg": cur.get("pressure_change_3h"),
                "how": "Zambretti forecaster: sea-level pressure, its 3-hour trend, wind direction and "
                       "season. Roughly right 3 times in 4 for the next 12 hours; null until there are "
                       "3 hours of pressure readings."}

    def records_and_normals(self) -> dict:
        return self._need("climate.json")

    def history(self, period: str | None = None, start: str | None = None, end: str | None = None,
                fields: list | None = None) -> dict:
        if start:
            q = {"from": start, "to": end or start}
        else:
            q = {"period": period or "today"}
        try:
            h = self._history(q)
        except ValueError as e:
            raise ToolError(str(e))
        want = [f for f in (fields or ["temp_f", "humidity_pct", "wind_mph", "wind_gust_mph", "pressure_inhg"])
                if f in OUTDOOR_FIELDS]
        cols = h.get("outdoor", {})
        rows = []
        for i, t in enumerate(h.get("t", [])):
            vals = {f: cols[f][i] for f in want if f in cols and cols[f][i] is not None}
            if vals:
                rows.append({"time": t, **vals})
        return {"period": h.get("period"), "from": h.get("from"), "to": h.get("to"),
                "step_seconds": h.get("step"),
                "note": "outdoor averages per step; 'time' is epoch seconds at the step's start",
                "outdoor": rows,
                "rooms": [{"id": r["id"], "name": r["name"],
                           "temp_f": _thin(h.get("t", []), r.get("temp_f"))} for r in h.get("rooms", [])],
                "rain_totals": h.get("rain_totals")}

    def daily_summary(self, days: int = 31, by: str = "days", sensor: str | None = None) -> dict:
        try:
            s = self._summaries({"days": str(max(int(days), 1))})
        except ValueError as e:
            raise ToolError(str(e))
        if by not in ("days", "weeks", "months", "years"):
            raise ToolError("by must be days, weeks, months or years")
        out = []
        for row in s.get("sensors", []):
            if sensor and sensor.lower() not in (row["id"].lower(), str(row["name"]).lower()):
                continue
            out.append({"id": row["id"], "name": row["name"], "outdoor": row["outdoor"], by: row[by]})
        if sensor and not out:
            raise ToolError(f"no sensor named {sensor!r}; list_sensors has their names")
        return {"today": s.get("today"), "sensors": out}


def _thin(t: list, values: list | None) -> list:
    return [[ts, v] for ts, v in zip(t, values or []) if v is not None]


class ToolError(Exception):
    """Told back to the assistant as the tool's answer, marked as an error."""


TOOLS = {
    "current_conditions": (
        "Right now: every sensor's latest readings (temperature, humidity, wind, rain, pressure, battery), "
        "the 3-hour pressure change and the barometer outlook.", _schema()),
    "list_sensors": ("The station's sensors: id, name, type, outdoor or not, when last heard.", _schema()),
    "forecast": ("The forecast for the station's location (National Weather Service in the US, else "
                 "Open-Meteo): daily periods, the next 24 hours, any active weather alerts, and the nearest "
                 "airport's observation where there is one.", _schema()),
    "outlook": ("A short outlook worked out from the station's own barometer (Zambretti), no internet.",
                _schema()),
    "records_and_normals": (
        "Today's high and low against the 1991-2020 normals for this place, the month's rain against "
        "normal, and the station's records (hottest, coldest, highest gust, wettest day, pressure) for "
        "today, this month, this year and all time, each with its date.", _schema()),
    "history": (
        "Outdoor readings over time, averaged into steps (5 minutes for a day, longer for longer spans), "
        "plus indoor temperatures and rain totals. Give period, or start (and end) dates.",
        _schema({"period": {"type": "string", "enum": ["today", "week", "month", "year", "all"]},
                 "start": {"type": "string", "description": "local date YYYY-MM-DD"},
                 "end": {"type": "string", "description": "local date YYYY-MM-DD, inclusive"},
                 "fields": {"type": "array", "items": {"type": "string", "enum": list(OUTDOOR_FIELDS)},
                            "description": "default: temp_f, humidity_pct, wind_mph, wind_gust_mph, pressure_inhg"}})),
    "daily_summary": (
        "Per sensor, per local day (or week, month, year): lows, highs, averages, the day's rain, max "
        "gust, pressure range. Good for 'how cold was last Tuesday' or 'how much rain this month'.",
        _schema({"days": {"type": "integer", "minimum": 1, "description": "how many latest days (default 31)"},
                 "by": {"type": "string", "enum": ["days", "weeks", "months", "years"]},
                 "sensor": {"type": "string", "description": "a sensor's id or name; default all"}})),
}


class Server:
    def __init__(self, station: Station, version: str, name: str = "cc0ac-weather"):
        self.station, self.version, self.name = station, version, name

    def handle(self, body: bytes) -> tuple[int, dict | None]:
        """One POSTed message → (HTTP status, JSON reply or None for no body)."""
        try:
            msg = json.loads(body)
        except ValueError:
            return 400, _error(None, -32700, "Parse error")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return 400, _error(None, -32600, "Invalid request: one JSON-RPC 2.0 message per POST")
        if "method" not in msg:
            return 202, None                     # a response or a stray reply: nothing to say
        if "id" not in msg:
            return 202, None                     # a notification, e.g. notifications/initialized
        mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                return 200, _result(mid, {
                    "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": INSTRUCTIONS})
            if method == "ping":
                return 200, _result(mid, {})
            if method == "tools/list":
                return 200, _result(mid, {"tools": [
                    {"name": n, "description": d, "inputSchema": s,
                     "annotations": {"readOnlyHint": True, "openWorldHint": False}}
                    for n, (d, s) in TOOLS.items()]})
            if method == "tools/call":
                return 200, _result(mid, self.call(params.get("name"), params.get("arguments") or {}))
            return 200, _error(mid, -32601, f"Method not found: {method}")
        except Exception as e:   # noqa: BLE001 — one bad call must not take the server down
            return 200, _error(mid, -32603, f"Internal error: {e}")

    def call(self, name: Any, args: dict) -> dict:
        if name not in TOOLS:
            return _tool_error(f"Unknown tool: {name}")
        if not isinstance(args, dict):
            return _tool_error("arguments must be an object")
        allowed = TOOLS[name][1]["properties"]
        extra = set(args) - set(allowed)
        if extra:
            return _tool_error(f"{name} takes no {', '.join(sorted(extra))}")
        try:
            out = getattr(self.station, name)(**args)
        except ToolError as e:
            return _tool_error(str(e))
        except (TypeError, ValueError) as e:
            return _tool_error(f"bad arguments: {e}")
        return {"content": [{"type": "text", "text": json.dumps(out, separators=(",", ":"))}],
                "structuredContent": out, "isError": False}


def _result(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}
