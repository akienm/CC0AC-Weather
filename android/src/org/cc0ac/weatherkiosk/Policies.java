package org.cc0ac.weatherkiosk;

import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.UserManager;
import android.provider.Settings;
import android.util.Log;

/** Device-owner policy: what makes the phone nothing but a weather kiosk. */
final class Policies {
    static final String TAG = "WeatherKiosk";

    // Restrictions that close the usual ways out of, or into, a wall phone.
    private static final String[] RESTRICTIONS = {
        UserManager.DISALLOW_SAFE_BOOT,
        UserManager.DISALLOW_ADD_USER,
        UserManager.DISALLOW_INSTALL_UNKNOWN_SOURCES,
        UserManager.DISALLOW_MOUNT_PHYSICAL_MEDIA,
        UserManager.DISALLOW_USB_FILE_TRANSFER,
        UserManager.DISALLOW_MODIFY_ACCOUNTS,
        UserManager.DISALLOW_CREATE_WINDOWS,
        UserManager.DISALLOW_OUTGOING_CALLS,
        UserManager.DISALLOW_SMS,
    };

    static ComponentName admin(Context c) {
        return new ComponentName(c, AdminReceiver.class);
    }

    /** The alias that claims the home screen; off unless this app is the device owner. */
    static ComponentName home(Context c) {
        return new ComponentName(c.getPackageName(), c.getPackageName() + ".Home");
    }

    static void claimHome(Context c, boolean on) {
        c.getPackageManager().setComponentEnabledSetting(home(c),
                on ? PackageManager.COMPONENT_ENABLED_STATE_ENABLED : PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                PackageManager.DONT_KILL_APP);
    }

    static boolean isOwner(Context c) {
        DevicePolicyManager dpm = c.getSystemService(DevicePolicyManager.class);
        return dpm.isDeviceOwnerApp(c.getPackageName());
    }

    /** Apply every policy from the current config. Safe to call repeatedly. */
    static void apply(Context c) {
        if (!isOwner(c)) {
            Log.w(TAG, "not device owner; no kiosk policy applied");
            return;
        }
        Config cfg = Config.load(c);
        DevicePolicyManager dpm = c.getSystemService(DevicePolicyManager.class);
        ComponentName a = admin(c);

        dpm.setLockTaskPackages(a, new String[] {c.getPackageName()});
        if (Build.VERSION.SDK_INT >= 28) {
            dpm.setLockTaskFeatures(a, DevicePolicyManager.LOCK_TASK_FEATURE_NONE);
        }

        IntentFilter home = new IntentFilter(Intent.ACTION_MAIN);
        home.addCategory(Intent.CATEGORY_HOME);
        home.addCategory(Intent.CATEGORY_DEFAULT);
        claimHome(c, true);
        dpm.clearPackagePersistentPreferredActivities(a, c.getPackageName());   // version 1 named KioskActivity itself
        dpm.addPersistentPreferredActivity(a, home, home(c));

        dpm.setKeyguardDisabled(a, cfg.noLockScreen);
        dpm.setStatusBarDisabled(a, true);
        dpm.setCameraDisabled(a, cfg.disableCamera);

        for (String r : RESTRICTIONS) dpm.addUserRestriction(a, r);
        if (cfg.lockAdb) dpm.addUserRestriction(a, UserManager.DISALLOW_DEBUGGING_FEATURES);
        else dpm.clearUserRestriction(a, UserManager.DISALLOW_DEBUGGING_FEATURES);

        // A wall phone is always plugged in; without this it would never sleep.
        dpm.setGlobalSetting(a, Settings.Global.STAY_ON_WHILE_PLUGGED_IN, "0");
        if (Build.VERSION.SDK_INT >= 28 && cfg.sleepMinutes > 0 && !cfg.wakeOnTouch) {
            dpm.setSystemSetting(a, Settings.System.SCREEN_OFF_TIMEOUT,
                    String.valueOf(cfg.sleepMinutes * 60_000));
        }
    }

    /** Undo everything and give up device ownership, so the app can be uninstalled. */
    static void release(Context c) {
        if (!isOwner(c)) return;
        DevicePolicyManager dpm = c.getSystemService(DevicePolicyManager.class);
        ComponentName a = admin(c);
        dpm.setLockTaskPackages(a, new String[0]);
        dpm.clearPackagePersistentPreferredActivities(a, c.getPackageName());
        claimHome(c, false);
        dpm.setKeyguardDisabled(a, false);
        dpm.setStatusBarDisabled(a, false);
        dpm.setCameraDisabled(a, false);
        for (String r : RESTRICTIONS) dpm.clearUserRestriction(a, r);
        dpm.clearUserRestriction(a, UserManager.DISALLOW_DEBUGGING_FEATURES);
        dpm.clearDeviceOwnerApp(c.getPackageName());
        dpm.removeActiveAdmin(a); // or Android refuses the uninstall that follows
    }
}
