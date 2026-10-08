# Reddit announcement

The post, ready to paste, and the screenshots to attach. The screenshots come
from a test copy of the program, with a made-up station name, room names and
location, fed by real readings.

| File | Shows |
|---|---|
| `dashboard.png` | The dashboard on a desktop browser |
| `charts.png` | The charts page, one week |
| `phone.png` | The dashboard at phone width |
| `custom-theme.png` | The dashboard restyled with a single `theme.css` |

## Where

In this order, a day or two apart, fixing what each one turns up before the
next. Check each subreddit's rules on self-promotion first.

1. **r/AcuRite**: the owners of the hardware.
2. **r/weatherstations**: hobbyists with every kind of station.
3. **r/selfhosted**: for the "your data stays on your own machine" angle. Its
   rules ask that AI-built projects say so; the post does.
4. **r/raspberry_pi**: lead with the Pi setup; use the project flair.

Post screenshots, not a link to a live dashboard: a live link gives out the
home's IP address, and the dashboard shows the station's location and the room
names.

## The post

**Title:** I wrote a free, open-source local dashboard for the AcuRite Access
hub. Your data stays on your own machine, and the AcuRite app and Weather
Underground keep working.

**Body:**

My AcuRite Atlas and Access hub only show their data through AcuRite's cloud.
I wanted my own copy of every reading, my own dashboard, and no dependence on
their servers, without breaking the AcuRite app or my Weather Underground
uploads. So I built this. It's MIT-licensed, a single Python file with no
packages to install, and it runs on a Raspberry Pi.

**How it works:** the hub sends each reading over HTTPS to a server name it's
configured with. This program sits in that path. It records the reading, then
passes it to AcuRite unchanged, so the app keeps working, and the hub keeps
uploading to Weather Underground itself. There are two ways to set it up:

- **Change the hub's server name** to point at your machine. No extra hardware
  is needed.
- **Plug the hub into a Pi's Ethernet port.** The Pi gives it an address and
  answers AcuRite's server name itself, and the hub's settings stay untouched.

**What you get:**

- **Dashboard:** every sensor, plus feels-like, wind, the 3-hour pressure
  trend, UV and light.
- **Sun and moon:** worked out in the page, with no outside service.
- **US forecast:** the hourly and 7-day forecast and alerts from the National
  Weather Service (free, no key).
- **Charts:** any period from today to all time, or a date range. Rain totals
  for the day, week, month and year.
- **Every reading kept** in a local SQLite database.
- **Easy to customize:** one file for the colors, a folder for your own pages,
  and a documented JSON feed for anything you want to build.
- **Buttons** that open your Weather Underground or AcuRite pages, in a pane
  or in their own tab.
- **Optional radio capture** with an RTL-SDR dongle if you have no hub.

Tested on an Access hub (firmware 051) with an Atlas and six indoor tower
sensors. If you have other AcuRite sensors, I'd like to hear whether they show
up properly.

I built this with Claude Code (AI-assisted). The design, testing and hardware
work were mine.

https://github.com/akienm/CC0AC-Weather

Not affiliated with AcuRite.
