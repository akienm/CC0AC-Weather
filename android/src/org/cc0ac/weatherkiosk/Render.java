package org.cc0ac.weatherkiosk;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.text.format.DateFormat;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.time.format.TextStyle;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** Draws one widget at one size, from its last good copy of the station's files.
 *  The room decides what shows: under 200dp wide, the temperature and feels-like;
 *  wider, today's high and low and the wind too; 110dp tall, the next hours; taller,
 *  the coming days, as many as fit. */
final class Render {
    static final long STALE_MS = 30 * 60 * 1000L;   // a reading older than this is greyed
    static final int[] HOURS = {R.id.h0, R.id.h1, R.id.h2, R.id.h3, R.id.h4, R.id.h5};
    static final int[][] DAYS = {
            {R.id.d0, R.id.d0_name, R.id.d0_sky, R.id.d0_pop, R.id.d0_temp},
            {R.id.d1, R.id.d1_name, R.id.d1_sky, R.id.d1_pop, R.id.d1_temp},
            {R.id.d2, R.id.d2_name, R.id.d2_sky, R.id.d2_pop, R.id.d2_temp},
            {R.id.d3, R.id.d3_name, R.id.d3_sky, R.id.d3_pop, R.id.d3_temp},
            {R.id.d4, R.id.d4_name, R.id.d4_sky, R.id.d4_pop, R.id.d4_temp},
            {R.id.d5, R.id.d5_name, R.id.d5_sky, R.id.d5_pop, R.id.d5_temp},
            {R.id.d6, R.id.d6_name, R.id.d6_sky, R.id.d6_pop, R.id.d6_temp}};
    static final int WHITE = Color.WHITE, DIM = 0xFFB0B8C0, GREY = 0xFF8A9098, AMBER = 0xFFFFC060;

    private Render() {}

    /** What fits in w × h dp. */
    static int hourCells(int w, int h) { return h < 110 ? 0 : w >= 280 ? 6 : w >= 200 ? 5 : 3; }
    static int dayRows(int w, int h) { return h < 160 ? 0 : Math.min(DAYS.length, (h - 135) / 20); }
    static boolean wide(int w) { return w >= 200; }

