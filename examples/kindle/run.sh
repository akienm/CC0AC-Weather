#!/bin/sh
# run.sh STATION EVERY FBINK: pause the Kindle's interface, show the weather until the screen is touched,
# then give the interface back. Started detached by the CC0AC Weather book.
DIR=/mnt/us/cc0ac-weather
echo $$ > $DIR/touch.pid
sleep 2                                    # let the book that started us finish
sh $DIR/gui.sh freeze                      # nothing of the Kindle's own draws over the weather now
cd $DIR && (FBINK=$3 setsid nohup lua weather.lua "$1" "$2" > $DIR/run.log 2>&1 < /dev/null &)
dd if=/dev/input/event0 bs=16 count=1 > /dev/null 2>&1   # wait for a touch
rm -f $DIR/touch.pid
sh $DIR/leave.sh
