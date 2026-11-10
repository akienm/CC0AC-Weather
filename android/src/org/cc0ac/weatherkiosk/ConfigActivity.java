package org.cc0ac.weatherkiosk;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** Shown when a widget is placed, and again from the widget's settings: the station's
 *  address, which sensor, and how often to fetch. */
public class ConfigActivity extends Activity {
    private int id = AppWidgetManager.INVALID_APPWIDGET_ID;
    private EditText url;
    private TextView status;
    private Spinner sensorPick, intervalPick;
    private Button save;
    private final List<String[]> sensors = new ArrayList<>();

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        setResult(RESULT_CANCELED);   // backing out leaves no empty widget behind
        Bundle extras = getIntent().getExtras();
        if (extras != null) id = extras.getInt(AppWidgetManager.EXTRA_APPWIDGET_ID, id);
        if (id == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish();
            return;
        }
        int pad = dp(16);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(pad, pad, pad, pad);

        box.addView(text("Your weather station's address", 18));
        box.addView(text("At home, the address you open the dashboard at, such as 192.168.1.20. "
                + "Away from home the widget needs a way in to the station: Tailscale, "
                + "or a port forward on your router (see the README, \"Seeing it from outside your house\").", 14));
        url = new EditText(this);
        url.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        url.setHint("http://192.168.1.20/");
        url.setText(Station.url(this, id));
        box.addView(url);

        Button find = new Button(this);
        find.setText("Find its sensors");
        find.setOnClickListener(v -> look());
        box.addView(find);

        status = text("", 14);
        box.addView(status);

        box.addView(text("Sensor", 16));
        sensorPick = new Spinner(this);
        box.addView(sensorPick);

        box.addView(text("Fetch every", 16));
        intervalPick = new Spinner(this);
        List<String> mins = new ArrayList<>();
        int chosen = 0;
        for (int i = 0; i < Station.INTERVALS.length; i++) {
            mins.add(Station.INTERVALS[i] + " minutes");
            if (Station.INTERVALS[i] == Station.interval(this, id)) chosen = i;
        }
        intervalPick.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, mins));
        intervalPick.setSelection(chosen);
        box.addView(intervalPick);
        box.addView(text("Android fetches no oftener than every 15 minutes. Tap the widget's bottom line to fetch now; "
                + "tap the rest to open the dashboard.", 13));

        save = new Button(this);
        save.setText("Done");
        save.setEnabled(false);
        save.setOnClickListener(v -> done());
        box.addView(save);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(box);
        setContentView(scroll);
        if (!url.getText().toString().trim().isEmpty()) look();
    }

    private void look() {
        final String base = Station.normalize(url.getText().toString());
        if (base.isEmpty()) {
            status.setText("Type the station's address first.");
            return;
        }
        status.setText("Looking at " + base + " …");
        save.setEnabled(false);
        new Thread(() -> {
            String err = null;
            List<String[]> found = new ArrayList<>();
            try {
                JSONObject current = Station.fetchJs(base, "current.js");
                found = Station.sensors(current);
                if (found.isEmpty()) err = "The station answered, but has no sensor reading a temperature yet.";
            } catch (Exception e) {
                err = "Couldn't reach a station at " + base + ": " + e.getMessage()
                        + (base.contains(".local") ? "\nSome phones can't find .local names; try the station's number address." : "");
            }
            final String problem = err;
            final List<String[]> list = found;
            runOnUiThread(() -> shown(base, list, problem));
        }, "config-look").start();
    }

    private void shown(String base, List<String[]> list, String problem) {
        sensors.clear();
        sensors.addAll(list);
        List<String> names = new ArrayList<>();
        int chosen = 0;
        String had = Station.sensor(this, id);
        for (int i = 0; i < list.size(); i++) {
            names.add(list.get(i)[1]);
            if (list.get(i)[0].equals(had)) chosen = i;
        }
        sensorPick.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, names));
        sensorPick.setSelection(chosen);
        if (problem != null) {
            status.setText(problem);
            return;
        }
        url.setText(base);
        status.setText("Found " + list.size() + (list.size() == 1 ? " sensor." : " sensors."));
        save.setEnabled(true);
    }

    private void done() {
        int s = sensorPick.getSelectedItemPosition();
        if (s < 0 || s >= sensors.size()) return;
        Station.save(this, id, Station.normalize(url.getText().toString()), sensors.get(s)[0],
                Station.INTERVALS[intervalPick.getSelectedItemPosition()]);
        RefreshJob.schedule(this);
        // The widget fetches at once, through its own refresh (a broadcast that runs in the background).
        sendBroadcast(new Intent(this, WeatherWidget.class).setAction(WeatherWidget.REFRESH)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id));
        setResult(RESULT_OK, new Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id));
        finish();
    }

    private TextView text(String s, int sp) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setPadding(0, dp(8), 0, dp(4));
        t.setLayoutParams(new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return t;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
