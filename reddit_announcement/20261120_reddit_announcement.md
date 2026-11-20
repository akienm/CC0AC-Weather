# Reddit announcement, 2026-11-20 (draft)

A draft for Akien to edit before posting. It is not posted yet. It is written for people who have
never heard of the project; what's new since the October posts comes after that.

Attach, in this order: `dashboard.png`, `kindle.png`, `phone.png`, `charts.png`, `status.png`,
`setup.png`. All of them show real readings with a made-up station name, rooms and location.

Where: the same four subreddits as before (see [README.md](README.md)), a day or two apart. For
r/raspberry_pi, lead with the Pi card. For r/selfhosted, keep the AI line; its rules ask for one.

---

**Title:** Free, open-source home for your weather station: your own dashboard and charts, your
data on your own computer, no subscription. Now it runs a jailbroken Kindle as a weather screen,
too.

**Body:**

**What it is.** CC0AC-Weather is a free (MIT-licensed) program that runs on a computer in your
house, often a Raspberry Pi, and becomes the home for your weather station. You get a dashboard,
charts and records in a web browser, on your phone, or on an old Kindle. Every reading is kept on
your own disk for as long as you like. There's no account, no subscription, and nothing to sign up
for.

**Who it's for:**

- **AcuRite owners** with an Access hub (Atlas, Iris, 5-in-1 and the indoor sensors). AcuRite is
  moving its app and web page to a subscription. This keeps the hub working your way, and the
  AcuRite app and Weather Underground uploads can keep working too.
- **Anyone whose sensors talk on 433/915 MHz radio** (Ambient, Ecowitt and its relabels, Bresser,
  La Crosse and many more). A ~$30 RTL-SDR USB radio listens to them directly, with no hub at all.
  *Honest note: only AcuRite is tested on real hardware so far; the others are tested in
  simulation. If you have one, I'd love to hear how it goes.*
- **People with no station at all.** It can run on the National Weather Service and Open-Meteo
  alone.

**What you get:**

- A dashboard of every sensor, with feels-like, wind, the 3-hour barometer trend and a barometer
  forecast that works offline, sun and moon, and the NWS hourly and 7-day forecast and alerts.
- Charts from today back to all time, rain totals, records against the 1991–2020 normals,
  NOAA-style monthly and yearly climate summaries, degree days and evapotranspiration.
- Alerts by phone (ntfy) or email for frost, heat, lightning, a falling barometer, a low battery or
  a sensor that goes quiet. AcuRite's app never told me about a low battery.
- Uploads, if you want them, to Weather Underground, Windy, PWSweather, Weathercloud and CWOP. It
  also works with MQTT and Home Assistant, a webhook, InfluxDB, and an MCP server so you can ask an
  AI assistant about your weather.
- A setup page in the browser, so you don't edit config files: it finds the hub, and you name your
  sensors, set your location, units, alerts and backups.
- It looks after itself: automatic backups (to a folder, a network share, SFTP or FTP), optional
  automatic updates that roll themselves back if something goes wrong, and disk-health checks.

**Getting it running:** a ready-made Raspberry Pi card, one command on Linux, macOS or Windows, or
Docker. The README starts with pictures and the list above.

**New since the October posts (1.3.18 → 1.5.2):**

- **An old Kindle as an e-ink weather screen.** A jailbroken Kindle draws the readings, the forecast
  and every room by itself, redrawn every 5 minutes, so the station does no extra work. Touch it to
  go back to your books. (Picture attached. The jailbreak is the hard part, and kindlemodding.org
  documents it well; we link there.)
- **An Android app**: a home-screen widget for your phone, and a kiosk mode that turns an old phone
  into a wall display.
- **Plug in a USB radio and tick a box** on the setup page: RTL-SDR support with no command line.
- **Air quality and pollen**, kept and charted over time.
- **Lightning**: a strikes-by-distance chart, one alert per storm, and far-off strikes left out.
- **An alert rules table**: decide which alerts go to your phone, which go to email, which only
  show on the dashboard, and how long a banner stays.
- **An Update now button** on the setup page, and daily, weekly and monthly email reports.
- **Private values** (passwords, keys) move by themselves into their own file, out of the settings
  file.
- **Backups over SFTP and FTP**, beside folders and network shares.
- **Tested by someone besides me**: the first outside station keeper runs it in Docker with an
  Access hub, and it updated itself across 1.4 to 1.5 without a hitch. Their bug reports became
  1.5.1 and 1.5.2 within a day.

**Other good options:** [Acuparse](https://github.com/acuparse/acuparse) and
[zasder](https://github.com/volneydouglas/zasder-weather-backend) do similar things, each a little
differently. Worth a look before you pick.

**How it was built:** I wrote the requirements, did the hardware work, and tested it. Claude Code
(AI) wrote the code. Bug reports, "does it work with my station?" reports and pull requests are all
welcome.

https://github.com/akienm/CC0AC-Weather

Not affiliated with AcuRite or any other brand named here.
