package org.cc0ac.weatherkiosk;

import android.content.Context;
import android.content.SharedPreferences;

/** The kiosk's settings. Written only by ConfigReceiver (adb), read everywhere else. */
final class Config {
    static final String PREFS = "kiosk";

    final String url;
    final int sleepMinutes;      // 0 = never sleep
    final boolean wakeOnTouch;   // true: dim to black, any touch wakes. false: real screen-off, power button wakes
    final boolean noLockScreen;  // true: keyguard disabled, never asks for a PIN
    final boolean lockAdb;       // true: USB debugging turned off and locked (re-setup then needs a factory reset)
    final boolean disableCamera;
    final String exitPin;        // "" = no on-screen exit; only adb can release the phone

    private Config(SharedPreferences p) {
        url = p.getString("url", "");
        sleepMinutes = p.getInt("sleep_minutes", 10);
        wakeOnTouch = p.getBoolean("wake_on_touch", true);
        noLockScreen = p.getBoolean("no_lock_screen", true);
        lockAdb = p.getBoolean("lock_adb", false);
        disableCamera = p.getBoolean("disable_camera", true);
        exitPin = p.getString("exit_pin", "");
    }

    static Config load(Context c) {
        return new Config(prefs(c));
    }

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
