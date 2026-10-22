"""
Zambretti: a short outlook from the barometer alone, no internet.

Negretti & Zambra's 1915 pocket forecaster, as worked out for electronic
stations (beteljuice.com's version): sea-level pressure, whether it is rising,
steady or falling over 3 hours, the wind's direction and the season pick one of
26 lines, from "Settled fine" to "Stormy, much rain". It is right roughly
three times in four for the next 12 hours or so, best in temperate latitudes;
it knows nothing about the land around you, so it is an outlook, not a
forecast.

The hub's pressure (baromin) is already sea-level pressure, as Weather
Underground takes it.
"""

from __future__ import annotations

OUTLOOKS = (
    "Settled fine", "Fine weather", "Becoming fine", "Fine, becoming less settled",
    "Fine, possible showers", "Fairly fine, improving", "Fairly fine, possible showers early",
    "Fairly fine, showery later", "Showery early, improving", "Changeable, mending",
    "Fairly fine, showers likely", "Rather unsettled, clearing later", "Unsettled, probably improving",
    "Showery, bright intervals", "Showery, becoming less settled", "Changeable, some rain",
    "Unsettled, short fine intervals", "Unsettled, rain later", "Unsettled, some rain",
    "Mostly very unsettled", "Occasional rain, worsening", "Rain at times, very unsettled",
    "Rain at frequent intervals", "Rain, very unsettled", "Stormy, may improve", "Stormy, much rain",
)
# For each of 22 pressure bands, low to high: which outlook (an index into OUTLOOKS).
RISING = (25, 25, 25, 24, 24, 19, 16, 12, 11, 9, 8, 6, 5, 2, 1, 1, 0, 0, 0, 0, 0, 0)
STEADY = (25, 25, 25, 25, 25, 25, 23, 23, 22, 18, 15, 13, 10, 4, 1, 1, 0, 0, 0, 0, 0, 0)
FALLING = (25, 25, 25, 25, 25, 25, 25, 25, 23, 23, 21, 20, 17, 14, 7, 3, 1, 1, 1, 0, 0, 0)
BOTTOM_HPA, TOP_HPA = 950.0, 1050.0
# A wind from the north (in the northern hemisphere) brings better weather than
# the pressure says, from the south worse: percent of the range, by 16 points.
WIND = (6, 5, 5, 2, -0.5, -2, -5, -8.5, -12, -10, -6, -4.5, -3, -0.5, 1.5, 3)
SEASON = 7          # percent of the range: summer's rises and falls count for more
TREND_HPA = 1.6     # in 3 hours: less is steady
HPA_PER_INHG = 33.8639


def outlook(pressure_inhg: float, change_3h_inhg: float | None, month: int,
            wind_dir_deg: float | None = None, north: bool = True) -> str | None:
    """The outlook for a sea-level pressure; None until there are 3 hours of trend."""
    if change_3h_inhg is None:
        return None
    hpa, change = pressure_inhg * HPA_PER_INHG, change_3h_inhg * HPA_PER_INHG
    trend = "rising" if change >= TREND_HPA else "falling" if change <= -TREND_HPA else "steady"
    span = TOP_HPA - BOTTOM_HPA
    if wind_dir_deg is not None:
        towards = (wind_dir_deg + (0 if north else 180)) % 360
        hpa += WIND[round(towards / 22.5) % 16] / 100 * span
    summer = 4 <= month <= 9 if north else not 4 <= month <= 9
    if summer and trend != "steady":
        hpa += (SEASON if trend == "rising" else -SEASON) / 100 * span
    band = int((hpa - BOTTOM_HPA) // (span / 22))
    exceptional = band < 0 or band > 21
    band = min(max(band, 0), 21)
    text = OUTLOOKS[{"rising": RISING, "steady": STEADY, "falling": FALLING}[trend][band]]
    return f"Exceptional weather: {text[0].lower()}{text[1:]}" if exceptional else text
