package org.cc0ac.weatherkiosk;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.role.RoleManager;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.provider.Settings;

/** Kiosk mode: the weather page full screen, pinned, dimming when idle.
 *
 *  Two strengths, one mode. Switched on from the app's own page, it uses Android's screen
 *  pinning, which any app may ask for and anyone can leave: Back asks "Leave kiosk mode?".
 *  On a phone setup-phone.sh made this app the device owner of, the same mode is locked:
 *  Policies makes the pinning permanent and there is no way out on screen. */
final class Kiosk {
    private Kiosk() {}

    /** Kiosk mode is on: switched on here, or locked on by setup-phone.sh. */
    static boolean on(Context c) {
        return Policies.isOwner(c) || Config.prefs(c).getBoolean("kiosk_on", false);
    }

    static boolean locked(Context c) {
        return Policies.isOwner(c);
    }

    /** A station address to offer: the kiosk's own, else the first widget's. */
    static String suggestedUrl(Context c) {
        String u = Config.load(c).url;
        if (!u.isEmpty()) return u;
        for (java.util.Map.Entry<String, ?> e : Station.prefs(c).getAll().entrySet()) {
            if (e.getKey().endsWith(".url") && e.getValue() instanceof String && !((String) e.getValue()).isEmpty()) {
                return (String) e.getValue();
            }
        }
        return "";
    }

    static final int ASK_HOME = 1;

    /** Switch kiosk mode on from the app's page. asHome: it becomes the home screen, so a
     *  restart or a press of Home comes back to it. Android asks the user about that first;
     *  the kiosk opens after (open(), from the page's onActivityResult) or at once. */
    static void start(Activity a, String url, boolean asHome) {
        Config.prefs(a).edit().putString("url", url).putBoolean("kiosk_on", true)
                .putBoolean("as_home", asHome).commit();
        Policies.claimHome(a, asHome);
        if (!asHome || !askToBeHome(a)) open(a);
    }

    static void open(Activity a) {
        a.startActivity(new Intent(a, KioskActivity.class).putExtra("reload", true));
    }

    /** Android decides the home screen; this asks it, and the user confirms. False: nothing
     *  to ask (already home, or no way to ask), so the caller opens the kiosk itself. */
    static boolean askToBeHome(Activity a) {
        if (Build.VERSION.SDK_INT >= 29) {
            RoleManager rm = a.getSystemService(RoleManager.class);
            if (rm == null || !rm.isRoleAvailable(RoleManager.ROLE_HOME) || rm.isRoleHeld(RoleManager.ROLE_HOME)) return false;
            a.startActivityForResult(rm.createRequestRoleIntent(RoleManager.ROLE_HOME), ASK_HOME);
            return true;
        }
        a.startActivityForResult(new Intent(Settings.ACTION_HOME_SETTINGS), ASK_HOME);
        return true;
    }

    /** Switch kiosk mode off (never on a locked kiosk): unpin, give the home screen back. */
    static void leave(Activity a) {
        if (locked(a)) return;
        Config.prefs(a).edit().putBoolean("kiosk_on", false).commit();
        Policies.claimHome(a, false);
        if (pinned(a)) {
            try {
                a.stopLockTask();
            } catch (IllegalStateException | SecurityException ignored) {
                // not pinned after all
            }
        }
        a.finish();   // the phone's own home screen comes back
    }

    static boolean pinned(Context c) {
        ActivityManager am = c.getSystemService(ActivityManager.class);
        return am != null && am.getLockTaskModeState() != ActivityManager.LOCK_TASK_MODE_NONE;
    }
}
