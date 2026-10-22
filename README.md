# CC0AC-Weather

Keep your AcuRite weather station working on your own terms: your own
dashboard, your own data files, Weather Underground uploads unchanged, and no
dependence on AcuRite's cloud. Built by Claude Code ("cc.0") with Akien
MacIain; contributions welcome.

**Other brands** (Ambient Weather, Ecowitt, Fine Offset and its relabels,
Bresser, La Crosse, and more) can feed it too, by console upload or by radio.
Only AcuRite is tested on real hardware so far; [SUPPORTED.md](SUPPORTED.md)
says what works how, and how sure we are.

Not affiliated with or endorsed by AcuRite or Chaney Instrument Co. AcuRite is
their trademark; it is used here only to say which hardware this works with.

## How it works

The AcuRite **Access** hub (or smartHUB) uploads each sensor reading over HTTPS
to a server named in its settings. CC0AC-Weather is that server:

    sensors ──radio──▶ Access hub ──HTTPS──▶ this program (port 443)
                                              ├─ current.json / current.js / history.csv / weather.db
                                              ├─ relays the reading to AcuRite (optional; their app keeps working)
                                              └─ web server ──▶ weather.html dashboard, charts.html charts
    The hub uploads to Weather Underground itself; that is untouched.

- One burst every 5 minutes, one request per sensor (Atlas, 5-in-1, Iris, towers, …).
- `current.js` loads with a plain `<script>` tag, so the dashboard also works
  opened as a file or from any static host — no web server required to view it.
- `weather.db` (SQLite) keeps every reading for good, one row per sensor reading,
  with each value in its own column and the hub's whole request kept beside it,
  so nothing the hub sends is lost. `history.csv` is the same record as plain text.
  Readings from before the database existed load from the raw hub logs:
  `python3 acurite-capture.py --import-raw` (safe to run again).

No hub? The older path still works: an RTL-SDR dongle and rtl_433 receive the
sensors directly (see *Radio capture* below).

## The easy way: a ready-made Raspberry Pi card

No programming and no router settings. You put a ready-made card in a
Raspberry Pi, plug the hub's network cable into the Pi instead of your router,
and answer one page in your web browser. The Pi then sits between the hub and
the internet: it keeps every reading and passes each one on, so the AcuRite app
and Weather Underground carry on as before.

**You need:**

- A Raspberry Pi with **both** a network port and Wi-Fi: Pi 3 B+, Pi 4, Pi 400
  or Pi 5 (not a Pi Zero, which has no network port). Its power supply.
- A microSD card, 16 GB or more (it gets erased).
- A computer to write the card from, and your Wi-Fi name and password.
- Your AcuRite Access hub and its network cable.

**Steps:**

