"""The Pi (or a Debian machine) becomes the AcuRite hub's network: the cable port gets
192.168.77.1, dnsmasq gives the hub an address and answers atlasapi.myacurite.com with
this machine, and the hub's other traffic goes out to the internet and nowhere else.

The firewall lets in, from your own network (any private address range): ssh, the
dashboard and setup page, console uploads and mDNS. From the internet, if you forward a
port: the dashboard only, a limited number of new connections per visitor. From the
cable: the hub's own traffic, and ssh, so a Pi whose Wi-Fi is lost can still be reached.

Skipped with install --no-hub-network, or when the cable port was set up by hand."""
import subprocess
from pathlib import Path

PLATFORMS = ("pi", "debian")
HUB_IF = "eth0"
HUB_NET = "192.168.77"
NM_FILE = Path("/etc/NetworkManager/system-connections/cc0ac-hub.nmconnection")
DNSMASQ_FILE = Path("/etc/dnsmasq.d/cc0ac-hub.conf")
NFT = Path("/etc/nftables.conf")
PRIVATE4 = "10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16"
PRIVATE6 = "fe80::/10, fc00::/7"


def apply(ctx):
    if not ctx.hub_network:
        ctx.log.info("  no hub on the cable; network left alone")
        return
    hand_made = not ctx.ours_or_absent(DNSMASQ_FILE) or (
        not ctx.image and not NM_FILE.exists()
        and f"inet {HUB_NET}.1/" in subprocess.run(["ip", "-4", "addr", "show", HUB_IF],
                                                    capture_output=True, text=True).stdout)
    if hand_made:
        ctx.log.info("  the cable port was set up by hand here; network left alone")
        return
    ctx.run(["apt-get", "install", "-y", "-qq", "dnsmasq", "nftables", "avahi-daemon"],
            env={"DEBIAN_FRONTEND": "noninteractive", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    ctx.write_ours(NM_FILE, f"""[connection]
id=cc0ac-hub
type=ethernet
interface-name={HUB_IF}
autoconnect=true

[ethernet]

[ipv4]
method=manual
address1={HUB_NET}.1/24
never-default=true

[ipv6]
method=disabled
""", 0o600)
    ctx.write_ours(DNSMASQ_FILE, f"""# DHCP and DNS for the hub, on the cable port only. bind-dynamic: the port may
# come up after dnsmasq does (no cable yet), and dnsmasq picks it up when it does.
interface={HUB_IF}
except-interface=lo
bind-dynamic
dhcp-range={HUB_NET}.50,{HUB_NET}.99,12h
dhcp-option=option:router,{HUB_NET}.1
dhcp-option=option:dns-server,{HUB_NET}.1
# The hub's server name answers with this machine; every other name passes through.
address=/atlasapi.myacurite.com/{HUB_NET}.1
""")
    ctx.write_ours(Path("/etc/sysctl.d/90-cc0ac-forward.conf"), "net.ipv4.ip_forward=1\n")
    if NFT.exists() and not ctx.ours_or_absent(NFT) and not Path(str(NFT) + ".before-cc0ac").exists():
        ctx.run(["cp", "-a", str(NFT), str(NFT) + ".before-cc0ac"])
        ctx.log.info("  kept the old firewall as %s.before-cc0ac", NFT)
    ctx.write_ours(NFT, FIREWALL.format(hub=HUB_IF, net=HUB_NET, p4=PRIVATE4, p6=PRIVATE6), 0o755)
    ctx.run(["nft", "-c", "-f", str(NFT)])
    ctx.run(["systemctl", "enable", "dnsmasq", "nftables"], capture_output=True)
    avahi = Path("/etc/avahi/avahi-daemon.conf")
    if avahi.exists():   # mDNS only on the house side, or avahi hears itself twice and renames the machine
        text = avahi.read_text().splitlines()
        text = [f"deny-interfaces={HUB_IF}" if l.lstrip("#").startswith("deny-interfaces=") else l for l in text]
        avahi.write_text("\n".join(text) + "\n")
    if not ctx.image:
        ctx.run(["sysctl", "-q", "--system"])
        ctx.run(["systemctl", "restart", "nftables"])
        subprocess.run(["nmcli", "connection", "reload"], capture_output=True)
        subprocess.run(["nmcli", "connection", "up", "cc0ac-hub"], capture_output=True)
        ctx.run(["systemctl", "restart", "dnsmasq", "avahi-daemon"], check=False)


FIREWALL = """#!/usr/sbin/nft -f
# {hub} = the hub's cable ({net}.0/24). Every other interface is your network or the internet.
flush ruleset

table inet cc0ac {{
    set private4 {{ type ipv4_addr; flags interval; elements = {{ {p4} }} }}
    set private6 {{ type ipv6_addr; flags interval; elements = {{ {p6} }} }}
    # Visitors from the internet: new dashboard connections per address.
    set visitors4 {{ type ipv4_addr; flags dynamic; timeout 5m; }}
    set visitors6 {{ type ipv6_addr; flags dynamic; timeout 5m; }}

    chain input {{
        type filter hook input priority filter; policy drop;
        iif lo accept
        ct state established,related accept
        ct state invalid drop
        meta l4proto {{ icmp, ipv6-icmp }} limit rate 20/second burst 50 packets accept
        meta l4proto {{ icmp, ipv6-icmp }} drop
        udp dport {{ 68, 546 }} accept comment "this machine's own DHCP replies"

        # ssh from your network, or over the hub's cable as a way back in; never from the internet.
        iifname "{hub}" tcp dport 22 accept comment "ssh over the cable"
        ip saddr @private4 tcp dport 22 accept comment "ssh from your network"
        ip6 saddr @private6 tcp dport 22 accept

        # Your network: the dashboard, the setup page and console uploads, unlimited.
        iifname != "{hub}" ip saddr @private4 tcp dport {{ 80, 12345, 12346 }} accept
        iifname != "{hub}" ip6 saddr @private6 tcp dport {{ 80, 12345, 12346 }} accept
        # The internet (a forwarded port): the dashboard only, rate-limited per visitor.
        iifname != "{hub}" tcp dport {{ 80, 12345 }} ct state new update @visitors4 {{ ip saddr limit rate over 120/minute burst 240 packets }} drop comment "dashboard flood"
        iifname != "{hub}" tcp dport {{ 80, 12345 }} ct state new update @visitors6 {{ ip6 saddr limit rate over 120/minute burst 240 packets }} drop
        iifname != "{hub}" tcp dport {{ 80, 12345 }} accept comment "dashboard, visitors"

        iifname != "{hub}" udp dport 5353 accept comment "mDNS: cc0ac-weather.local"
        iifname "{hub}" tcp dport 443 accept comment "the hub's uploads"
        iifname "{hub}" udp dport {{ 53, 67 }} accept comment "the hub's DNS and DHCP"
        iifname "{hub}" tcp dport 53 accept

        pkttype host limit rate 6/minute log prefix "cc0ac-drop " level info
    }}
    chain forward {{
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        # The hub gets the internet only; nothing else on your network is its business.
        iifname "{hub}" ip daddr @private4 limit rate 6/minute log prefix "cc0ac-hub-lan " level info
        iifname "{hub}" ip daddr @private4 drop
        iifname "{hub}" oifname != "{hub}" meta nfproto ipv4 accept comment "the hub out to the internet"
    }}
}}

table ip cc0ac_nat {{
    chain prerouting {{
        type nat hook prerouting priority dstnat;
        # The hub sometimes ignores the DNS server it was given and asks 8.8.8.8, then
        # uploads straight to AcuRite. These bring its DNS and its uploads here whatever
        # server or address it picked.
        iifname "{hub}" udp dport 53 dnat to {net}.1
        iifname "{hub}" tcp dport 53 dnat to {net}.1
        iifname "{hub}" tcp dport 443 dnat to {net}.1
    }}
    chain postrouting {{
        type nat hook postrouting priority srcnat;
        oifname != "{hub}" ip saddr {net}.0/24 masquerade
    }}
}}
"""
