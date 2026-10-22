# How CC0AC-Weather installs and keeps itself up to date (1.3.1)

This station is an appliance, like a fridge: once it works it should just keep
working without anyone looking after it. This page describes the small program
that installs it and, if you let it, keeps it patched.

## The one question

Setup asks one thing about updates:

- **Yes** (the default): it installs security fixes and small repairs by itself.
- **No**: it's an appliance, so leave it alone. Nothing is ever downloaded.

You can change the answer later on the settings page (`/config`, `[updates]`).

## Versions

A version number looks like **`1.2.0/1`**: `<major>.<minor>.<revision>/<security revision>`.

- The **revision** (`1.2.0` → `1.2.1`) is a small repair.
- The **minor** version (`1.2` → `1.3`) is a bigger change. The new version is
  installed beside the old one, and your `config.ini` and readings are kept.
- The **security revision** (`/1` → `/2`) is numbered on its own. A security fix
  is the smallest change that closes the hole: it adds or overlays files and
  never removes anything, so the same `/N` fix applies to every version still in
  use. When a fix can't be made that small, because it has to remove things or
  change the code a lot, it ships as a new minor version that carries **only
  that fix**. New features come in a later release.

Releases are git tags: `v1.2.0` is a version, and `s2` is security revision 2.
The update program reads the list of tags from GitHub and downloads a release as
a plain archive, so it doesn't need git.

1.2.0 added this installer and internet weather (below); 1.2.1, shipped before
any station had installed 1.2.0, added Home Assistant and four weather networks.
1.3.0 added weather alerts, records and normals, the barometer outlook, an
embeddable dashboard, an MCP server for AI assistants, and `--import` for older
readings. A house that answered Yes takes it at its weekly check.

1.3.1 is a repair: after a restart the barometer outlook stayed blank until it
had three new hours of readings. It also shows the version at the foot of the
dashboard.

## When it checks

At install the station picks a time to check, 06:00 unless you choose another,
and adds a few random minutes. It also picks one random day of the week. Both
are kept, so a house's updates always come at the same time, and not every
house asks GitHub at the same moment.

- **Security fixes**: checked every day at that time.
- **Repairs and new versions**: checked once a week, at that time on that day.

With **No**, it never checks.

## The boot loader

Every way of installing (the Pi image, the bash one-liner on Debian or macOS,
the Windows PowerShell line, the Docker image) starts the same small program,
`updates/bootloader.py`. It uses only the Python standard library, and does
this:

1. It downloads the newest release into `versions/<version>/`, beside any older
   ones.
2. It runs that release's **migrations**: numbered steps in `updates/migrations/`,
   such as writing the service file, setting up the hub's network port, or
   hardening the Pi's firewall. A fresh install runs them all, starting from
   `0001`. An update runs only the ones it hasn't run before. Each step is safe
   to run twice, and each one says which platforms it is for (`pi`, `debian`,
   `docker`, `windows`, `macos`), so a step for the Pi never runs on Windows.
3. It starts the station program from the chosen version, and restarts it if it
   stops.
4. It runs the reading-stopped check (`--check`) every 5 minutes, from outside
   the station program, so the check still happens if the station has died.

The service that the operating system starts (systemd, launchd, a Windows
scheduled task, or the container) is always the boot loader. It is never the
station program directly. That way updating, health-checking and rolling back
work the same way on every platform.

## Health check and rollback

After it switches to a new version, the boot loader waits up to 15 minutes for
all three of these:

- the station program is running;
- the dashboard answers;
- a new reading has arrived from at least one sensor that was reporting
  **before** the update.

If any of them fails, it switches back to the previous version, writes down why
in `updates.log` (which the diagnostics bundle includes), and tries again at the
next scheduled check.

Rolling back is just switching the program folder back, because migrations only
**add**, and only in ways the previous version doesn't mind. A migration that
can't be made harmless is marked `NO_ROLLBACK`, and the boot loader won't roll
back past it.

## Platforms

| Platform | How to install | What runs it | Tested |
|---|---|---|---|
| Raspberry Pi (image) | flash the image | systemd | not yet with 1.2.0 |
| Raspberry Pi / Debian / Ubuntu | `curl … setup/install.sh \| sudo bash` | systemd | not yet with 1.2.0 |
| Docker | `docker compose up -d` in `setup/docker/` | the container | not yet |
| Windows 10/11 | `irm … setup/install.ps1 \| iex` in PowerShell | a scheduled task at logon | not yet |
| macOS | `curl … setup/install.sh \| bash` (no sudo) | launchd | **no Mac to test on** |

"Not yet with 1.2.0": the station itself has run on a Pi every day since 4 October 2026, and the
boot loader's install, update and rollback have run in a test folder, but the
installers themselves have not yet run end to end on a fresh machine.

Only the Pi and Debian can take the AcuRite hub's cable. For that, the Pi
becomes the hub's network: it runs DHCP and DNS for the hub, and passes the
hub's other traffic out to the internet. Every platform can use the radio
dongle (rtl_433), console uploads (Ambient, Ecowitt and others), and the
internet's weather.

**macOS:** you can't make a signed, notarized `.app` or `.pkg` on Linux. That
needs a Mac, Xcode and a paid Apple developer account. The station doesn't need
one, though, because it is a Python program and a background service. The bash
installer and a launchd entry do the whole job on a Mac, and Docker Desktop
works on Macs too. Both stay marked untested until someone runs them on a Mac.

**Libraries:** the station uses only the Python standard library, so there is
nothing to pin there. The Docker image pins its Python version.

## Internet weather: a station with no station

`[online]` adds readings from the internet, so a spare phone or tablet with no
weather gear at all becomes a weather display:

- in the US: the National Weather Service's nearest observing station
  (api.weather.gov);
- anywhere: Open-Meteo's current conditions for your exact spot (open-meteo.com,
  free, no key). These are a weather model's estimate, not a measurement.

`source = auto` picks the Weather Service when it covers the location, and
Open-Meteo when it doesn't. The forecast works the same way (`[forecast] source`),
so outside the US the hourly and 7-day cards come from Open-Meteo. Open-Meteo has
no alerts, so the alert banner shows only in the US.

## Looking after the computer: disks and a backup

The boot loader also runs `upkeep.py` once a day, half an hour after the update
time, whether or not updates are on:

- **Disks, every day.** `smartctl` (smartmontools) asks each disk about its own
  health. A disk that says it is failing, or has sectors moved to spares,
  waiting to be moved, or unreadable (SMART 5, 197 and 198), or for NVMe a
  critical warning or media errors, is a problem. An SD card has no SMART, so a
  Pi on one reports nothing. Runs as root, because reading a disk's SMART needs
  it; the result is `disks.json` beside the boot loader.
- **Backup, once a week**, on the station's day, if `[backup] path` is set:
  `config.ini`, a consistent copy of the readings database taken while the
  station runs, and `history.csv`, in one file named
  `cc0ac-weather-<computer>-<date>.tar.gz`. The newest `keep` (8) of this
  computer's are kept and its older ones deleted; nothing else in the folder is
  touched. The path is a folder, or a Windows share (`\\server\share\folder`):
  on Linux and macOS the share is reached with `smbclient` using `[backup] user`
  and `password`, which go to smbclient in a file only the station can read,
  never on its command line; on Windows the share is used directly. The setup
  page has a **Copy now** button.

A problem shows as a line on the dashboard and goes out as one message by the
same alerts as "no readings" ("needs looking after"), and one more when it is
gone ("looked after"). By hand: `bootloader.py disks`, `bootloader.py backup`.
