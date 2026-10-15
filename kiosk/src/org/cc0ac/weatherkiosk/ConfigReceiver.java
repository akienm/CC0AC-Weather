package org.cc0ac.weatherkiosk;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.util.Log;

/**
 * Settings from setup-phone.sh, e.g.
 *   adb shell am broadcast -a org.cc0ac.weatherkiosk.CONFIGURE -n org.cc0ac.weatherkiosk/.ConfigReceiver \
 *       --es url http://example:12345/ --ei sleep_minutes 10 --ez wake_on_touch true
 * Only extras that are present change; the rest keep their value.
 */
public class ConfigReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        if ("org.cc0ac.weatherkiosk.RELEASE".equals(i.getAction())) {
            Policies.release(c);
            Config.prefs(c).edit().clear().commit();
            Log.i(Policies.TAG, "released: no longer device owner");
            setResultData("released");
            return;
        }
        SharedPreferences.Editor e = Config.prefs(c).edit();
        if (i.hasExtra("url")) e.putString("url", i.getStringExtra("url"));
        if (i.hasExtra("exit_pin")) e.putString("exit_pin", i.getStringExtra("exit_pin"));
        if (i.hasExtra("sleep_minutes")) e.putInt("sleep_minutes", i.getIntExtra("sleep_minutes", 10));
        for (String k : new String[] {"wake_on_touch", "no_lock_screen", "lock_adb", "disable_camera"}) {
            if (i.hasExtra(k)) e.putBoolean(k, i.getBooleanExtra(k, false));
        }
        e.commit();
        Policies.apply(c);
        Config cfg = Config.load(c);
        String summary = "url=" + cfg.url + " sleep_minutes=" + cfg.sleepMinutes
                + " wake_on_touch=" + cfg.wakeOnTouch + " no_lock_screen=" + cfg.noLockScreen
                + " lock_adb=" + cfg.lockAdb + " disable_camera=" + cfg.disableCamera
                + " exit_pin=" + (cfg.exitPin.isEmpty() ? "none" : "set")
                + " device_owner=" + Policies.isOwner(c);
        Log.i(Policies.TAG, "configured: " + summary);
        setResultData(summary);
        c.startActivity(new Intent(c, KioskActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra("reload", true));
    }
}
