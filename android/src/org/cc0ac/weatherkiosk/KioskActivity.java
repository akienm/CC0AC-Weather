package org.cc0ac.weatherkiosk;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.IntentFilter;
import android.content.Intent;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.TextView;

/** The whole kiosk: the weather page, full screen, with a sleep that a touch wakes. */
public class KioskActivity extends Activity {
    private static final long RETRY_MS = 30_000;
    private static final int EXIT_TAPS = 7;           // taps in the top-left corner open the PIN prompt
    private static final long EXIT_TAP_WINDOW_MS = 4_000;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private View sleepCover;
    private TextView message;
    private Config cfg;
    private boolean asleep;
    private boolean loadFailed;
    private boolean askedToPin;   // kiosk mode switched on here: ask Android to pin once per opening
    private int cornerTaps;
    private long firstCornerTap;

    private final Runnable sleep = this::goToSleep;
    private final Runnable retry = this::load;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        Policies.apply(this);
        registerReceiver(unlocked, new IntentFilter(Intent.ACTION_USER_PRESENT));
        cfg = Config.load(this);

        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return false; // links stay inside the kiosk; there is no browser to escape into
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                if (!loadFailed) message.setVisibility(View.GONE); // an error page finishes too
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (!r.isForMainFrame()) return;
                loadFailed = true;
                showMessage("Can't reach the weather station (" + e.getDescription()
                        + ").\nTrying again in 30 seconds.");
                handler.removeCallbacks(retry);
                handler.postDelayed(retry, RETRY_MS);
            }
        });
        root.addView(web);

        message = new TextView(this);
        message.setTextColor(Color.WHITE);
        message.setBackgroundColor(Color.BLACK);
        message.setTextSize(22);
        message.setGravity(Gravity.CENTER);
        message.setPadding(48, 48, 48, 48);
        message.setVisibility(View.GONE);
        root.addView(message);

        sleepCover = new View(this);
        sleepCover.setBackgroundColor(Color.BLACK);
        sleepCover.setVisibility(View.GONE);
        root.addView(sleepCover);

        setContentView(root);
        load();
    }

    private void load() {
        if (cfg.url.isEmpty()) {
            showMessage(Kiosk.locked(this) ? "No weather station address is set.\nRun setup-phone.sh with --url."
                    : "No weather station address is set.\nPress Back, then set one on CC0AC Weather's page.");
            return;
        }
        handler.removeCallbacks(retry);
        loadFailed = false;
        showMessage("Connecting to the weather station...");
        web.loadUrl(cfg.url);
    }

    private void showMessage(String text) {
        message.setText(text);
        message.setVisibility(View.VISIBLE);
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        if (i.getBooleanExtra("reload", false)) {
            askedToPin = false;
            cfg = Config.load(this);
            load();
            wake();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemBars();
        if (Kiosk.locked(this)) pin();   // pinned for good, no question asked
        wake();
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(sleep);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (!hasFocus) return;
        hideSystemBars();
        askToPin();
    }

    /** Switched on from the app's page: Android asks "Pin this app?", once per opening, so
     *  saying no doesn't bring the question back. Never behind the lock screen: after a
     *  restart the home screen is up and focused under it, and the question is dropped
     *  there, so it waits for the unlock (unlocked). */
    private void askToPin() {
        if (Kiosk.locked(this) || !Kiosk.on(this) || askedToPin || Kiosk.pinned(this)) return;
        KeyguardManager km = getSystemService(KeyguardManager.class);
        if (km != null && km.isKeyguardLocked()) return;
        askedToPin = true;
        pin();
    }

    private final BroadcastReceiver unlocked = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            handler.postDelayed(KioskActivity.this::askToPin, 1000);   // once the lock screen has gone
        }
    };

    @Override
    protected void onDestroy() {
        unregisterReceiver(unlocked);
        super.onDestroy();
    }

    private void pin() {
        try {
            startLockTask();
        } catch (IllegalStateException | SecurityException ignored) {
            // already pinned, or ownership was just released
        }
    }

    @SuppressWarnings("deprecation")
    private void hideSystemBars() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        if (asleep) {
            // The touch that wakes the screen is swallowed, so it can't press a button blind.
            if (ev.getActionMasked() == MotionEvent.ACTION_DOWN) wake();
            return true;
        }
        if (ev.getActionMasked() == MotionEvent.ACTION_DOWN) {
            countCornerTap(ev);
            scheduleSleep();
        }
        return super.dispatchTouchEvent(ev);
    }

    /** Awake: full brightness, screen held on until the idle timer runs out. */
    private void wake() {
        boolean wasAsleep = asleep;
        asleep = false;
        sleepCover.setVisibility(View.GONE);
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
        getWindow().setAttributes(lp);
        if (cfg.wakeOnTouch || cfg.sleepMinutes == 0) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            // Real screen-off: the system's own timeout (set by Policies) does the sleeping.
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        if (wasAsleep && web != null) load(); // show fresh readings, not the ones from before the nap
        scheduleSleep();
    }

    private void scheduleSleep() {
        handler.removeCallbacks(sleep);
        if (cfg.wakeOnTouch && cfg.sleepMinutes > 0) {
            handler.postDelayed(sleep, cfg.sleepMinutes * 60_000L);
        }
    }

    /**
     * Touch-to-wake sleep. A phone's touchscreen is off when its display is, so a true
     * screen-off can only be woken by the power button. Instead the screen stays on,
     * black, at the lowest backlight the hardware allows, and still listens for a touch.
     */
    private void goToSleep() {
        asleep = true;
        sleepCover.setVisibility(View.VISIBLE);
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_OFF;
        getWindow().setAttributes(lp);
    }

    private void countCornerTap(MotionEvent ev) {
        if (cfg.exitPin.isEmpty()) return;
        float corner = 120 * getResources().getDisplayMetrics().density / 2;
        if (ev.getX() > corner || ev.getY() > corner) {
            cornerTaps = 0;
            return;
        }
        long now = System.currentTimeMillis();
        if (cornerTaps == 0 || now - firstCornerTap > EXIT_TAP_WINDOW_MS) {
            cornerTaps = 0;
            firstCornerTap = now;
        }
        if (++cornerTaps >= EXIT_TAPS) {
            cornerTaps = 0;
            askForPin();
        }
    }

    private void askForPin() {
        EditText pin = new EditText(this);
        pin.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        new AlertDialog.Builder(this)
                .setTitle("Leave the kiosk")
                .setMessage("Enter the exit PIN. The kiosk comes back when Home is pressed or the phone restarts.")
                .setView(pin)
                .setPositiveButton("OK", (d, w) -> {
                    if (cfg.exitPin.equals(pin.getText().toString())) {
                        handler.removeCallbacks(sleep);
                        stopLockTask();
                        startActivity(new Intent(Settings.ACTION_SETTINGS)
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                    }
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else if (!Kiosk.locked(this)) {
            new AlertDialog.Builder(this)
                    .setTitle("Leave kiosk mode?")
                    .setMessage("The phone goes back to its own home screen. Turn kiosk mode on again from CC0AC Weather's page.")
                    .setPositiveButton("Leave", (d, w) -> Kiosk.leave(this))
                    .setNegativeButton("Stay", null)
                    .show();
        }
    }
}
