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
                                              ├─ current.json / current.js / history.csv
                                              ├─ relays the reading to AcuRite (optional; their app keeps working)
                                              └─ web server ──▶ weather.html dashboard
    The hub uploads to Weather Underground itself; that is untouched.

- One burst every 5 minutes, one request per sensor (Atlas, 5-in-1, Iris, towers, …).
- `current.js` loads with a plain `<script>` tag, so the dashboard also works
  opened as a file or from any static host — no web server required to view it.
- `history.csv` keeps every reading, for charts (coming).

No hub? The older path still works: an RTL-SDR dongle and rtl_433 receive the
sensors directly (see *Radio capture* below).

## Setup with an Access hub

You need: a Linux box that stays on (a Raspberry Pi is plenty), Python 3.8+,
openssl, and a router that can forward port 443.

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

### Point the hub at it

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

Things learned the hard way (firmware 051): the hub uses TLS 1.0/1.1 and
doesn't check the certificate (a self-signed one is generated for you); it
sends POST with everything in the query string; AcuRite rejects relayed
readings unless the hub's own `Atlas/<fw>` User-Agent is passed along; and
if the hub gets an error back it resends the same reading forever, so refusals
are answered locally.

### The dashboard

`http://THIS_BOX:12345/` (port set by `[web] port`; several comma-separated
ports allowed). To share it, forward that port on your router. Set
`[weather_underground] station_id` and a Weather Underground pane (forecast,
history) appears at the bottom; leave it unset and there is none.

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
| `cc0ac-weather@.service` | systemd unit |

## License

MIT, see `LICENSE`.
