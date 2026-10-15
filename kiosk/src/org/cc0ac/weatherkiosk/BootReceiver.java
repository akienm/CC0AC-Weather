package org.cc0ac.weatherkiosk;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** The kiosk is the home screen, so boot reaches it anyway; this is the belt to that braces. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        if (!Intent.ACTION_BOOT_COMPLETED.equals(i.getAction())) return;
        Policies.apply(c);
        c.startActivity(new Intent(c, KioskActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }
}
