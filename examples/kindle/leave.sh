#!/bin/sh
# leave.sh: end the weather screen and give the Kindle its interface back ("keep-gui": don't restart it)
DIR=/mnt/us/cc0ac-weather
for p in $(pidof lua); do grep -q weather.lua /proc/$p/cmdline 2>/dev/null && kill $p; done
[ -f $DIR/touch.pid ] && kill "$(cat $DIR/touch.pid)" 2>/dev/null; rm -f $DIR/touch.pid
for p in $(pidof dd); do [ "$(readlink /proc/$p/fd/0)" = /dev/input/event0 ] && kill $p; done
lipc-set-prop com.lab126.powerd preventScreenSaver 0
[ "$1" = keep-gui ] || start lab126_gui
