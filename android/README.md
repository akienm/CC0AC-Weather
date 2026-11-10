# CC0AC Weather for Android: a home-screen widget, or a wall kiosk

One app, `cc0ac-weather.apk` on the [app release](https://github.com/akienm/CC0AC-Weather/releases/tag/android-v2.0), with two uses:

- **On your own phone** it's a home-screen widget showing your station: the
  temperature now, how it feels, and, as you make it bigger, the next hours and
  the coming days. Install it like any app.
- **On a spare, freshly reset phone** it's a kiosk, a wall display showing only
  your weather page. `setup-phone.sh` sets that up over USB.

Nothing changes on an ordinary phone because the kiosk is in the app: the kiosk
only switches on when `setup-phone.sh` makes the app the phone's "device owner",
and Android allows that only on a phone that was just reset. A phone that was
set up as a kiosk with version 1 takes this one as an update.

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
   [app release](https://github.com/akienm/CC0AC-Weather/releases/tag/android-v2.0).
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

## The wall kiosk

This turns a spare Android phone into a wall display that shows your weather page
and does nothing else. There are no apps, no notifications, no lock screen, no
login and no way out (unless you ask for one). It goes dark when nobody is
using it, and a touch brings it back.

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
   `setup-phone.sh` from the [app release](https://github.com/akienm/CC0AC-Weather/releases/tag/android-v2.0)
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

`./build.sh` builds `build/cc0ac-weather.apk`, which `setup-phone.sh` installs. To build it you need a
JDK 17 and the Android SDK command-line tools. You don't need Gradle or Android
Studio.

    # one time: SDK into ~/Android/sdk
    sdkmanager --sdk_root=$HOME/Android/sdk "platform-tools" "build-tools;34.0.0" "platforms;android-34"

    ./build.sh

The first build makes a signing key at `~/.cc0ac-weather/kiosk-release.jks`.
**Keep it.** Android only accepts an update signed by the same key, and without
it you would have to release and set up each phone again.

## How it works

- `KioskActivity` is a full-screen WebView. It becomes the phone's home screen
  through the `Home` alias, which is off until `Policies` turns it on as device
  owner (and off again at release), and it is pinned with Android's lock-task mode. Home, Back, Recents and the notification
  shade all do nothing.
- `Policies` applies the device-owner policies every time the app starts, so a
  setting can't drift.
- Settings arrive through `ConfigReceiver`, which only the USB (adb) shell may
  call, so nothing running on the phone can change them.
- Tested on an Android 14 emulator: setup, every escape key, sleep and touch wake,
  power-button sleep, restart with no lock screen, the exit PIN, an unreachable
  page, and release.
- The widget (`WeatherWidget`, drawn by `Render`, fetched by `Station` and
  refreshed by `RefreshJob`) reads `current.js` and `forecast.js` itself and keeps
  its own copy per widget. On Android 12 and later the layout changes smoothly as
  the widget is resized; on 8 to 11 it follows the size you set.
- Tested on an Android 14 emulator (version 2): a version 1 kiosk updated to 2
  stays a kiosk and survives a restart; released and installed fresh, the app
  isn't offered as a home screen, its icon opens the widget's page, and a widget
  is pinned, pointed at a station, and shown offline. Not yet tested: a real
  phone, the smallest and widest widget sizes, °C, and the background refresh.
