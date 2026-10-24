#!/usr/bin/env bash
# CC0AC-Weather on a Raspberry Pi, a Debian or Ubuntu machine, or a Mac.
#
#   curl -fsSL https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install.sh | sudo bash
#   curl -fsSL https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install.sh | bash      # Mac: no sudo
#
# or, from a copy of the repository:   sudo setup/install.sh --source .
#
# It installs Python if it is missing, then hands over to the boot loader
# (updates/bootloader.py, UPDATES.md), which downloads the newest release, sets up
# the service and keeps the station patched. Safe to run again: an existing
# config.ini is never touched. When it finishes, open the address it prints and
# answer the setup page.
#
# Where things go:
#   Pi, Debian   the program in /opt/cc0ac-weather, run as the user cc0ac-weather,
#                readings and settings in /var/lib/cc0ac-weather
#                (config.ini in /var/lib/cc0ac-weather/.cc0ac-weather/)
#   Mac          ~/Library/Application Support/CC0AC-Weather, run as you
#
# The AcuRite Access hub's cable (README, method B): the hub plugs into a wired
# port of this machine, which becomes the hub's own little network, and this
# machine reaches yours some other way (Wi-Fi, or a second wired port).
#   Pi       the hub's port is eth0, unless the Pi itself reaches your network
#            through it; then the network is left alone.
#   Debian   a laptop or PC often has a port to spare only through a USB
#            network adapter. The installer lists the wired ports your network
#            isn't on and asks which one the hub's cable goes into (Enter for
#            none). Run without a terminal, it asks nothing and leaves the
#            network alone unless told: --hub-network PORT.
#
# Options:
#   --source DIR    install the program from DIR instead of downloading a release
#   --tag TAG       install this release (default: the newest)
#   --hub-network [PORT]  the hub's cable goes into PORT (e.g. enx00e04c680001; see
#                   `ip -br link`). Without PORT: the one wired port your network isn't on
#   --no-hub-network  no hub on a cable: leave the network alone
#   --hostname N    name the machine N (default with --image: cc0ac-weather)
#   --platform P    pi, debian or macos, instead of working it out
#   --image         building an SD card image in a chroot: install and enable, start nothing
set -euo pipefail

RAW=https://raw.githubusercontent.com/akienm/CC0AC-Weather/main
SOURCE="" TAG="" HUB="" HUB_IF="" IMAGE=0 HOSTNAME_WANTED="" PLATFORM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --source) SOURCE=$(cd "$2" && pwd); shift ;;
        --tag) TAG="$2"; shift ;;
        --hub-network) HUB=yes
            case "${2:-}" in ""|-*) ;; *) HUB_IF="$2"; shift ;; esac ;;
        --no-hub-network) HUB=no ;;
        --hostname) HOSTNAME_WANTED="$2"; shift ;;
        --image) IMAGE=1 ;;
        --platform) PLATFORM="$2"; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done
[ "$IMAGE" = 1 ] && [ -z "$HOSTNAME_WANTED" ] && HOSTNAME_WANTED=cc0ac-weather
say() { printf '\n== %s\n' "$*"; }

if [ -n "$PLATFORM" ]; then
    case "$PLATFORM" in pi|debian|macos) ;; *) echo "--platform is pi, debian or macos" >&2; exit 2 ;; esac
elif [ "$(uname -s)" = Darwin ]; then
    PLATFORM=macos
elif grep -qs "Raspberry Pi" /proc/device-tree/model; then
    PLATFORM=pi
elif [ -e /etc/debian_version ]; then
    PLATFORM=debian
else
    echo "This installer knows the Raspberry Pi, Debian, Ubuntu and macOS. On anything else, Docker: setup/docker/." >&2
    exit 1
fi
echo "Installing CC0AC-Weather for: $PLATFORM"

