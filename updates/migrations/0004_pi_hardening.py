"""An appliance Pi looks after itself: no ssh as root, the operating system's security
updates installed automatically, and a Wi-Fi watchdog.

The watchdog is from a measured outage (2026-10-15): the Wi-Fi stayed "connected" for
2.5 hours while carrying nothing, and NetworkManager never noticed. Every minute it pings
the router; after 2 missed minutes it reconnects the Wi-Fi, after 10 it reboots (a wedged
Wi-Fi chip only comes back that way), at most once an hour."""
import subprocess
from pathlib import Path

PLATFORMS = ("pi",)
ENV = {"DEBIAN_FRONTEND": "noninteractive", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}

WATCHDOG = r"""#!/bin/sh
# CC0AC-Weather Wi-Fi watchdog, run every minute by wifi-watchdog.timer. A Pi on a cable
# (no default route over wlan0) is left alone.
ip -4 route show default | grep -q 'dev wlan0' || { rm -f /run/wifi-watchdog.misses; exit 0; }
GW=$(ip -4 route show default dev wlan0 | awk '{print $3; exit}')
F=/run/wifi-watchdog.misses
[ -n "$GW" ] && ping -c 3 -W 2 -I wlan0 "$GW" >/dev/null 2>&1 && { rm -f $F; exit 0; }
n=$(( $(cat $F 2>/dev/null || echo 0) + 1 )); echo $n > $F
echo "router ${GW:-unknown} unreachable over wlan0 ($n minute(s))"
if [ $n -ge 10 ] && [ "$(cut -d. -f1 /proc/uptime)" -gt 3600 ]; then
    echo "still unreachable after 10 minutes; rebooting"; systemctl reboot
elif [ $n -eq 2 ] || [ $n -eq 6 ]; then
    echo "reconnecting Wi-Fi"; nmcli radio wifi off; sleep 3; nmcli radio wifi on
fi
"""


def apply(ctx):
    ctx.run(["apt-get", "install", "-y", "-qq", "unattended-upgrades"], env=ENV)
    auto = Path("/etc/apt/apt.conf.d/20auto-upgrades")
    if ctx.ours_or_absent(auto):
        ctx.write_ours(auto, 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n',
                       comment=("//", ""))
    sshd = Path("/etc/ssh/sshd_config.d/10-cc0ac.conf")
    if ctx.ours_or_absent(sshd):
        ctx.write_ours(sshd, "PermitRootLogin no\n")
        if not ctx.image:
            subprocess.run(["systemctl", "reload", "ssh"], capture_output=True)
    script = Path("/usr/local/sbin/wifi-watchdog")
    if ctx.ours_or_absent(script):
        ctx.write_ours(script, WATCHDOG, 0o755)
    keep = lambda path, text: ctx.ours_or_absent(path) and ctx.write_ours(path, text)   # noqa: E731
    keep(Path("/etc/systemd/system/wifi-watchdog.service"), """[Unit]
Description=CC0AC-Weather: check the Wi-Fi uplink, repair it if it went silent

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wifi-watchdog
""")
    keep(Path("/etc/systemd/system/wifi-watchdog.timer"), """[Unit]
Description=CC0AC-Weather: Wi-Fi watchdog every minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
""")
    ctx.run(["systemctl", "enable", "wifi-watchdog.timer"], capture_output=True)
    if not ctx.image:
        ctx.run(["systemctl", "daemon-reload"])
        ctx.run(["systemctl", "start", "wifi-watchdog.timer"])
