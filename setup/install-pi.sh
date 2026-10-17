#!/usr/bin/env bash
# CC0AC-Weather on a Raspberry Pi, with the AcuRite Access hub plugged into the Pi
# (README, method B): the hub's cable goes in the Pi's network port, and the Pi
# reaches your network over Wi-Fi.
#
#   curl -fsSL https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install-pi.sh | sudo bash
#
# or, from a copy of the repository:   sudo setup/install-pi.sh
#
# Safe to run again: each step checks what is already there. When it finishes,
# open http://cc0ac-weather.local/ (or the Pi's address) and answer the setup page.
#
# What it does:
#   * installs dnsmasq, nftables, git and automatic security updates
#   * puts the program in /opt/cc0ac-weather and runs it as the user cc0ac-weather,
#     settings in /var/lib/cc0ac-weather/.cc0ac-weather/config.ini
#   * the cable port gets 192.168.77.1; dnsmasq hands the hub an address and answers
#     atlasapi.myacurite.com with the Pi; the hub's other traffic (Weather Underground,
#     its clock) is passed out to the internet over Wi-Fi
#   * a firewall that lets in only ssh, the dashboard (ports 80 and 12345), console uploads (12346) and
#     mDNS from your network, and the hub's own traffic on the cable
#   * a timer that checks every 5 minutes that readings are coming in ([alerts])
#
# Options:
#   --image       building an SD card image in a chroot: install and enable, start nothing
#   --hostname N  name the Pi N (default with --image: cc0ac-weather; otherwise unchanged)
#   --no-network  leave the cable port, dnsmasq and the firewall alone
#   --source DIR  install the program from DIR instead of cloning it from GitHub
set -euo pipefail

REPO_URL=https://github.com/akienm/CC0AC-Weather.git
APP=/opt/cc0ac-weather
SVC_USER=cc0ac-weather
SVC_HOME=/var/lib/cc0ac-weather
HUB_IF=eth0
HUB_NET=192.168.77
MARK="# Written by CC0AC-Weather setup/install-pi.sh; it rewrites this file when run again."

IMAGE=0 NETWORK=1 HOSTNAME_WANTED="" SOURCE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE=1 ;;
        --no-network) NETWORK=0 ;;
        --hostname) HOSTNAME_WANTED="$2"; shift ;;
        --source) SOURCE="$2"; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done
[ "$IMAGE" = 1 ] && [ -z "$HOSTNAME_WANTED" ] && HOSTNAME_WANTED=cc0ac-weather

say() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0 $*" >&2; exit 1; }

# A file this script wrote (or none at all) may be rewritten; a file someone wrote
# by hand is kept and the step is skipped, so a Pi already set up by hand is left alone.
ours_or_absent() { [ ! -e "$1" ] || head -n 3 "$1" | grep -qF "$MARK"; }
write_ours() {   # write_ours PATH MODE < content
    local tmp; tmp=$(mktemp)
    { echo "$MARK"; cat; } > "$tmp"
    install -m "$2" "$tmp" "$1"; rm -f "$tmp"
}

say "Packages"
export DEBIAN_FRONTEND=noninteractive
if [ "$IMAGE" = 1 ]; then
    # Nothing starts inside the image builder's chroot.
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; chmod 755 /usr/sbin/policy-rc.d
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
fi
apt-get update -qq
apt-get install -y -qq python3 git dnsmasq nftables avahi-daemon unattended-upgrades >/dev/null
# The Debian package starts dnsmasq listening everywhere; ours is limited to the cable below.
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
    > /etc/apt/apt.conf.d/20auto-upgrades

say "The program, in $APP"
if [ -n "$SOURCE" ]; then
    rm -rf "$APP.new"; git clone -q "$SOURCE" "$APP.new"
    git -C "$APP.new" remote set-url origin "$REPO_URL"
    rm -rf "$APP"; mv "$APP.new" "$APP"
elif [ -d "$APP/.git" ]; then
    git -C "$APP" pull -q --ff-only
else
    git clone -q "$REPO_URL" "$APP"
fi
git config --system --add safe.directory "$APP" 2>/dev/null || true

