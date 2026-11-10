package org.cc0ac.weatherkiosk;

import org.json.JSONObject;

/** The station's chosen units (current.json "units"), as the dashboard's units.js shows them.
 *  The station keeps every reading in °F and mph; only what is shown changes. */
final class Units {
    final boolean celsius;
    final String wind;

    Units(JSONObject current) {
        JSONObject u = current == null ? null : current.optJSONObject("units");
        celsius = u != null && "c".equals(u.optString("temperature"));
        wind = u == null ? "mph" : u.optString("wind", "mph");
    }

    /** "47°" */
    String temp(double f) {
        double v = celsius ? (f - 32) * 5 / 9 : f;
        return Math.round(v) + "°";
    }

    /** "8 mph", "13 km/h", "3.6 m/s", "7 kn" */
    String speed(double mph) {
        switch (wind) {
            case "kmh": return Math.round(mph * 1.609344) + " km/h";
            case "ms": return String.format(java.util.Locale.ROOT, "%.1f m/s", mph * 0.44704);
            case "knots": return Math.round(mph * 0.868976) + " kn";
            default: return Math.round(mph) + " mph";
        }
    }

    static final String[] CARDINALS = {"N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"};

    static String cardinal(double deg) {
        return CARDINALS[(int) Math.round(deg / 22.5) % 16];
    }

    /** The Weather Service's short forecast → one emoji, the same rules as weather.html's skyEmoji. */
    static String sky(String text, boolean day) {
        String t = text == null ? "" : text.split(" then ")[0].toLowerCase(java.util.Locale.ROOT);
        if (t.contains("thunder")) return "⛈️";
        if (t.matches(".*(snow|flurr|blizzard|sleet|freezing|ice).*")) return "🌨️";
        if (t.matches(".*(rain|shower|drizzle).*")) return t.contains("chance") && day ? "🌦️" : "🌧️";
        if (t.matches(".*(fog|haze|smoke|dust).*")) return "🌫️";
        if (t.startsWith("cloudy") || t.contains("overcast") || t.contains("mostly cloudy"))
            return day && t.contains("mostly") ? "🌥️" : "☁️";
        if (t.contains("partly")) return day ? "⛅" : "☁️";
        if (t.matches(".*mostly (sunny|clear).*")) return day ? "🌤️" : "🌙";
        if (t.matches(".*(sunny|clear|fair).*")) return day ? "☀️" : "🌙";
        if (t.matches(".*(wind|breezy|blustery).*")) return "💨";
        return day ? "🌤️" : "☁️";
    }
}