1. Download `cc0ac-weather-<date>.img.xz` from this project's
   [Releases page](https://github.com/akienm/CC0AC-Weather/releases/latest).
2. Install **Raspberry Pi Imager** from <https://www.raspberrypi.com/software/>
   and open it.
3. *Choose Device*: your Pi. *Choose OS*: scroll to the bottom, **Use custom**,
   and pick the file you downloaded. *Choose Storage*: the microSD card.
4. Press **Next**. When it asks about OS customisation, choose **Edit settings**:
   - Make up a username and password (you'll rarely need them).
   - Tick *Configure wireless LAN*: your Wi-Fi name, password and country.
   - Set your time zone.
   - Leave the hostname as it is (`cc0ac-weather`).

   Save, then **Yes** to write the card. It takes a few minutes.
5. Put the card in the Pi. Unplug the hub's cable from your router and plug it
   into the Pi's network port. Power the Pi on.
6. Wait about five minutes (the first start is the slowest), then on a computer
   or phone on your Wi-Fi open **<http://cc0ac-weather.local/>**. The setup
   page finds the hub and lists your sensors. Name them, paste in your
   location, set up a phone alert if you like, and press **Save**.

That's it: the dashboard is at the same address, and the charts are linked from it.

**If <http://cc0ac-weather.local/> doesn't open:** some Android phones and
older Windows PCs don't understand `.local` names. Look in your router's list
of connected devices for `cc0ac-weather` and open its address instead (for
example `http://192.168.1.37/`).

**If something goes wrong,** plug the hub's cable back into your router. The
hub works exactly as before; nothing on it was changed.

**Phone alerts.** The setup page can send a message to your phone (through the
free ntfy app) or an email when readings stop, and another when they're back.
To hear about the Pi itself losing power, add a free healthchecks.io check on
the same page.
It can also warn about the weather itself: frost, heat, and a falling
barometer (see [Weather alerts](#weather-alerts)).

## Other computers: one command

Every installer below sets up the same program and then hands it to a small
boot loader that starts it and keeps it patched
([UPDATES.md](UPDATES.md)). Each is safe to run again. When it finishes it
prints an address: open it and answer the setup page.

| Computer | Run this | Notes |
|---|---|---|
| **A Pi already running Raspberry Pi OS** | `curl -fsSL https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install.sh \| sudo bash` | Takes over the Pi's network port for the hub, unless the Pi itself is plugged into your router there (`--no-hub-network` to be sure) |
| **Debian or Ubuntu** | the same line | Leaves the network alone; `--hub-network` to give eth0 to the hub |
| **Mac** | the same line **without** `sudo` | Needs Python 3 from python.org or Homebrew; runs while you're logged in. Untested: no Mac to test on |
| **Windows 10/11** | in PowerShell: `irm https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install.ps1 \| iex` | Installs Python if it's missing; starts when you log on. Untested |
| **Docker** (anything) | `cd setup/docker && docker compose up -d` in a copy of this project | Readings and settings in the volume `cc0ac-weather-data`. Untested |

Only a Pi or a Debian machine with a spare network port can take the hub's
cable. Everything else uses the radio dongle, a console upload from another
brand, or the **internet's weather** (no station at all: `[online]` in the
settings, see [UPDATES.md](UPDATES.md)).

From a copy of the project, add `--source .` (Windows: `-Source .`) to install
that copy instead of downloading the newest release. The old
`setup/install-pi.sh` still works and runs `install.sh`.

## Looked after for you

- **Updates.** Setup asks one question: install security fixes and small
  repairs by itself, or leave it alone. Each new version is tried and rolled
  back if it doesn't work. [UPDATES.md](UPDATES.md) has the details.
- **Disks.** Once a day it asks each disk about its own health (SMART) and
  says so on the dashboard, and by phone alert, when one is failing.
- **Backup.** Once a week, if you name a folder or a Windows share
  (`\\server\share\folder`) in setup, it copies the settings and every reading
  there and keeps the newest 8.

**Want a helper?** If you use Claude Code or another AI coding assistant on the
Pi, point it at [SETUP-FOR-CLAUDE.md](SETUP-FOR-CLAUDE.md).

To build the card image yourself: `sudo setup/build-image.sh` on a 64-bit Pi.

## Setup with an Access hub, by hand

There are two ways to get the hub's readings to this program. Install the
program first; it is the same for both.

| | **A. Change the hub's server name** | **B. Plug the hub into the Pi** |
|---|---|---|
| Hardware | Any Linux box that stays on | A Raspberry Pi (or any Linux box) with a spare Ethernet port, on Wi-Fi |
| Hub settings | Changed to name your box | Left exactly as they came |
| Router | Forward port 443 to your box | Nothing (optionally a port for the dashboard) |
| If your box is down | The hub keeps uploading to Weather Underground; AcuRite's app stops | The hub is offline: no AcuRite, no Weather Underground |
| Setup effort | Five minutes | About half an hour |

**A** is the quick one. **B** is what the author runs: the hub never knows
anything changed, a factory reset of the hub doesn't undo it, and nothing on
the internet can reach the hub.

### Install the program

You need: Python 3.8+ and openssl (both come with Raspberry Pi OS).

```bash
git clone https://github.com/akienm/CC0AC-Weather ~/dev/src/CC0AC-Weather
mkdir -p ~/.cc0ac-weather
cp ~/dev/src/CC0AC-Weather/config.ini.example ~/.cc0ac-weather/config.ini
```

Edit `~/.cc0ac-weather/config.ini`: `write_path`, your sensor names under
`[sensors]` (and the order to show them in under `[sensor_order]`), `hub_id` under `[hub]` (the hub's MAC, from its local web page),
and `[web] enabled = true`. Then install the service:

```bash
sudo cp ~/dev/src/CC0AC-Weather/cc0ac-weather@.service /etc/systemd/system/
sudo systemctl enable --now cc0ac-weather@$USER
```

Find the hub's address on your router's client list (it shows up as
`W550-…` or similar) and open `http://HUB_IP/` in a browser. That page lists
the hub's ID (its MAC), every sensor ID it hears, and its current settings.

### A. Change the hub's server name

You also need a router that can forward port 443.

1. **Give the hub a hostname that reaches this box.** The hub accepts only a
   name, not an IP address. If your router can't serve local names, use your
   public hostname (your ISP's reverse-DNS name, or a free dynamic DNS name)
   and forward **port 443** on the router to this box.
2. **Change the hub's server setting.** Its web form is read-only in a
   browser, but the hub accepts a direct POST. Send *all five* fields, or its
   Weather Underground settings are blanked (read the current values from the
   hub's page first):

   ```bash
   curl -d 'ser=YOUR.HOST.NAME&id=WU_STATION_ID&ps=WU_PASSWORD&dev1=ATLAS_ID&ele=ELEVATION' \
        http://HUB_IP/config.cgi
   ```

   To undo, send the same with `ser=atlasapi.myacurite.com`.
3. Watch `journalctl -u cc0ac-weather@$USER -f`: within 5 minutes a line per
   sensor appears, and every raw request is kept in `~/.cc0ac-weather/hub-raw/`.

### B. Plug the hub into the Pi

The hub's Ethernet cable goes into the Pi instead of your router, and the Pi
reaches your network over Wi-Fi. The Pi then plays router for the hub. It gives
the hub an address, and when the hub asks where `atlasapi.myacurite.com` is,
the Pi answers "here". Every other name the hub looks up gets the real answer,
so its Weather Underground uploads (and its clock) pass straight through the
Pi to the internet. The hub's settings never change.

    sensors ──radio──▶ hub ──cable──▶ Pi eth0 (192.168.77.1)
                                       ├─ "atlasapi.myacurite.com" → this program → AcuRite
                                       └─ everything else (Weather Underground) → Wi-Fi → internet

Commands below are for Raspberry Pi OS (Debian 12/13, NetworkManager). The
example uses a private network, `192.168.77.0/24`, that is unlikely to clash
with yours; any private range works.

1. **Get the Pi on Wi-Fi first** (Raspberry Pi Imager can set it up), and
   install the program as above. Set `hub_id` in `config.ini`.

2. **Give the cable port a fixed address, and keep it from becoming the
   Pi's way to the internet:**

   ```bash
   sudo nmcli con add type ethernet ifname eth0 con-name hub \
        ipv4.method manual ipv4.addresses 192.168.77.1/24 \
        ipv4.never-default yes ipv6.method disabled
   ```

3. **Hand the hub an address, and answer AcuRite's name with the Pi.**
   `sudo apt install dnsmasq`, then `/etc/dnsmasq.d/cc0ac-hub.conf`:

   ```ini
   # DHCP and DNS for the hub, on the cable port only.
   interface=eth0
   except-interface=lo
   bind-interfaces
   dhcp-range=192.168.77.50,192.168.77.99,12h
   dhcp-option=option:router,192.168.77.1
   dhcp-option=option:dns-server,192.168.77.1
   # The hub's server name answers with this Pi; every other name passes through.
   address=/atlasapi.myacurite.com/192.168.77.1
   ```

   `sudo systemctl restart dnsmasq`. dnsmasq serves the cable port only. The
   Pi itself still asks your router for names, so when this program relays to
   AcuRite it reaches the real server, not itself.

4. **Let the hub out through the Pi.** Turn on forwarding:

   ```bash
   echo net.ipv4.ip_forward=1 | sudo tee /etc/sysctl.d/90-cc0ac-forward.conf
   sudo sysctl --system
   ```

   Then replace `/etc/nftables.conf` with this (it also closes the Pi to
   everything except what's listed), and `sudo systemctl enable --now nftables`:

   ```
   #!/usr/sbin/nft -f
   # wlan0 = your network. eth0 = the hub's cable (192.168.77.0/24).
   flush ruleset

   table inet cc0ac {
       chain input {
           type filter hook input priority filter; policy drop;
           iif lo accept
           ct state established,related accept
           ct state invalid drop
           meta l4proto { icmp, ipv6-icmp } accept
           tcp dport 22 accept comment "ssh"
           udp dport { 68, 546 } accept comment "the Pi's own DHCP replies"
           iifname "wlan0" tcp dport 12345 accept comment "dashboard"
           iifname "wlan0" udp dport 5353 accept comment "mDNS: yourpi.local"
           iifname "eth0" tcp dport 443 accept comment "the hub's uploads"
           iifname "eth0" udp dport { 53, 67 } accept comment "the hub's DNS and DHCP"
           iifname "eth0" tcp dport 53 accept
       }
       chain forward {
           type filter hook forward priority filter; policy drop;
           ct state established,related accept
           iifname "eth0" oifname "wlan0" meta nfproto ipv4 accept comment "the hub out to the internet"
       }
   }

   table ip cc0ac_nat {
       chain prerouting {
           type nat hook prerouting priority dstnat;
           # The hub sometimes ignores the DNS server DHCP gave it and asks 8.8.8.8,
           # then uploads straight to AcuRite. These send its DNS and its uploads
           # to the Pi whatever server or address it picked.
           iifname "eth0" udp dport 53 dnat to 192.168.77.1
           iifname "eth0" tcp dport 53 dnat to 192.168.77.1
           iifname "eth0" tcp dport 443 dnat to 192.168.77.1
       }
       chain postrouting {
           type nat hook postrouting priority srcnat;
           oifname "wlan0" masquerade
       }
   }
   ```

   Keep the `tcp dport 22` line or you lock yourself out of ssh.

5. **Move the cable.** Unplug the hub from your router and plug it into the
   Pi. If you had used method A before, put the hub's server name back to
   `atlasapi.myacurite.com` (all five fields, as in A step 2). Then check:

   ```bash
   journalctl -u dnsmasq -f            # the hub asks for and gets an address
   journalctl -u cc0ac-weather@$USER -f  # a line per sensor within 5 minutes
   ```

   The hub's own page is now at `http://192.168.77.5x/` from the Pi only
   (`curl` it there, or use an ssh tunnel). To undo everything, plug the hub
   back into your router.

To see the dashboard from outside your house, forward one port on your router
(for example 12345) to the Pi's Wi-Fi address, nothing else.

### If Weather Underground stops

The hub uploads to Weather Underground itself, over plain HTTP, whichever
method you use; this program never touches it. If wunderground.com stops
showing new readings while the dashboard is fine, the usual cause is the
password: it must be the **station key** shown under My Devices on
wunderground.com, not your account password (and not an older key from a
previous signup). A wrong key gets `401 unauthorized` back every few seconds.
Test a key without sending any data:

```bash
curl 'https://rtupdate.wunderground.com/weatherstation/updateweatherstation.php?ID=YOUR_STATION&PASSWORD=YOUR_KEY&dateutc=now&action=updateraw'
```

`success` means the key is right; then set it on the hub (all five fields, as
in A step 2, with `ser=atlasapi.myacurite.com` if you use method B).

### Things learned the hard way

Measured on firmware 051: the hub uses TLS 1.0/1.1 and
doesn't check the certificate (a self-signed one is generated for you); it
sends POST with everything in the query string; AcuRite rejects relayed
readings unless the hub's own `Atlas/<fw>` User-Agent is passed along; and
if the hub gets an error back it resends the same reading forever, so refusals
are answered locally.

### The dashboard

`http://THIS_BOX:12345/` (port set by `[web] port`; several comma-separated
ports allowed). To share it, forward that port on your router. Visitors from
outside your network get the dashboard and the charts and nothing else (see
[Settings in the browser](#settings-in-the-browser)). The
`[buttons]` section puts a row of buttons in a pane at the bottom, each loading
a page that allows framing (a radar map such as Weather Underground's WunderMap,
a forecast, a webcam): `buttonN = Label | URL`. Blank buttons are not shown, and
with none set there is no pane. The page remembers which button you chose. To
log in to a site inside the pane, allow third-party cookies for the dashboard's
address in your browser. Or add a third part, `Label | URL | Target=wu`, and the
button opens its page in a browser window of that name instead, where logging
in works as usual; pressing it again reuses that window. `Target=_blank` opens
a new tab every time.

**The pictures are emoji.** Every icon on the dashboard (sky, moon phase, wind,
the buttons) is an emoji character, not an image, so it looks like whatever
your phone, tablet or computer draws for that emoji. The same page looks a bit
different on an iPhone, an Android phone and Windows, and a very old device may
show an empty box where its fonts have no such emoji.

Set `[station] latitude` and `longitude` (and optionally `elevation_ft`) and a
Sun & moon card appears: sunrise, sunset, daylight and the moon phase, computed
in the page with no outside service. The outdoor card shows everything the
Atlas sends (feels-like, dew point, wind with direction arrow, gusts, rain, UV
with its level, light, lightning) plus the 3-hour pressure trend.

In the US, set `[forecast] enabled = true` (with `[station]` latitude and
longitude) and the program also fetches from the National Weather Service
(api.weather.gov, free, no key): an hourly strip for the next 24 hours (48 on
request) with sky, temperature, rain chance and wind; a 7-day list (tap a day
for the full forecast); visibility and cloud layers from the nearest airport on
the Sun & moon card; and a banner while any alert is active. Forecasts refresh
hourly, the airport every 20 minutes, alerts every 10. A failed fetch keeps
the last good copy and says so on the page.

### Charts

`📈 Charts` on the dashboard opens `charts.html`: every reading from
`weather.db` on one page — outdoor temperature (with dew point, feels-like and
the low–high band), wind speed, gusts and direction, pressure, rain, UV and
light, and temperature and humidity for every room. One row of buttons sets the
period for all of them: Today, Week, Month, Year, All time, or a Range of dates.
The period sits in the address (`charts.html#week`), so a view can be
bookmarked. Rain also shows totals for today, this week, month and year, and
since recording began.

The page asks `/history.json?period=week` (or `?from=YYYY-MM-DD&to=YYYY-MM-DD`)
and the server averages the readings into a few hundred points for whatever
the period is: 5-minute averages for a day, daily ones across years. The charts
use [uPlot](https://github.com/leeoniya/uPlot), loaded from jsDelivr.

### Records and normals

A Records card on the dashboard sets today against normal: the high and low
beside the 1991-2020 normals for the date, and the month's rain so far beside
the normal month-to-date. Below that come the station's records for this month,
this year and all time: hottest, coldest, highest gust, wettest day and the
pressure range, each with its date. Records count from the station's first
reading here, or from older readings brought in with `--import` (see
[Bringing in older readings](#bringing-in-older-readings)), and the card says
since when.

The normals need `[station] latitude` and `longitude`. In the US they are
NOAA's official 1991-2020 daily normals from the nearest weather station within
35 miles that has them (the card names it). Elsewhere, or with no such
station, they are averages of Open-Meteo's history for the exact spot over the
same 30 years. They are fetched once, kept in `normals.json` and refreshed
twice a year; `[climate] normals = false` keeps records only and fetches
nothing. Everything is rewritten hourly to `climate.json` and `climate.js`.

### Barometer outlook

The outdoor card has an **Outlook** row: a short forecast such as "Fine,
becoming less settled" worked out on the spot, with no internet, from the
barometer, its 3-hour trend, the wind direction and the season. This is the
Zambretti forecaster, a dial for barometers from the 1910s. It is roughly
right three times in four for the next 12 hours, and it is often less right
than a real forecast. It appears once there are 3 hours of pressure readings,
and south of the equator it reverses the wind and the seasons.

### Putting the dashboard in another page

`weather.html?embed` is the dashboard without the button pane and the charts
link, made to sit in an `<iframe>` on another page: a home page, a Home
Assistant dashboard (its Webpage card), a family wiki. Add `&panes=` to show
only some cards, from `outdoor`, `hourly`, `daily`, `sky`, `rooms` and
`climate`:

```html
<iframe src="http://THIS_BOX:12345/weather.html?embed&panes=outdoor,climate"
        width="420" height="640" style="border:0"></iframe>
```

A page on the internet can frame it only through your router's port forward
(see [The dashboard](#the-dashboard)), and then anyone can see what the
dashboard shows, room temperatures included; `panes=` hides cards, it does not
keep their readings private.

### Settings in the browser

`http://THIS_BOX:12345/config.cgi` shows config.ini in one large pane, with
Save and Revert. Save checks the text still reads as an ini file (a missing
section header, a repeated section or option, or a broken `%(name)s` is refused
with the line it's on), keeps the old file as `config.ini.previous`, writes the
new one and restarts the service, so a change takes effect at once. Revert
throws away edits you haven't saved.

The page answers only addresses on your own network and the box itself; there
is **no password yet**, so anyone on your network can open it, and it shows
every password in the file. From outside (through the router's port forward)
it, like everything but the dashboard and the charts, is a 404. Outside
visitors get `/`, `weather.html`, `charts.html`, `theme.css` and the data those
pages load (`current.js`, `forecast.js`, `/history.json`); your own pages,
`/current.json`, `/summaries.json` and `history.csv` are for your network only.
Use the box's own address (`http://10.0.0.x:12345/`), not your outside name: a
router that loops your outside name back to you makes you look like a visitor.

### Making it yours

**Name.** `[station] name = Hilltop Weather` puts your station's name in the
heading and the browser tab of every page.

**Colors.** All the colors and the font are in `theme.css`: background, cards,
text, the accent color, warnings, and one color per chart line. Make a folder
for your own files, name it in config.ini, copy the theme there and edit the
copy:

```bash
mkdir -p ~/.cc0ac-weather/pages
cp theme.css ~/.cc0ac-weather/pages/
```
```ini
[web]
pages = ~/.cc0ac-weather/pages
```

Restart the service, then edit as often as you like: the pages pick up a
change on reload. Anything in that folder with the same name as a shipped file
(`theme.css`, `weather.html`, `charts.html`) is served in its place, so you can
also copy a whole page there and change its layout. Updating the program never
touches the folder. A copied page does not get later fixes to the original, so
copy only what you mean to change.

**Pages of your own.** Any `.html`, `.css`, `.js` or image (`.png`, `.jpg`,
`.svg`, `.webp`, `.ico`) in that folder is served by its name. The server
serves nothing else: no folder listings, no subfolders, no other files.
`examples/my-page.html` is a short page to start from. It shows each sensor's
temperature and humidity and today's high, low and rain, in a few dozen lines of
JavaScript. Copy it into the folder and open `http://THIS_BOX:12345/my-page.html`.

A page gets everything from two addresses on the same server:

- **`/current.json`**: the latest reading from every sensor. Main fields:
  - `title` and `written`, the time of the last reading;
  - `station`: latitude, longitude, elevation_ft;
  - `pressure_change_3h`;
  - `sensors`: a list. Each sensor has `sensor_id`, `name`, `type` (`Atlas`,
    `tower`, …), `updated` and `fields`. Fields include `temp_f`,
    `humidity_pct`, `dew_point_f` and `battery`. The Atlas also sends
    `feels_like_f`, `wind_mph`, `wind_gust_mph`, `wind_dir_deg`,
    `pressure_inhg`, `rain_day_in`, `rain_hour_in`, `uv_index` and
    `light_lux`.

  `/forecast.json` holds the Weather Service data (`hourly`, `daily`,
  `observation`, `alerts`) when `[forecast]` is on. Both are also written as
  `.js` files, for pages opened from a file or a cloud folder.
- **`/history.json`**: readings averaged over a period.
  - Ask for `?period=today` (or `week`, `month`, `year`, `all`), or
    `?from=YYYY-MM-DD&to=YYYY-MM-DD`.
  - The answer has `t`, a list of times in Unix seconds, and lists of the same
    length under:
    - `outdoor`: `temp_f`, `temp_min`, `temp_max`, `dew_point_f`,
      `feels_like_f`, `humidity_pct`, `wind_mph`, `wind_gust_mph`,
      `wind_dir_deg`, `pressure_inhg`, `uv_index`, `light_lux`;
    - `rooms`: one entry per indoor sensor, with `name`, `temp_f` and
      `humidity_pct`.
  - A slot with no readings holds `null`.
  - `rain` is a separate list per hour, day or week (`t`, `in`).
  - `rain_totals` has `today`, `week`, `month`, `year`, `all` and `since`.
- **`/summaries.json`**: each sensor's days, weeks, months and years.
  - `sensors` lists each sensor (in display order) with `id`, `name`, `type`,
    `outdoor`, and lists `days` (each with `day`), and `weeks`, `months` and
    `years` (each with `start`, and `days`: how many days it has readings for).
  - Every entry has `readings`, `temp_min`, `temp_max`, `temp_avg`,
    `humidity_min`, `humidity_max`, `humidity_avg`, `dew_point_avg`,
    `feels_like_min`, `feels_like_max`, `wind_avg`, `wind_gust_max`, `rain_in`,
    `pressure_min`, `pressure_max`, `pressure_avg`, `uv_max` and `light_max`.
    A longer period's average is the average of its days'; its rain, their sum.
  - The latest 400 days are listed; ask `?days=N` for more or fewer. Today is
    included, up to the latest reading. Weeks start on Sunday.
  - Finished days are kept in the database's `daily` table.

## MQTT and Home Assistant

With `[mqtt] enabled = true` and a broker's `host` (and `username` and
`password` if it wants them), every reading is published, one topic per field,
retained:

```
cc0ac-weather/00001234/temp_f        61.2
cc0ac-weather/00001234/wind_mph      3.4
cc0ac-weather/00005678/humidity_pct  44
```

Home Assistant's MQTT integration finds the sensors by itself (MQTT
discovery): each one appears as a device named as in `[sensors]`, with an
entity per field in the right units (Home Assistant converts them for a metric
house). Batteries show as battery problem sensors, on when low. A sensor not
heard for `stale_after_minutes` (60) shows as unavailable rather than its last
value. Set `discovery = false` to publish only the values. Plain MQTT 3.1.1 on
your own network; there is no TLS.

Checked 2026-10-21 against Home Assistant 2026.10 and Mosquitto 2.0: every
sensor appeared with its entities, units and classes, and no warnings.

**Apple Home, Google Home, Alexa:** through Home Assistant. Its HomeKit Bridge
integration puts the sensors in Apple's Home app, and its Google Assistant and
Alexa integrations (or Home Assistant Cloud) do the same for theirs. openHAB,
Node-RED and Homebridge can read the MQTT topics directly.

## Asking an AI assistant (MCP)

The station speaks MCP, the Model Context Protocol, at `/mcp` on the dashboard's
port. An AI assistant connected to it can read the station when you ask it
things like "how cold did it get last night?", "is this October wetter than
usual?" or "will I need a coat this afternoon?". Claude Code:

    claude mcp add --transport http weather http://THIS_BOX:12345/mcp

Any other program that speaks MCP over HTTP ("Streamable HTTP") takes the same
address. Claude Desktop runs its MCP servers as programs, so give it the
`mcp-remote` bridge in its `claude_desktop_config.json` (needs Node.js):

```json
{ "mcpServers": { "weather": { "command": "npx",
    "args": ["mcp-remote", "http://THIS_BOX:12345/mcp", "--allow-http"] } } }
```

The assistant gets seven tools:

| Tool | Reads |
|---|---|
| `current_conditions` | every sensor's latest readings, the 3-hour pressure change and the barometer outlook |
| `list_sensors` | the sensors: id, name, type, outdoor or not, when last heard |
| `forecast` | the forecast, alerts and nearest-airport observation, as on the dashboard |
| `outlook` | the barometer outlook, and how it is worked out |
| `records_and_normals` | today against normal, and the records, as on the Records card |
| `history` | outdoor readings over a period or between two dates, plus room temperatures and rain totals |
| `daily_summary` | per sensor, per day (or week, month, year): lows, highs, rain, gusts, pressure |

It can only read: nothing an assistant asks for can change a setting or a
reading. Like the settings page it answers only your own network, and a web
page from another site cannot use your browser to read it.
`[web] mcp = false` turns it off. An assistant that connects from the cloud
(claude.ai's connectors, ChatGPT's) cannot reach a box on your home network,
and is refused even through a port forward; one running on a computer in the
house can.

**Status:** checked 2026-10-21 by a test client over the network against this
station's own data: the handshake, the tool list, and every tool answering.
Not yet measured from Claude Desktop through mcp-remote.

## Weather networks

Besides Weather Underground, readings can go to four more networks, each
switched on in its own section of `config.ini` with the ID and key the network
gives you:

| Section | Network | Every |
|---|---|---|
| `[windy]` | [Windy](https://stations.windy.com) | 5 min |
| `[pwsweather]` | [PWSweather](https://www.pwsweather.com) | 5 min |
| `[weathercloud]` | [Weathercloud](https://weathercloud.net) | 10 min |
| `[cwop]` | [CWOP](https://weather.gladstonefamily.net/cwop_signup.html), which feeds the US National Weather Service | 10 min |

Each uploads one station: `sensor = <ID>` in its section, or else the first
outdoor sensor heard. Readings from the hub and from a radio both count;
internet weather never does. CWOP needs `[station] latitude` and `longitude`.
A failed upload is logged with the network's reason.

**Status:** the formats were checked 2026-10-21 against each network's live
endpoint (each answers a wrong key with its own refusal) and, for CWOP, by an
APRS parser; an accepted upload with a real account is not yet measured.

## Weather alerts

Besides telling you when readings stop, the station can warn about the
weather, in the same ways (ntfy, email): one message when it starts and one
when it's over. In `[alerts]`:

```ini
weather = frost, heat, storm
frost_f = 35              ; outdoors at or below this
heat_f = 105              ; feels-like at or above this
pressure_drop_inhg = 0.06 ; the barometer falls this much in 3 hours
```

The station's own outdoor sensor is used when it has been heard lately, and an
internet one (`[online]`) only when it hasn't. Each alert has a little room
before it says "over" (2° for frost, 3° for heat, half the drop for a storm),
so a reading hovering at the line doesn't send a message every few minutes.
Checked on the same 5-minute timer as the readings-stopped alert.

## Bringing in older readings

Readings from before this station can be brought into `weather.db`, so the
charts and records reach back further:

```
python3 acurite-capture.py --import ~/weewx.sdb           # a WeeWX database
python3 acurite-capture.py --import export.csv            # a CSV file
python3 acurite-capture.py --import wu --wu-station KXXMYTOWN12 \
        --wu-api-key YOUR_KEY --from 2023-01-01           # Weather Underground
```

- **WeeWX:** its SQLite database, read without changing it, in any of WeeWX's
  unit systems.
- **CSV:** this program's own `history.csv` (each row keeps its sensor), a
  Weather Underground table export, or most station software's exports. Columns
  are recognised by their names (Temperature, Humidity, Wind Speed, Pressure,
  Dewpoint and the like, with units such as "°F" in the cells allowed). Add
  `--metric` when the numbers are °C, km/h, hPa and mm.
- **Weather Underground:** your station's daily summaries, one day per request,
  with the API key from your Weather Underground account (Member Settings → API
  Keys). Give `--from` and optionally `--to` (default yesterday).

Everything but your own `history.csv` goes to one sensor: `--sensor <ID>`, or
else the first outdoor sensor heard here. Only readings from before that sensor's
first reading of its own are taken, so nothing it measured is overwritten, and
running the same import twice adds nothing the second time. Impossible values
(a 150% humidity, a -9999 placeholder) are dropped. The charts show the new
readings at once; the Records card counts them within the hour.

**Status:** each format is checked by the tests on small sample files; not yet
measured on a real WeeWX database or a real Weather Underground account.

## Radio capture (no hub)

With an RTL-SDR USB radio dongle and [rtl_433](https://github.com/merbanan/rtl_433),
the program hears the sensors itself, no hub needed:

```bash
sudo apt install rtl-sdr rtl-433
sudo usermod -aG plugdev $USER          # log out and back in
rtl_test -t                             # the dongle is found and tuned
python3 acurite-capture.py --discover   # lists sensor IDs in range
```

Name the sensors you want under `[sensors]` (neighbours' are heard too) and
set `[capture] enabled = true`. AcuRite sensors get the same IDs and names the
hub gives them, so a hub and a dongle can share one `[sensors]` list. Dew
point, heat index, wind chill and feels-like are worked out from what the
sensors send, and rain today and in the last hour from the rain gauge's
running total, as the hub does. `[weather_underground] enabled = true` with a
station key uploads readings heard by radio (a hub already uploads its own).

**The RTL-SDR Blog V4 dongle** needs a newer driver than some distributions
ship: if `rtl_test` doesn't find its R828D tuner, install the driver from
[rtl-sdr-blog](https://github.com/rtlsdrblog/rtl-sdr-blog) (see their V4
quick-start guide).

**Status:** running on the air since 2026-10-14 with an RTL-SDR Blog V4 and
AcuRite Atlas and tower sensors (stock rtl_433 25.02 found the V4). Other
brands' sensors work the same way with the right `frequency` and `protocols`;
see [SUPPORTED.md](SUPPORTED.md). Every line the radio hears is kept for 14
days in `~/.cc0ac-weather/radio-raw/`.

**Other brands by radio.** Ambient Weather, Ecowitt and Fine Offset gear sold
in North America transmits on 915 MHz, not AcuRite's 433.92. Set the band and
the decoders in `[capture]`:

```ini
[capture]
enabled = true
frequency = 915M          # 868M in Europe; 433.92M for AcuRite and older sensors
protocols = 78, 113, 213  # or "all", then --discover to see what's near
# frequency = 433.92M, 915M   # two bands on one dongle...
# hop_seconds = 30            # ...switching every 30 s, missing the other meanwhile
```

Sensors that send wind, rain, UV, light or lightning get an outdoor card on
the dashboard; the rest get a room card. One dongle hears one band at a time,
so for AcuRite and Ambient gear together a second dongle beats hopping.

What we've measured (2026-10-16): 13 real radio recordings from the rtl_433
project went through rtl_433 25.02 and this program on a Pi and decoded right,
among them the Ambient F007TH, WH31E and WH31B, the Ecowitt WH40, the Fine
Offset WH24, WH1050 and WS90, and the Bresser 5-in-1. Recorded rtl_433 output
for 205 weather models goes through our decoder; [SUPPORTED.md](SUPPORTED.md)
lists each with what we read from it. All 34 recorded AcuRite lines decode the
same as before this change.

## Other brands' consoles (no radio)

Ambient Weather, Ecowitt and Fine Offset consoles and gateways can upload to a
"custom server" as well as their own cloud. This program listens along: the
console keeps sending to Ambient's or Ecowitt's cloud exactly as before, and a
copy comes here. No radio and no hub needed.

1. In config.ini:

   ```ini
   [console]
   enabled = true
   port = 12346
   # station = 00:11:22:33:44:55   # optional: only record this console (MAC, passkey or WU ID)
   ```

   then restart the service (`sudo systemctl restart cc0ac-weather@$USER`).
   The ready-made Pi card already opens port 12346 in its firewall; on your own
   machine, open it yourself.

2. Point the console at this machine:

   | Brand | Where | Settings |
   |---|---|---|
   | Ambient Weather (WS-2902, WS-2000, WS-5000, their WiFi hubs) | awnet app → your device → Customized | Server: this machine's IP address. Port: 12346. Protocol: Ambient Weather. Path: `/data/report/?` (it must end in `?`). Interval: 60 |
   | Ecowitt (GW1000, GW1100, GW2000, HP2551, ...) | WS View Plus app, or the gateway's web page → Weather Services → Customized | Protocol: Ecowitt. Server: this machine's IP. Port: 12346. Path: `/data/report/`. Interval: 60 |
   | Fine Offset relabels (Misol, Froggit, Sainlogic, ...) | as Ecowitt | Ecowitt or Wunderground |
   | Anything that uploads in Weather Underground format | its own settings | Wunderground; any station ID and key |

3. Within a minute or two the dashboard shows `console-outdoor`,
   `console-indoor`, and `console-ch1` to `console-ch8` for extra room
   sensors. Name them under `[sensors]` like any other.

Every upload is kept in `~/.cc0ac-weather/console-raw/` (a Weather Underground
password shows as ****), and any field the program doesn't read yet is named
once in the service log, so a new console's oddities show up on the first day.

**Status: untested on real hardware.** The field names come from the published
formats and from other open-source receivers, and the tests feed it sample
uploads in all three formats. If you try it, please send a `--diagnostics` file
(below), working or not. The ready-made Pi card's setup page still assumes an
AcuRite hub, so for now a console-only setup is config.ini by hand.

## When something reads wrong

    python3 acurite-capture.py --diagnostics

writes one file holding the raw uploads and radio lines of the last three
days, the service log, and the radio and console settings. Passwords, station
keys, MACs, public IP addresses, sensor names and your location are left out.
Attach it to an [issue](https://github.com/akienm/CC0AC-Weather/issues).

## Wall displays: old Android phones as weather kiosks

`kiosk/` turns a spare, freshly reset Android phone into a wall display that shows
only your weather page. It has no lock screen and no way out, goes dark when idle,
and wakes at a touch. One command sets it up over USB:

    kiosk/setup-phone.sh --url http://my-pi.local:12345/

See [kiosk/README.md](kiosk/README.md) for the options and how to build it.

## Files

| File | Purpose |
|---|---|
| `acurite-capture.py` | The program: recording, database, web server, forecast; runs the sources and outputs |
| `sources/__init__.py` | The Reading every source produces, its fields and units, and how to add a source |
| `sources/acurite_access.py` | The Access hub source: listener, relay to AcuRite, raw log |
| `sources/rtl_433.py` | The radio source: runs rtl_433 and turns its messages into Readings |
| `sources/console_upload.py` | Console uploads: Ambient Weather, Ecowitt and Wunderground formats |
| `diagnostics.py` | `--diagnostics`: one file to attach to a problem report |
| `SUPPORTED.md` | Which brands work how, and how sure we are (made by `setup/supported.py`) |
| `tests/` | `python3 -m unittest discover tests`; `rtl_433_samples.jsonl` is one recorded line per radio model |
| `outputs/__init__.py` | How to add an output: somewhere readings are sent |
| `outputs/weather_underground.py` | Uploads readings heard by radio to Weather Underground |
| `outputs/mqtt.py` | Publishes readings to an MQTT broker, with Home Assistant discovery |
| `outputs/_station.py` | What the weather-network outputs share: the station, its latest values, how often |
| `outputs/windy.py` | Uploads the station to Windy |
| `outputs/pwsweather.py` | Uploads the station to PWSweather |
| `outputs/weathercloud.py` | Uploads the station to Weathercloud |
| `outputs/cwop.py` | Sends the station to CWOP as an APRS weather packet |
| `config.ini.example` | Configuration template, every option documented |
| `weather.html` | Dashboard; reads `current.js`, `forecast.js` and `climate.js` |
| `charts.html` | Charts over any period; reads `/history.json` |
| `config.html` | The settings page served at `/config.cgi` (your network only) |
| `setup.html` | The first-run setup page at `/setup` (your network only) |
| `wizard.py` | Finds the hub, reads its page, writes the setup page's answers into the settings |
| `alerts.py` | Sends a message when readings stop and when they're back, and weather alerts (`--check`, `--test-alert`) |
| `climate.py` | Records and the 1991-2020 normals: `climate.json` and the dashboard's Records card |
| `zambretti.py` | The barometer outlook on the outdoor card |
| `mcp_server.py` | The read-only MCP server at `/mcp`, for AI assistants |
| `importer.py` | `--import`: older readings from WeeWX, CSV files or Weather Underground |
| `setup/install.sh` | Installs on a Pi, Debian, Ubuntu or a Mac (`install-pi.sh` is the old name and runs it) |
| `setup/install.ps1` | Installs on Windows |
| `setup/docker/` | The Docker image and `compose.yaml` |
| `updates/bootloader.py` | The boot loader: installs, starts the station, updates and rolls back ([UPDATES.md](UPDATES.md)) |
| `updates/migrations/` | The install steps, numbered, each for the platforms it names |
| `upkeep.py` | The daily disk check and the weekly backup |
| `setup/build-image.sh` | Builds the ready-made SD card image |
| `SETUP-FOR-CLAUDE.md` | Setup notes for an AI assistant helping someone install it |
| `theme.css` | Colors and font for every page |
| `examples/my-page.html` | A short page of your own to start from |
| `cc0ac-weather@.service` | systemd unit |

## License

MIT, see `LICENSE`.