say "User $SVC_USER and its settings"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$SVC_HOME" --create-home --shell /usr/sbin/nologin "$SVC_USER"
CONF_DIR="$SVC_HOME/.cc0ac-weather"
CONF="$CONF_DIR/config.ini"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 700 "$CONF_DIR"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 755 "$SVC_HOME/AcuRite"
if [ ! -e "$CONF" ]; then
    python3 - "$APP/config.ini.example" "$CONF" "$SVC_HOME/AcuRite" <<'PY'
import sys
sys.path.insert(0, sys.argv[1].rsplit("/", 1)[0])
import wizard
text = open(sys.argv[1]).read()
text = wizard.set_values(text, "device", {"write_path": sys.argv[3]})
text = wizard.set_values(text, "web", {"enabled": "true", "port": "80, 12345"})
open(sys.argv[2], "w").write(text)
PY
    chown "$SVC_USER:$SVC_USER" "$CONF"; chmod 600 "$CONF"
    echo "Wrote $CONF"
else
    echo "Kept $CONF as it is"
fi

say "Services"
cat > /etc/systemd/system/cc0ac-weather.service <<EOF
$MARK
[Unit]
Description=CC0AC-Weather: AcuRite hub relay, recorder and dashboard
Documentation=https://github.com/akienm/CC0AC-Weather
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
ExecStart=/usr/bin/python3 $APP/acurite-capture.py --config $CONF
# The hub insists on port 443; the dashboard is on 80.
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/cc0ac-weather-check.service <<EOF
$MARK
[Unit]
Description=CC0AC-Weather: send an alert if readings have stopped ([alerts])

[Service]
Type=oneshot
User=$SVC_USER
ExecStart=/usr/bin/python3 $APP/acurite-capture.py --config $CONF --check
EOF
cat > /etc/systemd/system/cc0ac-weather-check.timer <<EOF
$MARK
[Unit]
Description=CC0AC-Weather: check every 5 minutes that readings are coming in

[Timer]
OnBootSec=10min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
systemctl enable cc0ac-weather.service cc0ac-weather-check.timer avahi-daemon.service >/dev/null 2>&1 \
    || systemctl enable cc0ac-weather.service cc0ac-weather-check.timer avahi-daemon.service

if [ -n "$HOSTNAME_WANTED" ]; then
    say "Hostname $HOSTNAME_WANTED"
    echo "$HOSTNAME_WANTED" > /etc/hostname
    sed -i "s/^127\.0\.1\.1\s.*/127.0.1.1\t$HOSTNAME_WANTED/" /etc/hosts
    grep -q '^127\.0\.1\.1' /etc/hosts || printf '127.0.1.1\t%s\n' "$HOSTNAME_WANTED" >> /etc/hosts
    [ "$IMAGE" = 1 ] || hostnamectl set-hostname "$HOSTNAME_WANTED" 2>/dev/null || true
fi

if [ "$NETWORK" = 1 ]; then
    say "The hub's cable port ($HUB_IF, $HUB_NET.1)"
    NM_FILE=/etc/NetworkManager/system-connections/cc0ac-hub.nmconnection
    DNSMASQ_FILE=/etc/dnsmasq.d/cc0ac-hub.conf
    if ! ours_or_absent "$DNSMASQ_FILE" || { [ "$IMAGE" = 0 ] && ! [ -e "$NM_FILE" ] \
            && ip -4 addr show "$HUB_IF" 2>/dev/null | grep -q "inet $HUB_NET.1/"; }; then
        echo "The cable port is already set up by hand here; leaving the network alone."
        NETWORK=0
    fi
fi
if [ "$NETWORK" = 1 ]; then
    write_ours "$NM_FILE" 600 <<EOF
[connection]
id=cc0ac-hub
type=ethernet
interface-name=$HUB_IF
autoconnect=true

[ethernet]

[ipv4]
method=manual
address1=$HUB_NET.1/24
never-default=true

[ipv6]
method=disabled
EOF
    write_ours "$DNSMASQ_FILE" 644 <<EOF
