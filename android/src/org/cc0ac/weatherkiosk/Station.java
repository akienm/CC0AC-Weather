package org.cc0ac.weatherkiosk;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/** One widget's settings and its last good copy of the station's files, kept per widget id.
 *  The station serves current.js and forecast.js to the outside as well as at home, so those
 *  are what is fetched; each is a line of JavaScript around one JSON object. */
final class Station {
    static final String PREFS = "widgets";
    static final int[] INTERVALS = {15, 30, 60};   // minutes; Android runs a periodic job no oftener than 15

    private Station() {}

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static String key(int id, String name) { return "w" + id + "." + name; }

    static String url(Context c, int id) { return prefs(c).getString(key(id, "url"), ""); }
    static String sensor(Context c, int id) { return prefs(c).getString(key(id, "sensor"), ""); }
    static int interval(Context c, int id) { return prefs(c).getInt(key(id, "interval"), 15); }
    static boolean configured(Context c, int id) { return !url(c, id).isEmpty(); }

    static void save(Context c, int id, String url, String sensor, int minutes) {
        prefs(c).edit().putString(key(id, "url"), url).putString(key(id, "sensor"), sensor)
                .putInt(key(id, "interval"), minutes).apply();
    }

    static void forget(Context c, int id) {
        SharedPreferences.Editor e = prefs(c).edit();
        for (String k : prefs(c).getAll().keySet()) if (k.startsWith("w" + id + ".")) e.remove(k);
        e.apply();
    }

    /** "192.168.1.20" → "http://192.168.1.20/": a bare address is plain http at home. */
    static String normalize(String url) {
        String u = url.trim();
        if (u.isEmpty()) return u;
        if (!u.matches("(?i)^https?://.*")) u = "http://" + u;
        int q = u.indexOf('?');
        if (q >= 0) u = u.substring(0, q);
        if (u.endsWith("/weather.html") || u.endsWith("/charts.html")) u = u.substring(0, u.lastIndexOf('/') + 1);
        if (!u.endsWith("/")) u += "/";
        return u;
    }

    /** Why a fetch failed, in words the person setting it up can act on. An https address
     *  fails in ways http never does, and Java's own messages for them name only classes. */
    static String explain(Exception e, String base) {
        // most specific first: an untrusted certificate arrives as an SSLHandshakeException
        // wrapping a CertPathValidatorException (measured, Android 16)
        if (in(e, java.security.cert.CertPathValidatorException.class) || in(e, java.security.cert.CertificateException.class)) {
            return "This phone doesn't trust the station's certificate (one the station made for itself, "
                    + "or one that has expired). Use http://, or give the station a certificate from a public authority.";
        }
        if (in(e, javax.net.ssl.SSLPeerUnverifiedException.class)) {
            return "The station's certificate is for another name. Use the name it was made for, or http://.";
        }
        if (in(e, javax.net.ssl.SSLException.class)) {
            return "Nothing speaks https at " + base + ". If the station is plain http there, use http://.";
        }
        if (in(e, java.net.UnknownHostException.class)) return "No such name: " + base + ".";
        if (in(e, java.net.SocketTimeoutException.class)) return "Nothing answered at " + base + " in time.";
        if (in(e, java.net.ConnectException.class)) return "Nothing is listening at " + base + ".";
        return e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
    }

    private static boolean in(Throwable t, Class<? extends Throwable> kind) {
        for (; t != null; t = t.getCause()) if (kind.isInstance(t)) return true;
        return false;
    }

    /** The JSON object inside "window.ACURITE_CURRENT = {...};". */
    static JSONObject fetchJs(String base, String file) throws Exception {
        HttpURLConnection http = (HttpURLConnection) new URL(base + file).openConnection();
        http.setConnectTimeout(10000);
        http.setReadTimeout(15000);
        http.setInstanceFollowRedirects(false);   // an unset-up station redirects to its setup page
        try {
            int code = http.getResponseCode();
            if (code != 200) throw new Exception(file + ": the station answered " + code);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            try (InputStream in = http.getInputStream()) {
                byte[] buf = new byte[16384];
                for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            }
            String text = out.toString("UTF-8");
            int a = text.indexOf('{'), b = text.lastIndexOf('}');
            if (a < 0 || b < a) throw new Exception(file + " isn't the station's");
            return new JSONObject(text.substring(a, b + 1));
        } finally {
            http.disconnect();
        }
    }

    /** The dashboard's outdoor types (weather.html OUTDOOR, sources/__init__.py OUTDOOR_TYPES). */
    static final java.util.Set<String> OUTDOOR =
            new java.util.HashSet<>(java.util.Arrays.asList("Atlas", "Iris", "5N1", "outdoor"));

    /** Sensors that read a temperature, as {sensor_id, label}: the outdoor ones first, as on
     *  the dashboard, so a new widget shows outside unless told otherwise. */
    static List<String[]> sensors(JSONObject current) {
        List<String[]> out = new ArrayList<>(), rooms = new ArrayList<>();
        JSONArray list = current.optJSONArray("sensors");
        if (list == null) return out;
        for (int i = 0; i < list.length(); i++) {
            JSONObject s = list.optJSONObject(i);
            if (s == null || s.optJSONObject("fields") == null || !s.optJSONObject("fields").has("temp_f")) continue;
            (OUTDOOR.contains(s.optString("type")) ? out : rooms).add(new String[]{s.optString("sensor_id"), label(s)});
        }
        out.addAll(rooms);
        return out;
    }

    static String label(JSONObject s) {
        String name = s.optString("name", "");
        return name.isEmpty() ? s.optString("type", "sensor") + " " + s.optString("sensor_id") : name;
    }

    /** Fetch both files and keep them. A forecast that fails keeps its last good copy;
     *  a current that fails is recorded, and the widget says so over what it had. */
    static void refresh(Context c, int id) {
        String base = url(c, id);
        if (base.isEmpty()) return;
        SharedPreferences.Editor e = prefs(c).edit();
        try {
            JSONObject current = fetchJs(base, "current.js");
            e.putString(key(id, "current"), current.toString()).putString(key(id, "error"), "");
        } catch (Exception ex) {
            e.putString(key(id, "error"), explain(ex, base));
        }
        try {
            e.putString(key(id, "forecast"), fetchJs(base, "forecast.js").toString());
        } catch (Exception ignored) {
            // the forecast is optional: a station without one, or one that is offline, shows now only
        }
        e.putLong(key(id, "fetched"), System.currentTimeMillis()).apply();
        // apply() is in memory at once; the widget is drawn from it next
    }
}