if [ "$PLATFORM" = macos ]; then
    [ "$(id -u)" != 0 ] || { echo "On a Mac, run it as yourself, without sudo: it runs in your login." >&2; exit 1; }
    ROOT="$HOME/Library/Application Support/CC0AC-Weather"
    STATION_HOME="$ROOT/station"
    CONFIG="$STATION_HOME/.cc0ac-weather/config.ini"
    USER_ARGS=()
    # /usr/bin/python3 on a Mac without the developer tools only offers to install them.
    if ! python3 -c "import ssl, sqlite3" 2>/dev/null; then
        echo "Python 3 is needed. Install it from https://www.python.org/downloads/macos/ (or: brew install python), then run this again." >&2
        exit 1
    fi
    HUB=no
else
    [ "$(id -u)" = 0 ] || { echo "Run as root: curl … | sudo bash, or sudo $0" >&2; exit 1; }
    ROOT=/opt/cc0ac-weather
    STATION_HOME=/var/lib/cc0ac-weather
    CONFIG="$STATION_HOME/.cc0ac-weather/config.ini"
    USER_ARGS=(--user cc0ac-weather)

    say "Packages"
    export DEBIAN_FRONTEND=noninteractive
    if [ "$IMAGE" = 1 ]; then
        # Nothing starts inside the image builder's chroot.
        printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; chmod 755 /usr/sbin/policy-rc.d
        trap 'rm -f /usr/sbin/policy-rc.d' EXIT
    fi
    apt-get update -qq
    # util-linux: setpriv, which runs the station as its own user.
    apt-get install -y -qq --no-install-recommends python3 ca-certificates curl util-linux \
        smartmontools smbclient avahi-daemon >/dev/null

    # The installer before 1.2.0 cloned the program into /opt/cc0ac-weather; the boot
    # loader lives there now. The settings and readings in /var/lib are kept.
    if [ -d "$ROOT/.git" ]; then
        OLD="$ROOT.before-1.2.0"
        [ -e "$OLD" ] && OLD="$OLD.$(date +%s)"
        mv "$ROOT" "$OLD"
        echo "Moved the old program to $OLD (your settings and readings stay where they are)"
    fi

    if [ -n "$HOSTNAME_WANTED" ]; then
        say "Hostname $HOSTNAME_WANTED"
        echo "$HOSTNAME_WANTED" > /etc/hostname
        sed -i "s/^127\.0\.1\.1\s.*/127.0.1.1\t$HOSTNAME_WANTED/" /etc/hosts
        grep -q '^127\.0\.1\.1' /etc/hosts || printf '127.0.1.1\t%s\n' "$HOSTNAME_WANTED" >> /etc/hosts
        [ "$IMAGE" = 1 ] || hostnamectl set-hostname "$HOSTNAME_WANTED" 2>/dev/null || true
    fi
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"; [ "$IMAGE" = 1 ] && rm -f /usr/sbin/policy-rc.d; true' EXIT
if [ -n "$SOURCE" ]; then
    cp "$SOURCE/updates/bootloader.py" "$WORK/bootloader.py"
else
    curl -fsSL -o "$WORK/bootloader.py" "$RAW/updates/bootloader.py"
fi

