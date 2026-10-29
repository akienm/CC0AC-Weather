# Last version for a while (draft)

A draft for Akien to edit before posting. It is not posted yet.

Attach: `dashboard.png`, `setup.png`, `status.png`, `charts.png`, `phone.png`.

---

**CC0AC-Weather 1.3.20: probably the last version for a while**

A quick update on the free, open source replacement for the AcuRite web page and
app. It's settled down, and this looks like the last version for a while unless
something breaks or somebody needs something.

Since the last post it has picked up:

- **A setup page** that finds the hub and lets you name your sensors, set your
  location, alerts, updates and backups, all from the browser.
- **A status page** showing every sensor's battery and reception, every receiver,
  every service it sends to, and the raw logs.
- **Low-battery warnings** for each sensor and for the hub itself (which
  AcuRite's own app never mentioned), on the dashboard and by phone alert.
- **Your units**: US, metric, UK, or any mix. Plus calibration for a sensor that
  reads a little off.
- **Records against the 1991-2020 normals**, degree days, evapotranspiration, and
  NOAA-style monthly and yearly climate summaries.
- **Weather alerts** by phone for frost, heat, lightning and a falling barometer.
- **Uploads** to Weather Underground (unchanged), plus Windy, PWSweather,
  Weathercloud and CWOP, and MQTT/Home Assistant, a webhook and InfluxDB.
- **https** on the same port with a free certificate, if you want to see it from
  outside the house.
- **Installers** for Linux, Mac, Windows and Docker, alongside the ready-made Pi
  card.

The README now starts with pictures and a feature list, newest first, with the
version that added each thing. SUPPORTED.md lists every station, sensor, console
and service it knows how to talk to, and how sure we are of each. Only AcuRite
is tested on real hardware so far; anything marked with a \* is written but not
yet tested.

Pictures are real readings with a made-up name, rooms and location.

https://github.com/akienm/CC0AC-Weather

Same as before: I wrote the requirements and did the testing, Claude wrote the
code. Bug reports and pull requests are welcome.
