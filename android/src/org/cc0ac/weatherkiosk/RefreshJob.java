package org.cc0ac.weatherkiosk;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Context;

/** One periodic job for every widget, at the shortest interval any of them asked for;
 *  each widget is fetched when its own interval has passed. */
public class RefreshJob extends JobService {
    static final int JOB = 1;

    static void schedule(Context c) {
        JobScheduler js = c.getSystemService(JobScheduler.class);
        int[] ids = WeatherWidget.ids(c);
        if (ids.length == 0) {
            js.cancel(JOB);
            return;
        }
        int minutes = 60;
        for (int id : ids) if (Station.configured(c, id)) minutes = Math.min(minutes, Station.interval(c, id));
        JobInfo.Builder b = new JobInfo.Builder(JOB, new ComponentName(c, RefreshJob.class))
                .setPeriodic(minutes * 60_000L)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPersisted(true);
        JobInfo had = js.getPendingJob(JOB);
        if (had == null || had.getIntervalMillis() != minutes * 60_000L) js.schedule(b.build());
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        final Context c = getApplicationContext();
        new Thread(() -> {
            AppWidgetManager m = AppWidgetManager.getInstance(c);
            long now = System.currentTimeMillis();
            for (int id : WeatherWidget.ids(c)) {
                long last = Station.prefs(c).getLong(Station.key(id, "fetched"), 0);
                if (Station.configured(c, id) && now - last >= (Station.interval(c, id) - 2) * 60_000L)
                    Station.refresh(c, id);
                WeatherWidget.draw(c, m, id);   // "as of" greys as a reading ages, fetched or not
            }
            jobFinished(params, false);
        }, "widget-job").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;
    }
}
