package org.cc0ac.weatherkiosk;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** The app's icon opens this: the widget is the app, so this says how to place one. */
public class InfoActivity extends Activity {
    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        int pad = Math.round(16 * getResources().getDisplayMetrics().density);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(pad, pad, pad, pad);
        TextView t = new TextView(this);
        t.setTextSize(16);
        t.setText("CC0AC Weather is a home-screen widget for your own weather station.\n\n"
                + "To place one, press and hold an empty spot on your home screen, choose Widgets, "
                + "and find CC0AC Weather. Make it bigger to see more: the next hours, then the coming days.\n\n"
                + "Place as many as you like, each showing its own sensor.\n\n"
                + "Away from home, the widget can reach your station only if you've given it a way in: "
                + "Tailscale on the station and on this phone, or a port forward on your router. "
                + "The station's README says how, under \"Seeing it from outside your house\".");
        box.addView(t);
        AppWidgetManager m = getSystemService(AppWidgetManager.class);
        if (m != null && m.isRequestPinAppWidgetSupported()) {
            Button add = new Button(this);
            add.setText("Put one on my home screen");
            add.setOnClickListener(v -> m.requestPinAppWidget(new ComponentName(this, WeatherWidget.class), null, null));
            box.addView(add);
        }
        ScrollView scroll = new ScrollView(this);
        scroll.addView(box);
        setContentView(scroll);
    }
}
