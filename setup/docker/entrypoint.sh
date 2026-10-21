#!/bin/sh
# First start: install the image's version into /data. Later starts: the boot
# loader in /data runs whatever version it has updated itself to, so a newer
# version is never replaced by the image's older one.
set -e
if [ -e /data/app/state.json ] && [ -e /data/app/bootloader.py ]; then
    exec python3 /data/app/bootloader.py run --root /data/app
fi
exec python3 /opt/cc0ac-weather-src/updates/bootloader.py install --platform docker \
    --root /data/app --home /data/station --config /data/station/.cc0ac-weather/config.ini \
    --from /opt/cc0ac-weather-src --then-run
