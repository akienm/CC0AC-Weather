# Setting up CC0AC-Weather: notes for an AI assistant

You are helping someone who is probably not a programmer set up a weather
station recorder. Keep your messages short and plain, do the work yourself, and
ask them only for what you cannot find out (their Wi-Fi is already working if
you are running on the Pi).

## The goal

An AcuRite Access hub (model 09155M, or a smartHUB) reports its sensors' readings
to AcuRite's server over HTTPS. This program receives those readings instead,
keeps them, relays each one on to AcuRite unchanged, and serves a dashboard.

The supported setup ("method B" in README.md): the hub's network cable is
plugged into the Raspberry Pi's Ethernet port (or a computer's spare wired
port), and the machine reaches the internet another way, over Wi-Fi on a Pi.
It gives the hub its address and answers AcuRite's server name with itself. Nothing on the hub is changed, and no router settings are needed.

## Do this

1. Check the hardware: `ip -br link` must show a wired port for the hub and a
   second, connected way to the house network. On a Pi those are `eth0` and
   Wi-Fi (`wlan0`). On a laptop or PC the hub's port is often a USB network
   adapter (`enx...`), and the house side may be Wi-Fi or the built-in port.
   No spare wired port (Pi Zero, a laptop with no adapter): stop and explain
   that this setup needs one. House network not connected: help them connect
   it (`sudo nmtui`) before going further, because the next step takes the
   hub's port away from the house network.
2. Run the installer, as root:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install.sh | sudo bash
   ```

   On a Pi it uses `eth0`. Elsewhere it lists the spare wired ports and asks
   which one the hub's cable goes into; you can answer for them with
   `| sudo bash -s -- --hub-network PORT`. It is safe to run twice. It skips the
   network part if the port was already set up by hand, or if the machine
   reaches the house network through that port, and stops if no other port
   reaches the internet; tell the person if that happens. Its log is
   `/opt/cc0ac-weather/updates.log`.
3. Ask them to plug the hub's cable into that port (and to power
   the hub, if it isn't). Within a minute `cat /var/lib/misc/dnsmasq.leases`
   shows the hub, and the setup page lists it.
4. Send them to `http://<hostname>.local/` (or the Pi's Wi-Fi address) to
   answer the setup page: sensor names, location, alerts. You can't fill it in
   for them; the names are theirs.
5. Within 5 minutes, `journalctl -u cc0ac-weather -f` shows a line per sensor.

## Where things are

- Program: `/opt/cc0ac-weather`, the boot loader, with each version under
  `versions/`. It updates itself if the person said yes in setup (UPDATES.md);
  `sudo python3 /opt/cc0ac-weather/bootloader.py status` says which version runs.
  Don't `git pull` there: it isn't a clone any more.
- Settings: `/var/lib/cc0ac-weather/.cc0ac-weather/config.ini` (also editable in
  the browser at `/config.cgi`). Data: `/var/lib/cc0ac-weather/AcuRite/`.
- Services: `cc0ac-weather` (the boot loader, which runs the program, the
  alerts, the disk check and the backup), `dnsmasq`, `nftables`.

## If it doesn't work

- **No lease for the hub:** cable, hub power, `journalctl -u dnsmasq`.
- **Lease but no readings:** the hub may be asking a public DNS server; the
  firewall's DNAT rules in `/etc/nftables.conf` catch that. Check with
  `sudo tcpdump -ni <the hub's port> port 53 or port 443` (the port is
  `hub_interface` in `/opt/cc0ac-weather/bootloader.json`).
- **Weather Underground stopped:** see README, "If Weather Underground stops".
  Never print or log the hub's Weather Underground password.
- **Give up cleanly:** the hub's cable back into the router restores everything.

Don't change the hub's own settings (its server name) for this setup, don't open
ports on their router, and don't put the hub's port back on the house
network while the installer's settings are in place.
