#!/bin/sh
# Name: CC0AC Weather
# Author: CC0AC-Weather
# Tap this book to turn the Kindle into a weather screen. Touch the screen to leave it.
STATION=http://YOUR-STATION:12345     # the address of your station's dashboard
EVERY=300                             # seconds between redraws
DIR=/mnt/us/cc0ac-weather

# Check what the screen needs before switching anything off; say what is missing.
FBINK=""
for f in /mnt/us/libkh/bin/fbink /var/local/kmc/kindlepw2/bin/fbink /var/local/kmc/kindlehf/bin/fbink \
         /mnt/us/extensions/MRInstaller/bin/KHF/fbink /mnt/us/extensions/MRInstaller/bin/PW2/fbink $(command -v fbink); do
  [ -x "$f" ] && { FBINK=$f; break; }
done
[ -n "$FBINK" ] || { echo "CC0AC Weather: FBInk not found. Install it (see the README), then tap again."; exit 1; }
lua -e 'require("cjson")' 2>/dev/null || { echo "CC0AC Weather: this Kindle has no Lua with cjson."; exit 1; }
[ -f $DIR/weather.lua ] || { echo "CC0AC Weather: copy weather.lua and leave.sh into $DIR first."; exit 1; }
case "$STATION" in *YOUR-STATION*) echo "CC0AC Weather: set STATION= in documents/cc0ac-weather.sh first."; exit 1;; esac

sh $DIR/leave.sh keep-gui             # end any copy already running
# run.sh pauses the Kindle's interface, which runs this book, so it starts detached from us.
(setsid sh $DIR/run.sh "$STATION" "$EVERY" "$FBINK" < /dev/null > /dev/null 2>&1 &)