    static RemoteViews draw(Context c, int id, int w, int h) {
        RemoteViews v = new RemoteViews(c.getPackageName(), R.layout.widget);
        SharedPreferences p = Station.prefs(c);
        String base = Station.url(c, id);
        v.setOnClickPendingIntent(R.id.root, open(c, id, base));
        v.setOnClickPendingIntent(R.id.asof, refresh(c, id));
        if (base.isEmpty()) {
            v.setTextViewText(R.id.asof, "Tap to choose your station");
            v.setOnClickPendingIntent(R.id.asof, configure(c, id));   // the words say tap: all of it chooses
            return v;
        }
        JSONObject current = parse(p.getString(Station.key(id, "current"), null));
        JSONObject forecast = parse(p.getString(Station.key(id, "forecast"), null));
        String error = p.getString(Station.key(id, "error"), "");
        Units u = new Units(current);
        JSONObject sensor = find(current, Station.sensor(c, id));

        if (sensor == null) {
            v.setTextViewText(R.id.asof, current == null
                    ? (error.isEmpty() ? "Loading…" : "⚠ " + error)
                    : "⚠ That sensor isn't on the station now; tap to choose");
            if (current != null) {
                v.setOnClickPendingIntent(R.id.root, configure(c, id));
                v.setOnClickPendingIntent(R.id.asof, configure(c, id));
            }
            return v;
        }
        JSONObject f = sensor.optJSONObject("fields");
        Instant updated = instant(sensor.optString("updated", current.optString("written", "")));
        boolean stale = updated == null || System.currentTimeMillis() - updated.toEpochMilli() > STALE_MS;

        v.setTextViewText(R.id.temp, f.has("temp_f") ? u.temp(f.optDouble("temp_f")) : "--");
        v.setTextColor(R.id.temp, stale ? GREY : WHITE);
        v.setTextViewText(R.id.feels, f.has("feels_like_f") ? "Feels " + u.temp(f.optDouble("feels_like_f"))
                : f.has("humidity_pct") ? "💧 " + Math.round(f.optDouble("humidity_pct")) + "%" : "");
        v.setTextViewText(R.id.name, Station.label(sensor));

        List<Day> days = days(forecast);
        if (wide(w)) {
            v.setViewVisibility(R.id.extra, View.VISIBLE);
            Day today = days.isEmpty() ? null : days.get(0);
            v.setTextViewText(R.id.hilo, today == null ? "" : hilo(u, today));
            v.setTextViewText(R.id.wind, wind(u, f.has("wind_mph") ? f : windFrom(current)));
        }

        JSONArray hourly = periods(forecast, "hourly");
        int cells = hourCells(w, h);
        int shown = 0;
        long now = System.currentTimeMillis();
        boolean h24 = DateFormat.is24HourFormat(c);
        DateTimeFormatter hourFmt = DateTimeFormatter.ofPattern(h24 ? "HH:mm" : "h a", Locale.getDefault());
        for (int i = 0; hourly != null && i < hourly.length() && shown < cells; i++) {
            JSONObject hp = hourly.optJSONObject(i);
            OffsetDateTime start = time(hp.optString("start"));
            if (start == null || start.toInstant().toEpochMilli() + 3600_000L <= now) continue;
            // three short lines, so five or six fit side by side: the hour, the sky (and the
            // chance of rain when there is one), the temperature
            String pop = hp.isNull("pop") || hp.optInt("pop") < 10 ? "" : " " + hp.optInt("pop") + "%";
            v.setTextViewText(HOURS[shown], start.atZoneSameInstant(ZoneId.systemDefault()).format(hourFmt)
                    + "\n" + Units.sky(hp.optString("sky"), hp.optBoolean("day", true)) + pop
                    + "\n" + (hp.isNull("temp_f") ? "--" : u.temp(hp.optDouble("temp_f"))));
            v.setViewVisibility(HOURS[shown++], View.VISIBLE);
        }
        if (shown > 0) v.setViewVisibility(R.id.hours, View.VISIBLE);

        int rows = Math.min(dayRows(w, h), days.size());
        for (int i = 0; i < rows; i++) {
            Day d = days.get(i);
            int[] ids = DAYS[i];
            v.setTextViewText(ids[1], d.name);
            v.setTextViewText(ids[2], Units.sky(d.sky, d.day));
            v.setTextViewText(ids[3], d.pop >= 10 ? d.pop + "%" : "");   // under 10%, as the hours, no chance worth naming
            v.setTextViewText(ids[4], (d.hi == null ? "" : u.temp(d.hi)) + "  " + (d.lo == null ? "" : u.temp(d.lo)));
            v.setViewVisibility(ids[0], View.VISIBLE);
        }
        if (rows > 0) v.setViewVisibility(R.id.days, View.VISIBLE);

        String when = updated == null ? "?" : DateFormat.getTimeFormat(c).format(new java.util.Date(updated.toEpochMilli()));
        v.setTextViewText(R.id.asof, (error.isEmpty() ? "" : "⚠ Can't reach it · ") + "as of " + when);
        v.setTextColor(R.id.asof, error.isEmpty() && !stale ? DIM : AMBER);
        return v;
    }

    /** One day's row: the daytime period and the night after it, or tonight alone. */
    static final class Day {
        String name, sky;
        boolean day;
        Double hi, lo;
        int pop;
    }

