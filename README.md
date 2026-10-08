# CC0AC-Weather

Keep your AcuRite weather station working on your own terms: your own
dashboard, your own data files, Weather Underground uploads unchanged, and no
dependence on AcuRite's cloud. Built by Claude Code ("cc.0") with Akien
MacIain; contributions welcome.

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

## Setup with an Access hub

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
`[sensors]`, `hub_id` under `[hub]` (the hub's MAC, from its local web page),
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
ports allowed). To share it, forward that port on your router. The
`[buttons]` section puts a row of buttons in a pane at the bottom, each loading
a page that allows framing (a radar map such as Weather Underground's WunderMap,
a forecast, a webcam): `buttonN = Label | URL`. Blank buttons are not shown, and
with none set there is no pane. The page remembers which button you chose. To
log in to a site inside the pane, allow third-party cookies for the dashboard's
address in your browser. Or add a third part, `Label | URL | Target=wu`, and the
button opens its page in a browser window of that name instead, where logging
in works as usual; pressing it again reuses that window. `Target=_blank` opens
a new tab every time.

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

## Radio capture (no hub)

With an RTL-SDR dongle (~$25) and [rtl_433](https://github.com/merbanan/rtl_433):

```bash
sudo apt install rtl-sdr rtl-433
sudo usermod -aG plugdev $USER          # log out and back in
python3 acurite-capture.py --discover   # lists sensor IDs in range
```

Then set `[capture] enabled = true` in config.ini. `[weather_underground]
enabled = true` with a station key uploads to WU from this path (a hub
already does that itself).

## Files

| File | Purpose |
|---|---|
| `acurite-capture.py` | The program: hub listener and relay, web server, optional radio capture |
| `config.ini.example` | Configuration template, every option documented |
| `weather.html` | Dashboard; reads `current.js` and `forecast.js` |
| `charts.html` | Charts over any period; reads `/history.json` |
| `theme.css` | Colors and font for every page |
| `examples/my-page.html` | A short page of your own to start from |
| `cc0ac-weather@.service` | systemd unit |

## License

MIT, see `LICENSE`.
