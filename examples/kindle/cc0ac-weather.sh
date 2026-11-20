#!/bin/sh
# Name: CC0AC Weather
# Author: CC0AC-Weather
# Tap this book to turn the Kindle into a weather screen. Tap "CC0AC Weather stop" to end it.
STATION=http://YOUR-STATION:12345     # the address of your station's dashboard
EVERY=300                             # seconds between redraws
DIR=/mnt/us/cc0ac-weather
for p in $(pidof lua); do grep -q weather.lua /proc/$p/cmdline 2>/dev/null && kill $p; done
rm -f $DIR/stop
cd $DIR && (setsid nohup lua weather.lua "$STATION" "$EVERY" > $DIR/run.log 2>&1 < /dev/null &)
echo "CC0AC Weather started; first screen in a few seconds."
