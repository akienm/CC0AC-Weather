"""dnsmasq passes the hub's other names on to this machine's own resolver. A station set up
under systemd-resolved (Ubuntu, Mint) before 1.5.3 got a dnsmasq that answered the hub's
server name and refused every other: on 2026-11-21 the hub restarted, asked for
pool.ntp.org to set its clock, was refused, and sent nothing for 25 minutes. 0003 now
writes the upstream for new stations; this adds it to a station 0003 set up already.
A file set up by hand is left alone, as is one that already names its upstream."""
import importlib.util
import subprocess
from pathlib import Path

PLATFORMS = ("pi", "debian")
RERUN_WHEN = ("hub_network", "hub_interface")
DNSMASQ_FILE = Path("/etc/dnsmasq.d/cc0ac-hub.conf")
_spec = importlib.util.spec_from_file_location("hub_network", Path(__file__).with_name("0003_hub_network.py"))
hub_network = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hub_network)


def apply(ctx, dnsmasq_file: Path = DNSMASQ_FILE, resolv: Path = hub_network.RESOLV):
    if not dnsmasq_file.exists() or not ctx.ours_or_absent(dnsmasq_file):
        ctx.log.info("  no dnsmasq of ours here; left alone")
        return
    lines = dnsmasq_file.read_text().splitlines()
    if any(l.startswith(("server=", "no-resolv", "resolv-file=")) for l in lines):
        ctx.log.info("  dnsmasq already names where it asks; left alone")
        return
    more = hub_network.upstream(resolv)
    if not more:
        ctx.log.info("  dnsmasq's own upstream works here; left alone")
        return
    body = "\n".join(l for l in lines if ctx.mark not in l) + "\n" + more
    ctx.write_ours(dnsmasq_file, body)
    if not ctx.image:
        ctx.run(["systemctl", "restart", "dnsmasq"], check=False)
    ctx.log.info("  dnsmasq passes the hub's other names to systemd-resolved")
