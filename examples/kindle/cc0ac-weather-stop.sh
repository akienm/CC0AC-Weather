#!/bin/sh
# Name: CC0AC Weather stop
# Author: CC0AC-Weather
for p in $(pidof lua); do grep -q weather.lua /proc/$p/cmdline 2>/dev/null && kill $p; done
lipc-set-prop com.lab126.powerd preventScreenSaver 0
echo "CC0AC Weather stopped."
