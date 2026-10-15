package org.cc0ac.weatherkiosk;

import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
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

    static boolean isOwner(Context c) {
        DevicePolicyManager dpm = c.getSystemService(DevicePolicyManager.class);
        return dpm.isDeviceOwnerApp(c.getPackageName());
    }

    /** Apply every policy from the current config. Safe to call repeatedly. */
    static void apply(Context c) {
        if (!isOwner(c)) {
            Log.w(TAG, "not device owner; running as a plain full-screen app");
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
        dpm.addPersistentPreferredActivity(a, home, new ComponentName(c, KioskActivity.class));

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
        dpm.setKeyguardDisabled(a, false);
        dpm.setStatusBarDisabled(a, false);
        dpm.setCameraDisabled(a, false);
        for (String r : RESTRICTIONS) dpm.clearUserRestriction(a, r);
        dpm.clearUserRestriction(a, UserManager.DISALLOW_DEBUGGING_FEATURES);
        dpm.clearDeviceOwnerApp(c.getPackageName());
        dpm.removeActiveAdmin(a); // or Android refuses the uninstall that follows
    }
}
