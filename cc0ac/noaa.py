"""
NOAA-style climate summaries: a month's days, or a year's months, as the plain
text table the National Weather Service's climatological reports use, and
WeeWX and Cumulus print.

    python3 acurite-capture.py --noaa 2026-10         # a month
    python3 acurite-capture.py --noaa 2026            # a year
    http://<station>:12345/noaa.html                  # the same, on your network

One outdoor sensor's readings (the one with the most readings in the period,
unless another is asked for), in the [units] the pages show.
Each day, from its readings in local time:

- mean: the average of the day's temperatures; high and low with their times;
- heating and cooling degree days, from the middle of the high and low against
  [climate] heating_base_f and cooling_base_f (65 °F, as the Weather Service);
- rain: the day's total; wind: the average speed, the highest gust and its
  time, and the direction it blew from most (the speed-weighted average).

A year is worked out from the finished days in the `daily` table, the same
numbers the charts use, and only today from the readings.

A day without readings is listed with nothing in it; a month or year counts
only the days it has.
"""

from __future__ import annotations

import calendar
import configparser
import math
import sqlite3
from datetime import date, datetime, timezone

from cc0ac import units
from cc0ac.climate import degree_days   # the dashboard's degree days are the same

CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
# The counts under each table, in the stored units: (label, field, test, threshold).
US_LIMITS = {"hot": 90, "cold_max": 32, "cold_min": 32, "very_cold": 0, "rain": (0.01, 0.1, 1.0)}
METRIC_LIMITS = {"hot": 86, "cold_max": 32, "cold_min": 32, "very_cold": -0.4, "rain": (0.25 / 25.4, 2.5 / 25.4, 25 / 25.4)}


def _local(utc: str) -> datetime:
    return datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()


# Wind as the `daily` table sums it, so a month and its year agree.
SPEED = "wind_mph"
GUST = "wind_gust_mph"
_WHERE = "WHERE sensor_id = ? AND received_utc >= ? AND received_utc < ?"