# Which port the hub's cable goes into, if any.
ask() {   # ask "question" → the answer, from the terminal; fails when there is none
    { : </dev/tty; } 2>/dev/null || return 1
    printf '%s ' "$1" >/dev/tty
    read -r REPLY </dev/tty
}
if [ "$PLATFORM" != macos ] && [ "$HUB" != no ] && [ "$IMAGE" = 0 ]; then
    # name usb|board cable|none, for each wired port your network isn't on
    PORTS=$(python3 "$WORK/bootloader.py" ports)
    NAMES=$(printf '%s\n' "$PORTS" | awk 'NF {print $1}')
    if [ -n "$HUB_IF" ]; then
        if ! printf '%s\n' "$NAMES" | grep -qx -- "$HUB_IF"; then
            echo "$HUB_IF can't take the hub's cable: it isn't a wired port, or it is this machine's way to your network." >&2
            echo "Ports it could be: ${NAMES:-none}" | tr '\n' ' ' >&2; echo >&2
            exit 1
        fi
    elif [ "$PLATFORM" = pi ] && [ -z "$HUB" ]; then
        if printf '%s\n' "$NAMES" | grep -qx eth0; then
            HUB=yes HUB_IF=eth0
        else
            echo "This Pi reaches your network through its cable port, so the port can't be the hub's network; leaving it alone."
            HUB=no
        fi
    elif [ -z "$NAMES" ]; then
        if [ "$HUB" = yes ]; then
            echo "No wired port to spare for the hub: every one is this machine's way to your network, or there are none." >&2
            echo "A USB network adapter gives it one. Plug one in and run this again." >&2
            exit 1
        fi
        HUB=no
    elif [ "$HUB" = yes ] && [ "$(printf '%s\n' "$NAMES" | wc -l)" = 1 ]; then
        HUB_IF=$NAMES
    else
        echo
        echo "An AcuRite Access hub can plug into this machine by cable, which then becomes"
        echo "the hub's network (README, method B). Wired ports your network isn't on:"
        n=0
        while read -r name kind cable; do
            n=$((n + 1))
            printf '  %d) %s  (%s%s)\n' "$n" "$name" \
                "$([ "$kind" = usb ] && echo "USB adapter" || echo "built in")" \
                "$([ "$cable" = cable ] && echo ", a cable is plugged in")"
        done <<<"$PORTS"
        if ask "Which one does the hub's cable go into? Number, or Enter for none:"; then
            if [ -z "$REPLY" ]; then
                HUB=no
            elif [ "$REPLY" -ge 1 ] 2>/dev/null && [ "$REPLY" -le "$n" ]; then
                HUB=yes HUB_IF=$(printf '%s\n' "$NAMES" | sed -n "${REPLY}p")
            else
                echo "No port $REPLY; run this again." >&2; exit 2
            fi
        elif [ "$HUB" = yes ]; then
            echo "Several ports could take the hub's cable; name one: --hub-network PORT" >&2; exit 2
        else
            echo "(no terminal to ask: network left alone; --hub-network PORT to give the hub one)"
            HUB=no
        fi
    fi
fi
[ "$IMAGE" = 1 ] && [ "$PLATFORM" = pi ] && [ -z "$HUB" ] && HUB=yes
[ "$HUB" = yes ] && [ -z "$HUB_IF" ] && HUB_IF=eth0
[ "$HUB" = yes ] && echo "The hub's cable goes into: $HUB_IF"

say "The boot loader"
ARGS=(install --platform "$PLATFORM" --root "$ROOT" --config "$CONFIG" --home "$STATION_HOME"
      --python "$(command -v python3)" ${USER_ARGS[@]+"${USER_ARGS[@]}"})
[ -n "$SOURCE" ] && ARGS+=(--from "$SOURCE")
[ -n "$TAG" ] && ARGS+=(--tag "$TAG")
if [ "$HUB" = yes ]; then ARGS+=(--hub-interface "$HUB_IF"); else ARGS+=(--no-hub-network); fi
[ "$IMAGE" = 1 ] && ARGS+=(--image)
python3 "$WORK/bootloader.py" "${ARGS[@]}"

if [ "$IMAGE" = 1 ]; then
    say "Done (image): everything starts on first boot"
    exit 0
fi

if [ "$PLATFORM" = macos ]; then
    URL="http://localhost:12345/setup"
    ADDR=$(ipconfig getifaddr en0 2>/dev/null || true)
    OTHER="http://${ADDR:-<its address>}:12345/"
    STATUS="python3 \"$ROOT/bootloader.py\" status"
else
    URL="http://$(hostname).local/"
    # Not the hub's own network, 192.168.77.x.
    ADDR=$(ip -4 -o addr show scope global 2>/dev/null | awk '{sub("/.*", "", $4)} $4 !~ /^192\.168\.77\./ {print $4; exit}')
    OTHER="http://${ADDR:-<its address>}/"
    STATUS="sudo python3 $ROOT/bootloader.py status"
fi
cat <<EOF

All set. Open the setup page from a computer or phone on your network:

    $URL        (or $OTHER)

Status:   $STATUS
How updates work: https://github.com/akienm/CC0AC-Weather/blob/main/UPDATES.md
EOF
[ "$HUB" = yes ] && echo "Plug the hub's network cable into $HUB_IF if it isn't already."
true
