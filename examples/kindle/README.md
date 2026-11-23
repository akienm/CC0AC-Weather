# A Kindle as a weather screen

An old e-ink Kindle can show your station's readings, the forecast, and every room, redrawn every
five minutes. **The Kindle does all the drawing.** It fetches `current.json` and `forecast.js`,
two small files your station already writes. The station does no extra work for it.

Tested on a Kindle Basic 2 (8th generation, firmware 5.15.1.1, jailbroken with WinterBreak).
Other touchscreen Kindles that run firmware 5.x and can be jailbroken should work, but none has
been tested yet.

## 1. Jailbreak the Kindle

This part is not ours, and the people who maintain it keep it current, so we point to them
rather than copy it: **[kindlemodding.org/jailbreaking](https://kindlemodding.org/jailbreaking/)**.
Its *Jailbreaking Wizard* asks for your model and firmware and picks the method that works.
Do the post-jailbreak steps too (**Setting Up A Hotfix**, and **Disabling OTA Updates** so an
Amazon update doesn't undo it).

The weather screen needs **FBInk**, the tool that draws on the e-ink screen. On ours it came with
the jailbreak's hotfix. If the start book (below) says FBInk is missing, install it from the
kindlemodding.org "What's Next" pages. Lua and `cjson` are already on the Kindle.

## 2. Copy our files over

Plug the Kindle into a computer by USB. It shows up as a drive.

| From this folder | To the Kindle drive |
|---|---|
| `weather.lua`, `touch.lua`, `run.sh`, `gui.sh`, `leave.sh` | a new folder `cc0ac-weather/` at the top of the drive |
| `cc0ac-weather.sh`, `cc0ac-weather-stop.sh` | the `documents/` folder |

## 3. Tell it where your station is

On the Kindle drive, open `documents/cc0ac-weather.sh` in a plain text editor and change the line

    STATION=http://YOUR-STATION:12345

to your dashboard's address, the one you open in a browser, for example
`STATION=http://192.168.1.20:12345`. Use the station's local network address, because the Kindle
must reach it over Wi-Fi. `EVERY=300` is the number of seconds between redraws.

Eject the Kindle and unplug it from the computer. Two new "books" appear in its library:
**CC0AC Weather** and **CC0AC Weather stop**.

## 4. Use it

Tap **CC0AC Weather**. If something is missing, the book says what. Otherwise the library
shows for a moment, the screen flickers, and the weather appears, about 3 seconds in all.

While the weather shows, the Kindle's own interface is **paused**, so nothing draws over it.
Wi-Fi and power keep running. **Touch the screen anywhere to leave**: the interface resumes, and
the library is back in about a second. (You only need the "stop" book if you started the screen
some other way.) If the Kindle ever seems stuck, holding the power button for about 40 seconds
restarts it normally.

While the screen runs, the Kindle won't go to sleep. Leave it on its charger. Every sixth redraw
flashes the screen black once to clear e-ink ghosting. If the station stops answering, the screen
says so, keeps trying, and shows a **Try now** button: touch it to try at once, or touch anywhere
else to leave. The Kindle doesn't rejoin Wi-Fi by itself after a drop, so when the station can't be
reached the screen asks Wi-Fi to reconnect first, and says "Wi-Fi is down" if it stays down. If the readings are more than 15 minutes old, the bottom line says how
old they are.
