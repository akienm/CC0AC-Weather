"""dnsmasq, which gives the hub its address and points it at this station, is started
again whenever it stops. On 2026-11-11 a reboot started it before the hub's port was up;
it quit ("unknown interface eth0") and the hub went unheard for three hours. 0003 now
writes bind-dynamic, which waits for the port; this is the second line: systemd restarts
it if it ever exits. Any station with the hub on the cable, set up by hand or not."""
import subprocess
from pathlib import Path

PLATFORMS = ("pi", "debian")
DNSMASQ_FILE = Path("/etc/dnsmasq.d/cc0ac-hub.conf")
DROP_IN = Path("/etc/systemd/system/dnsmasq.service.d/cc0ac-restart.conf")
TEXT = "[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestart=always\nRestartSec=5\n"


def apply(ctx, dnsmasq_file: Path = DNSMASQ_FILE, drop_in: Path = DROP_IN):
    if not dnsmasq_file.exists():
        ctx.log.info("  no hub on the cable here; dnsmasq left alone")
        return
    if not ctx.ours_or_absent(drop_in):
        return
    ctx.write_ours(drop_in, TEXT)
    if not ctx.image:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        ctx.run(["systemctl", "restart", "dnsmasq"], check=False)
    ctx.log.info("  dnsmasq is started again whenever it stops")
