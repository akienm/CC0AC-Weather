#!/bin/sh
# Name: CC0AC Weather
# Author: CC0AC-Weather
# Tap this book to turn the Kindle into a weather screen. Touch the screen to leave it.
STATION=http://YOUR-STATION:12345     # the address of your station's dashboard
EVERY=300                             # seconds between redraws
DIR=/mnt/us/cc0ac-weather
sh $DIR/leave.sh keep-gui             # end any copy already running
# Stop the Kindle's own interface so nothing draws over the weather; Wi-Fi and power stay up.
stop lab126_gui; sleep 3
cd $DIR && (setsid nohup lua weather.lua "$STATION" "$EVERY" > $DIR/run.log 2>&1 < /dev/null &)
# Any touch on the screen ends the weather screen and brings the library back.
(setsid sh -c "echo \$\$ > $DIR/touch.pid; dd if=/dev/input/event0 bs=16 count=1 > /dev/null 2>&1; rm -f $DIR/touch.pid; sh $DIR/leave.sh" < /dev/null > /dev/null 2>&1 &)
