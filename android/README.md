# CC0AC Weather for Android: a home-screen widget, and kiosk mode

One app, `cc0ac-weather.apk` on the [app release](https://github.com/akienm/CC0AC-Weather/releases/tag/android-v2.1), with two uses:

- **A home-screen widget** showing your station: the temperature now, how it
  feels, and, as you make it bigger, the next hours and the coming days.
- **Kiosk mode**, which turns the phone into a wall display showing your weather
  page full screen. Switch it on from the app's own page; no computer needed.
  For a display nobody can leave, `setup-phone.sh` locks it on a freshly reset
  phone over USB.

Install it like any app. A phone set up as a kiosk with an earlier version takes
this one as an update and stays a kiosk.

## The widget

The widget needs Android 8 or later. It fills in more as you make it larger:

| Size | Shows |
|---|---|
| Small (about 2×1) | the temperature, feels-like (or humidity) and the sensor's name |
| Wide (3×1 and up) | also today's high and low, and the wind with its gusts |
| Taller (3×2) | also the next few hours: time, sky, chance of rain and temperature |
| Tall (3×3 and up) | also up to seven days: sky, chance of rain, high and low |

The bottom line says when the reading was taken. A tap there fetches a new one
now. A tap anywhere else opens the full dashboard. When the station can't be
reached, the widget keeps the last readings and the bottom line turns amber:
`⚠ Can't reach it · as of 11:13 AM`. A reading older than half an hour shows its
temperature greyed.

Units follow the station's own settings (°F or °C; mph, km/h, m/s or knots). The
forecast is whatever the station shows: the National Weather Service in the US,
or Open-Meteo elsewhere.

### Setting it up

1. Install `cc0ac-weather.apk` from the
   [app release](https://github.com/akienm/CC0AC-Weather/releases/tag/android-v2.1).
   Your phone will ask you to allow installs from your browser or file app.
2. Open **CC0AC Weather** and tap **Put one on my home screen**, or long-press
   the home screen and choose it from the widgets.
3. Tap the widget, type the station's address, for example
   `http://192.168.1.50:12345/`, and tap **Find its sensors**. Then choose the
   sensor and how often it should check (every 15, 30 or 60 minutes).

You can have more than one widget, each on its own station or sensor (outside on
one, the garage on another). Long-press a widget and choose its settings to
change it.

**A `.local` name may not work.** Many Android phones can't look up names like
`cc0ac-weather.local`. If **Find its sensors** says it can't find the station,
use its number address instead: the setup page shows it, or your router's list
of devices does.

### Away from home: this needs a port forward or Tailscale

**Out of the house, the widget works only if your station can be reached from
outside.** On your home Wi-Fi it works as it is. On mobile data, or anyone
else's Wi-Fi, the phone can't reach a computer inside your house unless you've
set up one of these:

- **Tailscale** (or ZeroTier, or a similar private network). Install it on the
  station's computer and on the phone, sign both into the same account, and give
  the widget the station's Tailscale address, for example
  `http://cc0ac-weather:12345/`. Nothing is opened to the internet. This is the
  safer choice, and it is the one to use if the widget is only for your own phones.
- **A port forward** on your router, to the station's web port. Then give the
  widget your home's outside address, best with a name and `https://`. This is
  the choice when you want to share the station with people who won't install
  anything.

The main README's section
[Seeing it from outside your house](../README.md#seeing-it-from-outside-your-house)
has the steps for both. Either way, the widget reads only `current.js` and
`forecast.js`, the same two files the dashboard itself loads. Those are the
files any outside visitor can see.

## Kiosk mode

Kiosk mode shows your weather page full screen and nothing else. It goes dark
when nobody is using it, and a touch brings it back with fresh readings.

### From the phone

1. Open **CC0AC Weather**. Under **Kiosk mode**, type your station's address,
   for example `http://192.168.1.50:12345/`.
2. Leave **Make it this phone's home screen** ticked, so the kiosk comes back by
   itself after a restart or a power cut, and tap **Start kiosk mode**.
3. Android asks whether CC0AC Weather should be the home app, and then whether
   to pin it to the screen. Say yes to both.

To leave, press Back and choose **Leave**. The phone's own home screen comes back.
After a restart, the pin is asked for again once the phone is unlocked.

This is for a phone anyone in the house may pick up and use for something else.
For one on the wall that nobody can leave, lock it, below.

## A locked kiosk

This turns a spare Android phone into a wall display that shows your weather page
and does nothing else. There are no apps, no notifications, no lock screen, no
login and no way out (unless you ask for one). It is the same kiosk mode, locked:
Back does nothing, and there is nothing to leave it with.

It uses Android's own kiosk mode, the "device owner" that schools and shops use to
lock tablets down. The phone ends up with far less exposed than a normal phone:
no app installs from unknown sources, no new users, no safe mode, no USB file
transfer, no camera, no calls or texts, and no browser to wander off into.

### What you need

- An Android phone running Android 6 or later, **freshly factory reset**.
- A computer with `adb` (Android's USB tool) and a USB cable.
- The address of your weather page, for example `http://my-pi.local:12345/`.

### Setting up a phone

1. Factory reset the phone. During setup, join your Wi-Fi but **skip signing in
   to Google** (or any account). Android only lets an app become the device owner
   on a phone with no accounts on it.
2. Turn on USB debugging: Settings > About phone > tap **Build number** seven
   times, then Settings > System > Developer options > **USB debugging**.
3. Plug the phone in, accept the "Allow USB debugging?" prompt, and run:

       ./setup-phone.sh --url http://my-pi.local:12345/

   Without a copy of this project, download `cc0ac-weather.apk` and
   `setup-phone.sh` from the [app release](https://github.com/akienm/CC0AC-Weather/releases/tag/android-v2.1)
   into one folder and add `--apk cc0ac-weather.apk`. The same command with a
   newer APK updates the phone.

The phone switches to the weather page straight away. Unplug it and hang it on
the wall. It comes back to the weather page by itself after a restart or a power
cut.

### Options

You can run `setup-phone.sh` again at any time to change settings. Settings you
leave out keep their current value.

| Option | What it does |
|---|---|
| `--url URL` | the page to show |
| `--sleep-minutes N` | go dark after N idle minutes; `0` never sleeps (default 10) |
| `--wake touch` | dark means a black screen at the lowest backlight, and **any touch wakes it** (default) |
| `--wake power` | dark means the screen is really off, and the **power button** wakes it |
| `--lock-screen` | keep the phone's normal lock screen and PIN (default: none) |
| `--exit-pin PIN` | lets someone at the phone leave the kiosk: tap the top-left corner 7 times, then enter the PIN. Settings opens; pressing Home returns to the kiosk |
| `--allow-camera` | leave the camera on (default: off) |
| `--lock-adb` | turns USB debugging off for good. This is the most locked-down setting, but **changing settings or releasing the phone afterwards needs a factory reset** |
| `--wifi SSID PASS` | join a WPA2 network (Android 11 and later) |
| `-s SERIAL` | pick a phone when more than one is plugged in |
| `--release` | undo everything, give up device ownership and uninstall the app |

**Why touch-to-wake doesn't turn the screen fully off:** a phone's touchscreen
stops listening when its display turns off, so only the power button can wake a
screen that is really off. In `touch` mode the screen stays on, black, at the
lowest backlight the phone allows. That costs almost nothing on an OLED phone and
a little on an LCD one. The phone is plugged in either way.

If the page can't be reached (the Pi is rebooting, or the Wi-Fi drops), the phone
says so and tries again every 30 seconds. After it wakes, it always loads fresh
readings.

## Building the app

`./build.sh` builds two files from the same code:

- `build/cc0ac-weather.apk`, the one on the release, which `setup-phone.sh`
  installs and a phone installs by hand;
- `build/cc0ac-weather.aab`, the bundle Google Play takes. It's built only when
  bundletool is found (`$BUNDLETOOL`, or a `bundletool-all-*.jar` in
  `~/Android/bundletool/`); without it you get the APK alone.

To build it you need a JDK 17 and the Android SDK command-line tools. You don't
need Gradle or Android Studio.

    # one time: SDK into ~/Android/sdk
    sdkmanager --sdk_root=$HOME/Android/sdk "platform-tools" "build-tools;36.0.0" "platforms;android-36"
    # for the .aab: bundletool from https://github.com/google/bundletool/releases
    # into ~/Android/bundletool/

    ./build.sh

The first build makes a signing key at `~/.cc0ac-weather/kiosk-release.jks`.
**Keep it.** Android only accepts an update signed by the same key, and without
it you would have to release and set up each phone again. The bundle is signed
with the same key, as Play's upload key.

## How it works

- `KioskActivity` is a full-screen WebView. `Kiosk` switches kiosk mode on and
  off. Switched on from the app's page, it asks Android for the home role and
  pins the screen (`startLockTask`), which Android asks the user about; the pin
  is asked only once the phone is unlocked, since Android drops a request made
  behind the lock screen. As device owner, `Policies` makes the pin permanent
  and the `Home` alias the home screen, and Back, Home, Recents and the
  notification shade all do nothing.
- `Policies` applies the device-owner policies every time the app starts, so a
  setting can't drift.
- Settings arrive through `ConfigReceiver`, which only the USB (adb) shell may
  call, so nothing running on the phone can change them.
- Tested on an Android 14 emulator (locked): setup, every escape key, sleep and
  touch wake, power-button sleep, restart with no lock screen, the exit PIN, an
  unreachable page, and release.
- Tested on Android 14 and Android 16 emulators (version 2.1, kiosk mode from
  the app's page): the home-app and pin prompts, Back asking to leave, Leave
  unpinning and giving the home screen back, a restart coming back to the kiosk
  and asking for the pin after unlock (Android 14), not becoming the home
  screen when unticked, and going dark and waking at a touch.
- The app targets Android 16 (API 36), as Google Play requires: its pages sit
  clear of the status and navigation bars, and Back is Android's predictive
  back. Tested on an Android 16 emulator.
- When a station can't be reached, the widget's settings say why: a certificate
  the phone doesn't trust (one the station made itself), a certificate for
  another name, https sent to a port that speaks only http, nothing listening,
  or no such name. Each was tested on the Android 16 emulator against a test
  server.
- The widget (`WeatherWidget`, drawn by `Render`, fetched by `Station` and
  refreshed by `RefreshJob`) reads `current.js` and `forecast.js` itself and keeps
  its own copy per widget. On Android 12 and later the layout changes smoothly as
  the widget is resized; on 8 to 11 it follows the size you set.
- Tested on an Android 14 emulator (version 2): a version 1 kiosk updated to 2
  stays a kiosk and survives a restart; released and installed fresh, the app
  isn't offered as a home screen, its icon opens the widget's page, and a widget
  is pinned, pointed at a station, and shown offline.
- Tested on a Pixel 8 Pro, Android 17, against a live station (the widget only):
  installed as an ordinary app, pinned from its own page, pointed at the
  station, and drawn with the same temperature, feels-like and wind the station
  reported; a tap opens the dashboard. The phone couldn't look up a `.local`
  name, and the settings page said so and pointed to the number address, which
  worked. The outdoor sensor comes first in the list, as on the dashboard, so a
  new widget shows outside. Not yet tested: kiosk mode on a real phone, the
  smallest and widest widget sizes, °C, and the background refresh.
