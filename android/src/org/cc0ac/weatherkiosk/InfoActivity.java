package org.cc0ac.weatherkiosk;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** The app's icon opens this: how to place the widget, and the switch for kiosk mode. */
public class InfoActivity extends Activity {
    private LinearLayout box;
    private int pad;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (Kiosk.on(this)) {   // a kiosk's Home, or its icon: straight back to the weather
            Kiosk.open(this);
            finish();
            return;
        }
        pad = Math.round(16 * getResources().getDisplayMetrics().density);
        box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(pad, pad, pad, pad);

        if (Build.VERSION.SDK_INT >= 26) {
            heading("The widget");
            text("A home-screen widget for your own weather station.\n\n"
                    + "To place one, press and hold an empty spot on your home screen, choose Widgets, "
                    + "and find CC0AC Weather. Make it bigger to see more: the next hours, then the coming days. "
                    + "Place as many as you like, each showing its own sensor.\n\n"
                    + "Away from home, the widget can reach your station only if you've given it a way in: "
                    + "Tailscale on the station and on this phone, or a port forward on your router. "
                    + "The station's README says how, under \"Seeing it from outside your house\".");
            AppWidgetManager m = getSystemService(AppWidgetManager.class);
            if (m != null && m.isRequestPinAppWidgetSupported()) {
                Button add = new Button(this);
                add.setText("Put one on my home screen");
                add.setOnClickListener(v -> m.requestPinAppWidget(new ComponentName(this, WeatherWidget.class), null, null));
                box.addView(add);
            }
        }

        heading("Kiosk mode");
        text("Turns this phone into a wall display: your station's dashboard, full screen, going dark "
                + "when nobody's touching it and waking at a touch. Android pins it to the screen and "
                + "asks you first. To leave, press Back.\n\n"
                + "For a display nobody can leave (no lock screen, no way out), set up a freshly reset "
                + "phone with setup-phone.sh from a computer; the station's README says how.");
        EditText url = new EditText(this);
        url.setSingleLine(true);
        url.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        url.setHint("http://192.168.1.20/");
        url.setText(Kiosk.suggestedUrl(this));
        box.addView(url);
        CheckBox home = new CheckBox(this);
        home.setText("Make it this phone's home screen, so it comes back after a restart");
        home.setChecked(true);
        box.addView(home);
        TextView problem = text("");
        Button start = new Button(this);
        start.setText("Start kiosk mode");
        start.setOnClickListener(v -> {
            String u = Station.normalize(url.getText().toString());
            if (u.isEmpty()) {
                problem.setText("Give it your station's address first: the one you open the dashboard at.");
                return;
            }
            Kiosk.start(this, u, home.isChecked());
        });
        box.addView(start);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(box);
        setContentView(scroll);
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == Kiosk.ASK_HOME && Kiosk.on(this)) {
            Kiosk.open(this);   // whatever the answer: kiosk mode was asked for
            finish();
        }
    }

    private void heading(String s) {
        TextView t = new TextView(this);
        t.setTextSize(20);
        t.setPadding(0, box.getChildCount() == 0 ? 0 : pad * 2, 0, pad / 2);
        t.setText(s);
        box.addView(t);
    }

    private TextView text(String s) {
        TextView t = new TextView(this);
        t.setTextSize(16);
        t.setText(s);
        box.addView(t);
        return t;
    }
}
