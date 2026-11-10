package org.cc0ac.weatherkiosk;

import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.util.SizeF;
import android.widget.RemoteViews;

import java.util.LinkedHashMap;
import java.util.Map;

/** The home-screen widget. Each one placed has its own station address and sensor. */
public class WeatherWidget extends AppWidgetProvider {
    static final String REFRESH = "org.cc0ac.weatherkiosk.REFRESH";
    // The sizes drawn ahead for Android 12 and later, which shows the largest that fits.
    // Android takes at most 16; the wider ones get the finer steps in height, a day row each.
    static final int[][] SIZES = {{110, 40}, {110, 110}, {110, 180},
            {200, 40}, {200, 110}, {200, 160}, {200, 180}, {200, 210}, {200, 240}, {200, 280},
            {280, 40}, {280, 110}, {280, 160}, {280, 200}, {280, 240}, {280, 280}};

    @Override
    public void onUpdate(Context c, AppWidgetManager m, int[] ids) {
        for (int id : ids) draw(c, m, id);   // what it had, at once; then fetch
        refreshInBackground(c, ids);
        RefreshJob.schedule(c);
    }

    @Override
    public void onAppWidgetOptionsChanged(Context c, AppWidgetManager m, int id, Bundle options) {
        draw(c, m, id);
    }

    @Override
    public void onDeleted(Context c, int[] ids) {
        for (int id : ids) Station.forget(c, id);
        RefreshJob.schedule(c);
    }

    @Override
    public void onReceive(Context c, Intent intent) {
        if (REFRESH.equals(intent.getAction())) {
            int id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID);
            if (id != AppWidgetManager.INVALID_APPWIDGET_ID) {
                AppWidgetManager m = AppWidgetManager.getInstance(c);
                RemoteViews busy = new RemoteViews(c.getPackageName(), R.layout.widget);
                busy.setTextViewText(R.id.asof, "Fetching…");
                m.partiallyUpdateAppWidget(id, busy);
                refreshInBackground(c, new int[]{id});
            }
            return;
        }
        super.onReceive(c, intent);
    }

    /** A broadcast may not wait on the network: goAsync, and a thread. */
    private void refreshInBackground(Context c, int[] ids) {
        final PendingResult done = goAsync();
        final Context app = c.getApplicationContext();
        new Thread(() -> {
            try {
                AppWidgetManager m = AppWidgetManager.getInstance(app);
                for (int id : ids) {
                    Station.refresh(app, id);
                    draw(app, m, id);
                }
            } finally {
                done.finish();
            }
        }, "widget-refresh").start();
    }

    static int[] ids(Context c) {
        return AppWidgetManager.getInstance(c).getAppWidgetIds(new ComponentName(c, WeatherWidget.class));
    }

    static void draw(Context c, AppWidgetManager m, int id) {
        if (Build.VERSION.SDK_INT >= 31) {
            Map<SizeF, RemoteViews> sizes = new LinkedHashMap<>();
            for (int[] wh : SIZES) sizes.put(new SizeF(wh[0], wh[1]), Render.draw(c, id, wh[0], wh[1]));
            m.updateAppWidget(id, new RemoteViews(sizes));
        } else {
            // Before 12 the launcher says the range; on a phone held upright the width is the
            // smallest and the height the largest.
            Bundle o = m.getAppWidgetOptions(id);
            int w = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 110);
            int h = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT, 40);
            m.updateAppWidget(id, Render.draw(c, id, w, h));
        }
    }
}