def _bounds(sensor_id: str, first: int, after: int) -> list[str]:
    """sensor_id and the UTC times of local midnight on two day ordinals."""
    return [sensor_id] + [datetime.fromordinal(k).astimezone().astimezone(timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ") for k in (first, after)]


def blew_from(con: sqlite3.Connection, bounds: list[str]) -> tuple[float, float]:
    """The wind's speed-weighted direction over a span, as (x, y): summed by direction."""
    x = y = 0.0
    for heading, speed in con.execute(f"SELECT wind_dir_deg, sum({SPEED}) FROM readings {_WHERE} "
                                      f"AND wind_dir_deg IS NOT NULL GROUP BY wind_dir_deg", bounds):
        if speed:
            x += speed * math.sin(math.radians(heading))
            y += speed * math.cos(math.radians(heading))
    return x, y


def gather(con: sqlite3.Connection, sensor_id: str, first: date, last: date, detail: bool = True) -> dict[str, dict]:
    """Each local day from first to last that has readings: {day: summary}.
    detail adds when the high, low and gust came and the day's wind direction."""
    days: dict[str, dict] = {}
    for n in range(first.toordinal(), last.toordinal() + 1):
        bounds = _bounds(sensor_id, n, n + 1)
        rows, mean, high, low, rain, wind, gust = con.execute(
            f"SELECT count(*), avg(temp_f), max(temp_f), min(temp_f), max(rain_day_in), "
            f"avg({SPEED}), max({GUST}) FROM readings {_WHERE}", bounds).fetchone()
        if not rows:
            continue
        d = {"mean": mean, "high": high, "low": low, "rain": rain, "wind": wind, "gust": gust, "x": 0.0, "y": 0.0}
        if detail:
            # When the high, low and gust came: the first reading of each.
            for key, column in (("high", "temp_f"), ("low", "temp_f"), ("gust", GUST)):
                if d[key] is not None:
                    at = con.execute(f"SELECT min(received_utc) FROM readings {_WHERE} AND {column} = ?",
                                     bounds + [d[key]]).fetchone()[0]
                    d[f"{key}_time"] = _local(at).strftime("%H:%M")
            d["x"], d["y"] = blew_from(con, bounds)
        days[date.fromordinal(n).isoformat()] = d
    return days


def gather_year(con: sqlite3.Connection, sensor_id: str, year: int) -> tuple[dict[str, dict], dict[int, tuple]]:
    """A year's days and each month's wind direction ({month: (x, y)}). Finished
    days come from the `daily` table, the rest from the readings: a year of one
    sensor is about a million readings, too many to go through on request."""
    days: dict[str, dict] = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'daily'").fetchone():
        for day, mean, high, low, rain, wind, gust in con.execute(
                "SELECT day, temp_avg, temp_max, temp_min, rain_in, wind_avg, wind_gust_max FROM daily "
                "WHERE sensor_id = ? AND day >= ? AND day <= ? AND readings > 0",
                [sensor_id, f"{year:04d}-01-01", f"{year:04d}-12-31"]):
            days[day] = {"mean": mean, "high": high, "low": low, "rain": rain, "wind": wind, "gust": gust}
    today = date.today()
    for n in range(date(year, 1, 1).toordinal(), min(date(year, 12, 31), today).toordinal() + 1):
        if date.fromordinal(n).isoformat() not in days:
            days.update(gather(con, sensor_id, date.fromordinal(n), date.fromordinal(n), detail=False))
    winds = {}
    for m in range(1, 13):
        if any(k.startswith(f"{year:04d}-{m:02d}-") for k in days):
            after = date(year + m // 12, m % 12 + 1, 1).toordinal()
            winds[m] = blew_from(con, _bounds(sensor_id, date(year, m, 1).toordinal(), after))
    return days, winds


def direction(x: float, y: float) -> str:
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return "---"
    return CARDINALS[round(math.degrees(math.atan2(x, y)) % 360 / 22.5) % 16]


class Report:
    def __init__(self, cfg: configparser.ConfigParser, name: str):
        self.units = units.chosen(cfg)
        self.name = name
        self.heat_base = cfg.getfloat("climate", "heating_base_f", fallback=65)
        self.cool_base = cfg.getfloat("climate", "cooling_base_f", fallback=65)
        self.limits = US_LIMITS if self.units["temperature"] == "f" else METRIC_LIMITS

    def _v(self, kind: str, v: float | None, width: int, places: int, change: bool = False) -> str:
        if v is None:
            return " " * (width - 3) + "---"
        return f"{units.convert(kind, v, self.units, change):{width}.{places}f}"

    def _label(self, kind: str) -> str:
        return units.KINDS[kind][self.units[kind]][2].strip()

    def _rain_places(self) -> int:
        return 2 if self.units["rain"] == "in" else 1

    def _dd(self, d: dict, heating: bool) -> float | None:
        return degree_days(d.get("high"), d.get("low"), self.heat_base if heating else self.cool_base, heating)

    def _head(self, title: str) -> list[str]:
        return [f"{title:^80}".rstrip(), "", f"NAME: {self.name}", "",
                f"{'TEMPERATURE (' + self._label('temperature') + '), RAIN (' + self._label('rain') + '), WIND SPEED (' + self._label('wind') + ')':^80}".rstrip(),
                ""]

    def month(self, year: int, month: int, days: dict[str, dict]) -> str:
        r = self._rain_places()
        out = self._head(f"MONTHLY CLIMATOLOGICAL SUMMARY for {calendar.month_abbr[month]} {year}")
        out += ["                                      HEAT   COOL           AVG",
                "      MEAN                            DEG    DEG            WIND                  DOM",
                "DAY   TEMP   HIGH   TIME    LOW   TIME   DAYS   DAYS   RAIN  SPEED   HIGH   TIME   DIR",
                "-" * 86]
        have = []
        for n in range(1, calendar.monthrange(year, month)[1] + 1):
            d = days.get(f"{year:04d}-{month:02d}-{n:02d}")
            if d is None:
                out.append(f"{n:02d}")
                continue
            have.append((n, d))
            out.append(f"{n:02d} {self._v('temperature', d['mean'], 6, 1)} {self._v('temperature', d['high'], 6, 1)}"
                       f"  {d.get('high_time', '  ---'):>5} {self._v('temperature', d['low'], 6, 1)}"
                       f"  {d.get('low_time', '  ---'):>5} {self._v('temperature', self._dd(d, True), 6, 1, True)}"
                       f" {self._v('temperature', self._dd(d, False), 6, 1, True)} {self._v('rain', d['rain'], 6, r)}"
                       f" {self._v('wind', d['wind'], 6, 1)} {self._v('wind', d['gust'], 6, 1)}"
                       f"  {d.get('gust_time', '  ---'):>5} {direction(d['x'], d['y']):>5}")
        out.append("-" * 86)
        out += self._month_totals(have, r)
        return "\n".join(line.rstrip() for line in out) + "\n"

    def _month_totals(self, have: list[tuple[int, dict]], r: int) -> list[str]:
        def best(key, pick):
            found = [(d[key], n) for n, d in have if d.get(key) is not None]
            return pick(found, key=lambda p: p[0]) if found else (None, None)

        def total(values):
            values = [v for v in values if v is not None]
            return sum(values) if values else None

        def mean(values):
            values = [v for v in values if v is not None]
            return sum(values) / len(values) if values else None

        hi, hi_day = best("high", max)
        lo, lo_day = best("low", min)
        gust, gust_day = best("gust", max)
        rain = [d["rain"] for _, d in have]
        most, most_day = best("rain", max)
        lim = self.limits
        line = (f"   {self._v('temperature', mean([d['mean'] for _, d in have]), 6, 1)}"
                f" {self._v('temperature', hi, 6, 1)}  {hi_day or '---':>5} {self._v('temperature', lo, 6, 1)}"
                f"  {lo_day or '---':>5} {self._v('temperature', total([self._dd(d, True) for _, d in have]), 6, 1, True)}"
                f" {self._v('temperature', total([self._dd(d, False) for _, d in have]), 6, 1, True)}"
                f" {self._v('rain', total(rain), 6, r)} {self._v('wind', mean([d['wind'] for _, d in have]), 6, 1)}"
                f" {self._v('wind', gust, 6, 1)}  {gust_day or '---':>5}"
                f" {direction(sum(d['x'] for _, d in have), sum(d['y'] for _, d in have)):>5}")
        t = lambda v: self._v("temperature", v, 1, 1).strip()   # noqa: E731
        rr = lambda v: self._v("rain", v, 1, r).strip()         # noqa: E731
        count = lambda key, test: sum(1 for _, d in have if d.get(key) is not None and test(d[key]))   # noqa: E731
        return [line, "",
                f"Days with high >= {t(lim['hot'])}: {count('high', lambda v: v >= lim['hot'])}",
                f"Days with high <= {t(lim['cold_max'])}: {count('high', lambda v: v <= lim['cold_max'])}",
                f"Days with low <= {t(lim['cold_min'])}: {count('low', lambda v: v <= lim['cold_min'])}",
                f"Days with low <= {t(lim['very_cold'])}: {count('low', lambda v: v <= lim['very_cold'])}",
                f"Most rain in a day: {rr(most) if most is not None else '---'}"
                + (f" on day {most_day}" if most_day else ""),
                "Days of rain: " + ", ".join(f"{count('rain', lambda v, k=k: v >= k - 1e-9)} with {rr(k)} or more"
                                             for k in lim["rain"]),
                f"Degree days from the middle of each day's high and low; heating base "
                f"{t(self.heat_base)}, cooling base {t(self.cool_base)}.",
                f"Days with readings: {len(have)}"]

    def year(self, year: int, days: dict[str, dict], winds: dict[int, tuple]) -> str:
        r = self._rain_places()
        lim = self.limits
        out = self._head(f"ANNUAL CLIMATOLOGICAL SUMMARY for {year}")
        t = lambda v: self._v("temperature", v, 1, 0).strip()   # noqa: E731
        rr = lambda v: self._v("rain", v, 1, r).strip()         # noqa: E731
        temp = ["                              HEAT   COOL                                 "
                f"{'MAX':>5} {'MAX':>5} {'MIN':>5} {'MIN':>5}",
                "        MEAN   MEAN           DEG    DEG                                  "
                f"{'>=':>5} {'<=':>5} {'<=':>5} {'<=':>5}",
                "YR  MO   MAX    MIN   MEAN   DAYS   DAYS     HI   DAY    LOW   DAY "
                f"{t(lim['hot']):>5} {t(lim['cold_max']):>5} {t(lim['cold_min']):>5} {t(lim['very_cold']):>5}",
                "-" * 89]
        rain = ["                         MAX         DAYS OF RAIN",
                "                         OBS.        OVER",
                f"YR  MO   TOTAL    DAY   DAY  {rr(lim['rain'][0]):>6} {rr(lim['rain'][1]):>6} {rr(lim['rain'][2]):>6}",
                "-" * 52]
        wind = ["                                     DOM",
                "YR  MO     AVG     HI   DAY   DIR",
                "-" * 34]
        whole: list[tuple[str, dict]] = []
        for m in range(1, 13):
            have = [(k, d) for k, d in sorted(days.items()) if k.startswith(f"{year:04d}-{m:02d}-")]
            whole += have
            row = f"{year % 100:02d}  {m:02d}"
            if not have:
                temp.append(row)
                rain.append(row)
                wind.append(row)
                continue
            temp.append(row + self._year_temp(have, lim))
            rain.append(row + self._year_rain(have, lim, r))
            wind.append(row + self._year_wind(have, [winds.get(m, (0.0, 0.0))]))
        # Under each table, its rule again and the whole year.
        temp.append(temp[3])
        rain.append(rain[3])
        wind.append(wind[2])
        if whole:
            temp.append("      " + self._year_temp(whole, lim, True))
            rain.append("      " + self._year_rain(whole, lim, r, True))
            wind.append("      " + self._year_wind(whole, list(winds.values()), True))
        out += ["TEMPERATURE", ""] + temp + ["", "RAIN", ""] + rain + ["", "WIND", ""] + wind + [
            "", f"Degree days from the middle of each day's high and low; heating base "
                f"{self._v('temperature', self.heat_base, 1, 1).strip()}, cooling base "
                f"{self._v('temperature', self.cool_base, 1, 1).strip()}. A year's HI and LOW name the month-day.",
            f"Days with readings: {len(whole)}"]
        return "\n".join(line.rstrip() for line in out) + "\n"

    @staticmethod
    def _when(key: str, whole: bool) -> str:
        return key[5:] if whole else key[8:]

    def _year_temp(self, have: list[tuple[str, dict]], lim: dict, whole: bool = False) -> str:
        mean = lambda vs: (sum(vs) / len(vs)) if vs else None   # noqa: E731
        highs = [(d["high"], k) for k, d in have if d.get("high") is not None]
        lows = [(d["low"], k) for k, d in have if d.get("low") is not None]
        hi, hi_k = max(highs) if highs else (None, None)
        lo, lo_k = min(lows) if lows else (None, None)
        dd = lambda heating: sum(v for v in (self._dd(d, heating) for _, d in have) if v is not None) if highs else None   # noqa: E731
        count = lambda key, test: sum(1 for _, d in have if d.get(key) is not None and test(d[key]))   # noqa: E731
        return (f" {self._v('temperature', mean([h for h, _ in highs]), 6, 1)}"
                f" {self._v('temperature', mean([l for l, _ in lows]), 6, 1)}"
                f" {self._v('temperature', mean([d['mean'] for _, d in have if d['mean'] is not None]), 6, 1)}"
                f" {self._v('temperature', dd(True), 6, 1, True)} {self._v('temperature', dd(False), 6, 1, True)}"
                f" {self._v('temperature', hi, 6, 1)} {self._when(hi_k, whole) if hi_k else '---':>5}"
                f" {self._v('temperature', lo, 6, 1)} {self._when(lo_k, whole) if lo_k else '---':>5}"
                f" {count('high', lambda v: v >= lim['hot']):5d} {count('high', lambda v: v <= lim['cold_max']):5d}"
                f" {count('low', lambda v: v <= lim['cold_min']):5d} {count('low', lambda v: v <= lim['very_cold']):5d}")

    def _year_rain(self, have: list[tuple[str, dict]], lim: dict, r: int, whole: bool = False) -> str:
        rains = [(d["rain"], k) for k, d in have if d.get("rain") is not None]
        most, most_k = max(rains) if rains else (None, None)
        count = lambda k: sum(1 for v, _ in rains if v >= k - 1e-9)   # noqa: E731
        return (f" {self._v('rain', sum(v for v, _ in rains) if rains else None, 7, r)}"
                f" {self._v('rain', most, 6, r)} {self._when(most_k, whole) if most_k else '---':>5}"
                f"  {count(lim['rain'][0]):6d} {count(lim['rain'][1]):6d} {count(lim['rain'][2]):6d}")

    def _year_wind(self, have: list[tuple[str, dict]], winds: list[tuple], whole: bool = False) -> str:
        speeds = [d["wind"] for _, d in have if d.get("wind") is not None]
        gusts = [(d["gust"], k) for k, d in have if d.get("gust") is not None]
        gust, gust_k = max(gusts) if gusts else (None, None)
        return (f" {self._v('wind', sum(speeds) / len(speeds) if speeds else None, 7, 1)}"
                f" {self._v('wind', gust, 6, 1)} {self._when(gust_k, whole) if gust_k else '---':>5}"
                f" {direction(sum(x for x, _ in winds), sum(y for _, y in winds)):>5}")


def period(text: str) -> tuple[int, int | None]:
    """'2026-10' → (2026, 10); '2026' → (2026, None)."""
    try:
        if len(text) == 4:
            return int(text), None
        when = datetime.strptime(text, "%Y-%m")
        return when.year, when.month
    except ValueError:
        raise ValueError(f"{text!r} isn't a month (YYYY-MM) or a year (YYYY)")


def report(cfg: configparser.ConfigParser, con: sqlite3.Connection, sensor_id: str, name: str,
           year: int, month: int | None) -> str:
    first = date(year, month or 1, 1)
    last = date(year, month or 12, calendar.monthrange(year, month or 12)[1])
    r = Report(cfg, name)
    if month:
        return r.month(year, month, gather(con, sensor_id, first, last))
    return r.year(year, *gather_year(con, sensor_id, year))
