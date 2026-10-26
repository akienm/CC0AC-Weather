# CC0AC-Weather in a container: the boot loader runs the station and keeps it
# patched, with everything it keeps in the volume at /data. Build from the
# repository's top folder:   docker build -f setup/docker/Dockerfile -t cc0ac-weather .
# or use compose.yaml beside this file. Python is pinned here; the station itself
# uses only the standard library.
FROM python:3.12.7-slim-bookworm

# smartmontools: the disk check (it sees disks only when they are passed in);
# smbclient: the backup to a Windows share.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends smartmontools smbclient tzdata \
 && rm -rf /var/lib/apt/lists/*

# The version in the image installs on the first start; after that the volume
# holds the boot loader and its versions, and updates come from the releases.
COPY . /opt/cc0ac-weather-src
RUN rm -rf /opt/cc0ac-weather-src/.git /opt/cc0ac-weather-src/CLAUDE.md

VOLUME /data
# The Access hub's uploads; the dashboard and setup page; console uploads
# (Ambient, Ecowitt and others).
EXPOSE 443 12345 12346
ENTRYPOINT ["sh", "/opt/cc0ac-weather-src/setup/docker/entrypoint.sh"]
