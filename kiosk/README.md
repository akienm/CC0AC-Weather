# Weather Kiosk for old Android phones

This turns a spare Android phone into a wall display that shows your weather page
and does nothing else. There are no apps, no notifications, no lock screen, no
login and no way out (unless you ask for one). It goes dark when nobody is
using it, and a touch brings it back.

It uses Android's own kiosk mode, the "device owner" that schools and shops use to
lock tablets down. The phone ends up with far less exposed than a normal phone:
no app installs from unknown sources, no new users, no safe mode, no USB file
transfer, no camera, no calls or texts, and no browser to wander off into.

## What you need

- An Android phone running Android 6 or later, **freshly factory reset**.
- A computer with `adb` (Android's USB tool) and a USB cable.
- The address of your weather page, for example `http://my-pi.local:12345/`.

## Setting up a phone

1. Factory reset the phone. During setup, join your Wi-Fi but **skip signing in
   to Google** (or any account). Android only lets an app become the device owner
   on a phone with no accounts on it.
2. Turn on USB debugging: Settings > About phone > tap **Build number** seven
   times, then Settings > System > Developer options > **USB debugging**.
3. Plug the phone in, accept the "Allow USB debugging?" prompt, and run:

       ./setup-phone.sh --url http://my-pi.local:12345/

The phone switches to the weather page straight away. Unplug it and hang it on
the wall. It comes back to the weather page by itself after a restart or a power
cut.

## Options

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

`setup-phone.sh` installs `build/weather-kiosk.apk`. To build it you need a
JDK 17 and the Android SDK command-line tools. You don't need Gradle or Android
Studio.

    # one time: SDK into ~/Android/sdk
    sdkmanager --sdk_root=$HOME/Android/sdk "platform-tools" "build-tools;34.0.0" "platforms;android-34"

    ./build.sh

The first build makes a signing key at `~/.cc0ac-weather/kiosk-release.jks`.
**Keep it.** Android only accepts an update signed by the same key, and without
it you would have to release and set up each phone again.

## How it works

- `KioskActivity` is a full-screen WebView set as the phone's home screen and
  pinned with Android's lock-task mode. Home, Back, Recents and the notification
  shade all do nothing.
- `Policies` applies the device-owner policies every time the app starts, so a
  setting can't drift.
- Settings arrive through `ConfigReceiver`, which only the USB (adb) shell may
  call, so nothing running on the phone can change them.
- Tested on an Android 14 emulator: setup, every escape key, sleep and touch wake,
  power-button sleep, restart with no lock screen, the exit PIN, an unreachable
  page, and release.
