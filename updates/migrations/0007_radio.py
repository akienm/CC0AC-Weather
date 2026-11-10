"""The radio software, so a USB radio (an RTL-SDR dongle) works by plugging it in and
ticking a box on the setup page. rtl-433 decodes the sensors; its library, librtlsdr0,
brings the udev rule that opens the radio to the plugdev group, which the station's
user is in (0001). The kernel takes the same radio for a TV tuner unless told not to,
so a file in /etc/modprobe.d keeps its TV driver off it, and one plugged in now is let go.

The radio is optional: if the packages can't be installed, the station runs without
them and the setup page says how to install them by hand."""
from pathlib import Path

PLATFORMS = ("pi", "debian")
ENV = {"DEBIAN_FRONTEND": "noninteractive", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
TV_DRIVERS = ("dvb_usb_rtl28xxu", "rtl2832", "rtl2830")
BLACKLIST = Path("/etc/modprobe.d/cc0ac-weather-radio.conf")
INSTALL = ["apt-get", "install", "-y", "-qq", "--no-install-recommends", "rtl-433", "rtl-sdr"]


def apply(ctx, blacklist: Path = BLACKLIST):
    got = ctx.run(INSTALL, check=False, env=ENV)
    if got.returncode != 0 and not ctx.image:   # a package list from long ago names versions since gone
        ctx.run(["apt-get", "update", "-qq"], check=False, env=ENV)
        got = ctx.run(INSTALL, check=False, env=ENV)
    if got.returncode != 0:
        ctx.log.warning("  The radio software didn't install (apt-get exit %s); the station runs without it. "
                        "By hand: sudo apt install rtl-433 rtl-sdr", got.returncode)
        return
    if ctx.ours_or_absent(blacklist):
        ctx.write_ours(blacklist, "# The radio is for rtl_433, not for watching TV.\n"
                       + "".join(f"blacklist {d}\n" for d in TV_DRIVERS))
    if not ctx.image:
        for d in TV_DRIVERS:
            ctx.run(["modprobe", "--remove", d], check=False, env=ENV, capture_output=True)