    static List<Day> days(JSONObject forecast) {
        List<Day> out = new ArrayList<>();
        JSONArray p = periods(forecast, "daily");
        for (int i = 0; p != null && i < p.length(); i++) {
            JSONObject a = p.optJSONObject(i);
            Day d = new Day();
            d.day = a.optBoolean("day", true);
            d.sky = a.optString("sky");
            d.pop = a.isNull("pop") ? 0 : a.optInt("pop");
            OffsetDateTime start = time(a.optString("start"));
            d.name = out.isEmpty() ? (d.day ? "Today" : "Tonight")
                    : start == null ? a.optString("name") : start.getDayOfWeek().getDisplayName(TextStyle.SHORT, Locale.getDefault());
            Double t = a.isNull("temp_f") ? null : a.optDouble("temp_f");
            if (d.day) {
                d.hi = t;
                JSONObject night = i + 1 < p.length() ? p.optJSONObject(i + 1) : null;
                if (night != null && !night.optBoolean("day", true)) {
                    d.lo = night.isNull("temp_f") ? null : night.optDouble("temp_f");
                    if (!night.isNull("pop")) d.pop = Math.max(d.pop, night.optInt("pop"));
                    i++;
                }
            } else {
                d.lo = t;
            }
            out.add(d);
        }
        return out;
    }

    static String hilo(Units u, Day d) {
        if (d.hi != null && d.lo != null) return "↑" + u.temp(d.hi) + " ↓" + u.temp(d.lo);
        if (d.hi != null) return "High " + u.temp(d.hi);
        return d.lo == null ? "" : "Low " + u.temp(d.lo);
    }

    static String wind(Units u, JSONObject f) {
        if (f == null || !f.has("wind_mph")) return "";
        double mph = f.optDouble("wind_mph");
        if (mph < 0.5) return "💨 Calm";
        String dir = f.has("wind_dir_deg") ? Units.cardinal(f.optDouble("wind_dir_deg")) + " " : "";
        String gust = f.has("wind_gust_mph") && f.optDouble("wind_gust_mph") >= mph + 5
                ? " (" + u.speed(f.optDouble("wind_gust_mph")).split(" ")[0] + ")" : "";
        return "💨 " + dir + u.speed(mph) + gust;
    }

    /** A sensor without a wind cup (an indoor one) shows the station's wind. */
    static JSONObject windFrom(JSONObject current) {
        JSONArray list = current.optJSONArray("sensors");
        for (int i = 0; list != null && i < list.length(); i++) {
            JSONObject f = list.optJSONObject(i).optJSONObject("fields");
            if (f != null && f.has("wind_mph")) return f;
        }
        return null;
    }

    static JSONObject find(JSONObject current, String id) {
        JSONArray list = current == null ? null : current.optJSONArray("sensors");
        for (int i = 0; list != null && i < list.length(); i++) {
            JSONObject s = list.optJSONObject(i);
            if (s != null && id.equals(s.optString("sensor_id")) && s.optJSONObject("fields") != null) return s;
        }
        return null;
    }

    static JSONArray periods(JSONObject forecast, String part) {
        JSONObject o = forecast == null ? null : forecast.optJSONObject(part);
        return o == null ? null : o.optJSONArray("periods");
    }

    static JSONObject parse(String s) {
        try { return s == null ? null : new JSONObject(s); } catch (Exception e) { return null; }
    }

    static OffsetDateTime time(String s) {
        try { return OffsetDateTime.parse(s); } catch (Exception e) { return null; }
    }

    /** "2026-11-10T17:39:27Z", or with an offset. */
    static Instant instant(String s) {
        try { return OffsetDateTime.parse(s).toInstant(); } catch (Exception e) {
            try { return Instant.parse(s); } catch (Exception e2) { return null; }
        }
    }

    static PendingIntent open(Context c, int id, String base) {
        Intent i = base.isEmpty() ? new Intent(c, ConfigActivity.class)
                .putExtra(android.appwidget.AppWidgetManager.EXTRA_APPWIDGET_ID, id)
                : new Intent(Intent.ACTION_VIEW, Uri.parse(base));
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(c, id * 4, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    static PendingIntent configure(Context c, int id) {
        Intent i = new Intent(c, ConfigActivity.class)
                .putExtra(android.appwidget.AppWidgetManager.EXTRA_APPWIDGET_ID, id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(c, id * 4 + 1, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    static PendingIntent refresh(Context c, int id) {
        Intent i = new Intent(c, WeatherWidget.class).setAction(WeatherWidget.REFRESH)
                .putExtra(android.appwidget.AppWidgetManager.EXTRA_APPWIDGET_ID, id);
        return PendingIntent.getBroadcast(c, id * 4 + 2, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }
}
