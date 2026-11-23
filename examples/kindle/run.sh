#!/bin/sh
# run.sh STATION EVERY FBINK: pause the Kindle's interface, show the weather until the screen is touched,
# then give the interface back. Started detached by the CC0AC Weather book.
DIR=/mnt/us/cc0ac-weather
echo $$ > $DIR/touch.pid
sleep 2                                    # let the book that started us finish
sh $DIR/gui.sh freeze                      # nothing of the Kindle's own draws over the weather now
cd $DIR && (FBINK=$3 setsid nohup lua weather.lua "$1" "$2" > $DIR/run.log 2>&1 < /dev/null &)
# Wait for a touch. While the "Try now" button shows (weather.lua writes where it is), a touch on it
# asks for a fetch now; any other touch leaves.
while :; do
  at=$(lua $DIR/touch.lua /dev/input/event0 ${TOUCH_SCALE:-1 1}) || break
  echo "$(date) touch $at" >> $DIR/touch.log
  [ -f $DIR/retry-box ] || break
  set -- $at $(cat $DIR/retry-box)
  [ "$1" -ge "$3" ] && [ "$1" -le "$5" ] && [ "$2" -ge "$4" ] && [ "$2" -le "$6" ] || break
  touch $DIR/retry
done
rm -f $DIR/touch.pid
sh $DIR/leave.sh
