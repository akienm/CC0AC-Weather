#!/bin/sh
# leave.sh: end the weather screen and give the Kindle its interface back ("keep-gui": leave it paused)
DIR=/mnt/us/cc0ac-weather
for p in $(pidof lua); do grep -qE 'weather.lua|touch.lua' /proc/$p/cmdline 2>/dev/null && kill $p; done
[ -f $DIR/touch.pid ] && kill "$(cat $DIR/touch.pid)" 2>/dev/null; rm -f $DIR/touch.pid
for p in $(pidof dd); do [ "$(readlink /proc/$p/fd/0)" = /dev/input/event0 ] && kill $p; done
lipc-set-prop com.lab126.powerd preventScreenSaver 0
[ "$1" = keep-gui ] && exit 0
sh $DIR/gui.sh thaw
start lab126_gui 2>/dev/null               # in case an older copy stopped it outright
lipc-set-prop com.lab126.appmgrd start app://com.lab126.booklet.home   # the library redraws itself
