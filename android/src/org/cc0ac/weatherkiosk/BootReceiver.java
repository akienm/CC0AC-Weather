package org.cc0ac.weatherkiosk;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** On a kiosk: the kiosk is the home screen, so boot reaches it anyway; this is the belt to
 *  that braces. After an update it re-applies the policy, which switches the Home alias on
 *  (a kiosk set up by version 1 had no alias). On an ordinary phone it does nothing. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        String a = i.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(a) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) return;
        if (!Policies.isOwner(c)) return;
        Policies.apply(c);
        c.startActivity(new Intent(c, KioskActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }
}
