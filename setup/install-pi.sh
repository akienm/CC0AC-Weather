#!/usr/bin/env bash
# The Pi's installer is setup/install.sh now (the Pi, Debian, Ubuntu and macOS).
# This name stays so the old one-liner keeps working:
#
#   curl -fsSL https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install-pi.sh | sudo bash
#
# Its old options are passed on: --no-network is install.sh's --no-hub-network.
set -euo pipefail
ARGS=()
for a in "$@"; do
    case "$a" in
        --no-network) ARGS+=(--no-hub-network) ;;
        *) ARGS+=("$a") ;;
    esac
done
HERE=$(dirname "${BASH_SOURCE[0]:-}")
if [ -f "$HERE/install.sh" ]; then
    exec bash "$HERE/install.sh" ${ARGS[@]+"${ARGS[@]}"}
fi
curl -fsSL https://raw.githubusercontent.com/akienm/CC0AC-Weather/main/setup/install.sh | bash -s -- ${ARGS[@]+"${ARGS[@]}"}