# DHCP and DNS for the hub, on the cable port only. bind-dynamic: the port may
# come up after dnsmasq does (no cable yet), and dnsmasq picks it up when it does.
interface=$HUB_IF
except-interface=lo
bind-dynamic
dhcp-range=$HUB_NET.50,$HUB_NET.99,12h
dhcp-option=option:router,$HUB_NET.1
dhcp-option=option:dns-server,$HUB_NET.1
# The hub's server name answers with this Pi; every other name passes through.
address=/atlasapi.myacurite.com/$HUB_NET.1
EOF
    write_ours /etc/sysctl.d/90-cc0ac-forward.conf 644 <<<"net.ipv4.ip_forward=1"
    if [ -e /etc/nftables.conf ] && ! ours_or_absent /etc/nftables.conf && [ ! -e /etc/nftables.conf.before-cc0ac ]; then
        cp -a /etc/nftables.conf /etc/nftables.conf.before-cc0ac
        echo "Kept the old firewall as /etc/nftables.conf.before-cc0ac"
    fi
    write_ours /etc/nftables.conf 755 <<EOF
#!/usr/sbin/nft -f
# $HUB_IF = the hub's cable ($HUB_NET.0/24). Every other interface is your network.
flush ruleset

table inet cc0ac {
    chain input {
        type filter hook input priority filter; policy drop;
        iif lo accept
        ct state established,related accept
        ct state invalid drop
        meta l4proto { icmp, ipv6-icmp } accept
        tcp dport 22 accept comment "ssh"
        udp dport { 68, 546 } accept comment "the Pi's own DHCP replies"
        iifname != "$HUB_IF" tcp dport { 80, 12345, 12346 } accept comment "dashboard, setup page, console uploads"
        iifname != "$HUB_IF" udp dport 5353 accept comment "mDNS: cc0ac-weather.local"
        iifname "$HUB_IF" tcp dport 443 accept comment "the hub's uploads"
        iifname "$HUB_IF" udp dport { 53, 67 } accept comment "the hub's DNS and DHCP"
        iifname "$HUB_IF" tcp dport 53 accept
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        iifname "$HUB_IF" oifname != "$HUB_IF" meta nfproto ipv4 accept comment "the hub out to the internet"
    }
}

table ip cc0ac_nat {
    chain prerouting {
        type nat hook prerouting priority dstnat;
        # The hub sometimes ignores the DNS server it was given and asks 8.8.8.8,
        # then uploads straight to AcuRite. These bring its DNS and its uploads
        # to the Pi whatever server or address it picked.
        iifname "$HUB_IF" udp dport 53 dnat to $HUB_NET.1
        iifname "$HUB_IF" tcp dport 53 dnat to $HUB_NET.1
        iifname "$HUB_IF" tcp dport 443 dnat to $HUB_NET.1
    }
    chain postrouting {
        type nat hook postrouting priority srcnat;
        oifname != "$HUB_IF" ip saddr $HUB_NET.0/24 masquerade
    }
}
EOF
    nft -c -f /etc/nftables.conf
    systemctl enable dnsmasq nftables >/dev/null 2>&1 || systemctl enable dnsmasq nftables
    # mDNS only on the house side, or avahi hears itself on both ports and renames the Pi.
    if [ -e /etc/avahi/avahi-daemon.conf ]; then
        sed -i "s/^#\?deny-interfaces=.*/deny-interfaces=$HUB_IF/" /etc/avahi/avahi-daemon.conf
    fi
fi

if [ "$IMAGE" = 1 ]; then
    say "Done (image): everything starts on first boot"
    exit 0
fi

say "Starting"
systemctl daemon-reload
if [ "$NETWORK" = 1 ]; then
    sysctl -q --system
    systemctl restart nftables
    nmcli connection reload 2>/dev/null && nmcli connection up cc0ac-hub >/dev/null 2>&1 || true
    systemctl restart dnsmasq avahi-daemon
fi
systemctl restart cc0ac-weather.service
systemctl start cc0ac-weather-check.timer
sleep 2
systemctl --no-pager --lines=5 status cc0ac-weather.service || true
NAME=$(hostname)
ADDR=$(ip -4 -o addr show scope global | awk -v hub="$HUB_IF" '$2 != hub {sub("/.*", "", $4); print $4; exit}')
cat <<EOF

All set. Plug the hub's network cable into this Pi's network port if it isn't
already, then open the setup page from a computer or phone on your Wi-Fi:

    http://$NAME.local/        (or http://${ADDR:-<this Pi's address>}/)
EOF
