"""The station keeps a copy of its network profiles and puts one back when it finds the
file empty or broken. On 2026-10-23 two power drops at the Pi left both its profiles, the
Wi-Fi and the hub's cable, at 0 bytes: no Wi-Fi, the hub cut off, and the Wi-Fi watchdog
(0004) could only report it, because there was no profile left to reconnect with. It took
a person at the keyboard.

Every minute a small root script copies each whole profile (a NetworkManager keyfile, or
a netplan file that NetworkManager writes on newer Pi OS) to /var/lib/cc0ac-weather, only
readable by root since a Wi-Fi profile holds its password. A profile that is there but
empty or cut short is replaced by its copy, and NetworkManager is told to read it again.
A profile that is gone is left gone: deleting one is something a person does on purpose."""
import subprocess
from pathlib import Path

PLATFORMS = ("pi", "debian")
SCRIPT = Path("/usr/local/libexec/cc0ac-weather/keep-network")
UNITS = Path("/etc/systemd/system")

KEEP = r"""#!/bin/sh
# CC0AC-Weather: keep a copy of every whole network profile, and put the copy back when the
# profile is found empty or broken. Run every minute by cc0ac-keep-network.timer.
KEEP=${KEEP:-/var/lib/cc0ac-weather/network-profiles}
DIRS=${DIRS:-/etc/NetworkManager/system-connections /etc/netplan}
RELOAD=${RELOAD:-1}
umask 077
whole() {
    case "$1" in
        *.nmconnection) grep -q '^\[connection\]' "$1" ;;
        *.yaml) grep -q '^network:' "$1" ;;
    esac
}
restored=0
for dir in $DIRS; do
    [ -d "$dir" ] || continue
    keep="$KEEP/$(echo "$dir" | tr / _)"
    mkdir -p "$keep" || exit 1
    for f in "$dir"/*.nmconnection "$dir"/*.yaml; do
        [ -f "$f" ] || continue
        copy="$keep/${f##*/}"
        if whole "$f"; then
            cmp -s "$f" "$copy" || install -p -m 600 "$f" "$copy"
        elif [ -s "$copy" ]; then
            cp -p "$copy" "$f" && restored=1 &&
                echo "put back $f: it was empty or broken; the copy is of $(date -r "$copy" '+%Y-%m-%d %H:%M')"
        fi
    done
done
if [ "$restored" = 1 ] && [ "$RELOAD" = 1 ]; then
    if command -v netplan >/dev/null; then netplan generate; fi
    nmcli connection reload && echo "NetworkManager has read the profiles again"
fi
exit 0
"""

SERVICE = """[Unit]
Description=CC0AC-Weather: keep a copy of the network profiles, put back one found empty

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/cc0ac-weather/keep-network
"""

TIMER = """[Unit]
Description=CC0AC-Weather: network profile keeper every minute

[Timer]
OnBootSec=20s
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
"""


def apply(ctx, script: Path = SCRIPT, units: Path = UNITS):
    if ctx.ours_or_absent(script):
        ctx.write_ours(script, KEEP, 0o755)
    for name, text in (("cc0ac-keep-network.service", SERVICE), ("cc0ac-keep-network.timer", TIMER)):
        if ctx.ours_or_absent(units / name):
            ctx.write_ours(units / name, text)
    ctx.run(["systemctl", "enable", "cc0ac-keep-network.timer"], capture_output=True, check=False)
    if not ctx.image:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        ctx.run(["systemctl", "start", "cc0ac-keep-network.timer"], check=False)
    ctx.log.info("  network profiles are copied every minute, and one found empty is put back")
