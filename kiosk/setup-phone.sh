#!/usr/bin/env bash
# Turn a freshly reset Android phone, plugged in over USB, into a weather kiosk.
# Run again at any time to change settings; --release gives the phone back.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
    cat <<'USAGE'
usage: setup-phone.sh --url URL [options]
       setup-phone.sh --release

  --url URL            the weather page to show (required on first setup)
  --sleep-minutes N    go dark after N idle minutes; 0 = never (default 10)
  --wake touch|power   touch: screen goes black, any touch wakes it (default)
                       power: real screen-off; the power button wakes it
  --lock-screen        keep the phone's lock screen (default: none, no PIN, no login)
  --exit-pin PIN       allow leaving the kiosk: tap the top-left corner 7 times, enter PIN
                       (default: no way out on screen; only this script can release it)
  --allow-camera       leave the camera on (default: disabled)
  --lock-adb           turn USB debugging off for good. After this, changing settings
                       or releasing the phone needs a factory reset. Most locked down.
  --wifi SSID PASS     join a WPA2 network first (Android 11 and later)
  --apk FILE           install this apk (default: build/weather-kiosk.apk)
  -s SERIAL            which phone, when more than one is plugged in
  --release            undo the kiosk, give up device ownership, uninstall the app
USAGE
}

PKG=org.cc0ac.weatherkiosk
APK=build/weather-kiosk.apk
URL="" SLEEP="" WAKE="" LOCKSCREEN="" EXITPIN="" CAMERA="" LOCKADB="" SSID="" PASS="" RELEASE=""
ADB=(adb)
command -v adb >/dev/null || ADB=("${ANDROID_HOME:-$HOME/Android/sdk}/platform-tools/adb")

while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="$2"; shift 2 ;;
        --sleep-minutes) SLEEP="$2"; shift 2 ;;
        --wake) WAKE="$2"; shift 2 ;;
        --lock-screen) LOCKSCREEN=1; shift ;;
        --exit-pin) EXITPIN="$2"; shift 2 ;;
        --allow-camera) CAMERA=1; shift ;;
        --lock-adb) LOCKADB=1; shift ;;
        --wifi) SSID="$2"; PASS="$3"; shift 3 ;;
        --apk) APK="$2"; shift 2 ;;
        -s) ADB+=(-s "$2"); shift 2 ;;
        --release) RELEASE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

adb_() { "${ADB[@]}" "$@"; }
q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }   # quote for the phone's shell

n=$(adb_ devices | awk 'NR>1 && $2=="device"' | wc -l)
if [ "$n" -eq 0 ]; then
    echo "No phone found. Plug it in, turn on USB debugging (Settings > About phone > tap" >&2
    echo "Build number 7 times; then Developer options > USB debugging) and accept the prompt." >&2
    exit 1
fi
if [ "$n" -gt 1 ] && [ "${#ADB[@]}" -eq 1 ]; then
    echo "More than one phone is plugged in; pick one with -s SERIAL:" >&2
    adb_ devices -l >&2
    exit 1
fi

if [ -n "$RELEASE" ]; then
    adb_ shell am broadcast -a $PKG.RELEASE -n $PKG/.ConfigReceiver | tail -1
    # Android drops the admin a moment after the app asks; until then it refuses the uninstall.
    for try in 1 2 3 4 5; do
        sleep 2
        adb_ uninstall $PKG >/dev/null 2>&1 && break
    done
    if adb_ shell pm list packages | grep -q "$PKG"; then
        echo "The app is still installed; run --release again." >&2
        exit 1
    fi
    echo "Released: the phone is an ordinary phone again."
    exit 0
fi

case "$WAKE" in ""|touch|power) ;; *) echo "--wake must be touch or power" >&2; exit 2 ;; esac
[ -f "$APK" ] || { echo "no $APK; run ./build.sh first" >&2; exit 1; }

if [ -n "$SSID" ]; then
    echo "Joining Wi-Fi $SSID..."
    adb_ shell cmd wifi connect-network "$(q "$SSID")" wpa2 "$(q "$PASS")"
fi

echo "Installing $APK..."
adb_ install -r "$APK" >/dev/null

if adb_ shell dpm list-owners 2>/dev/null | grep -q "$PKG"; then
    echo "Already the device owner."
else
    echo "Making the kiosk the device owner..."
    # Shortly after boot Android can refuse with "already some accounts" while its account
    # service is still busy, even with none on the phone; a retry a few seconds later works.
    ok=""
    for try in 1 2 3; do
        if out=$(adb_ shell dpm set-device-owner $PKG/.AdminReceiver 2>&1) && grep -q '^Success' <<<"$out"; then
            ok=1; break
        fi
        sleep 5
    done
    if [ -z "$ok" ]; then
        grep -m1 -E 'Exception|Error' <<<"$out" >&2 || echo "$out" >&2
        echo >&2
        echo "Android refused. It only allows this on a phone with no accounts on it:" >&2
        echo "factory reset it, skip signing in to Google during setup, and run this again." >&2
        echo "Accounts it found:" >&2
        adb_ shell dumpsys account 2>/dev/null | grep -E '^ +(Accounts:|Account \{)' >&2 || true
        exit 1
    fi
fi

args=(am broadcast -a $PKG.CONFIGURE -n $PKG/.ConfigReceiver)
[ -n "$URL" ] && args+=(--es url "$(q "$URL")")
[ -n "$SLEEP" ] && args+=(--ei sleep_minutes "$SLEEP")
[ -n "$WAKE" ] && args+=(--ez wake_on_touch "$([ "$WAKE" = touch ] && echo true || echo false)")
[ -n "$EXITPIN" ] && args+=(--es exit_pin "$(q "$EXITPIN")")
args+=(--ez no_lock_screen "$([ -n "$LOCKSCREEN" ] && echo false || echo true)")
args+=(--ez disable_camera "$([ -n "$CAMERA" ] && echo false || echo true)")
args+=(--ez lock_adb "$([ -n "$LOCKADB" ] && echo true || echo false)")

if [ "$WAKE" = power ] && [ -n "$SLEEP" ] && [ "$SLEEP" -gt 0 ]; then
    # The app sets this itself on Android 9+, but older phones need it from here.
    adb_ shell settings put system screen_off_timeout $((SLEEP * 60000))
fi

# Skip Android's one-time "Viewing full screen" hint; on a kiosk nobody can act on it.
adb_ shell settings put secure immersive_mode_confirmations confirmed

echo "Applying settings..."
adb_ shell "${args[@]}" | sed -n 's/^Broadcast completed: result=0, data="\(.*\)"$/  \1/p'
echo "Done. The phone now shows only the weather page; unplug it whenever you like."
