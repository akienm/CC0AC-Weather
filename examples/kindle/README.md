# A Kindle as a weather screen

An old e-ink Kindle can show your station's readings, the forecast, and every room, redrawn every
five minutes. **The Kindle does all the drawing.** It fetches `current.json` and `forecast.js`,
two small files your station already writes. The station does no extra work for it.

Tested on a Kindle Basic 2 (8th generation, firmware 5.15.1.1). Any jailbroken Kindle with FBInk
and Lua should work.

## You need

- A **jailbroken** Kindle (WinterBreak works on current firmware) with **FBInk**, which the
  jailbreak's hotfix installs at `/mnt/us/libkh/bin/fbink`. Its Lua and `cjson` are built in.
- The Kindle on the same network as your station.

## Install

1. Plug the Kindle in by USB.
2. Copy `weather.lua` and `leave.sh` into a new folder `cc0ac-weather/` at the top of the Kindle's drive.
3. Copy `cc0ac-weather.sh` and `cc0ac-weather-stop.sh` into its `documents/` folder.
4. Open `cc0ac-weather.sh` and change `STATION=` to your dashboard's address, for example
   `http://192.168.1.20:12345`.
5. Eject the Kindle. Two new "books" appear in its library: **CC0AC Weather** and
   **CC0AC Weather stop**. Tap **CC0AC Weather** to start the screen.

While the weather shows, the Kindle's own interface is switched off so that nothing draws over it.
Wi-Fi and power stay on. **Touch the screen anywhere to leave**: the weather stops and the library
comes back. (You only need the "stop" book if you started the screen some other way.) If the
Kindle ever seems stuck, holding the power button for about 40 seconds restarts it normally.

While the screen runs, the Kindle won't go to sleep. Leave it on its charger. Every sixth redraw
flashes the screen black once to clear e-ink ghosting. If the station stops answering, the screen
says so and keeps trying. If the readings are more than 15 minutes old, the bottom line says how
old they are.
