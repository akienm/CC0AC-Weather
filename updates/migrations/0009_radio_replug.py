"""The station may unplug its radio and plug it back in, in software: the kernel lets go of
the RTL-SDR dongle as if it were pulled out, then finds it again from scratch (only its
power stays on). It is a step on rtl_433's ladder (sources/rtl_433.py), after the USB
reset and before restarting the whole station (Akien 2026-11-11: "it's supposed to be an
appliance").

That takes root, so this writes one small script owned by root, and a sudoers rule that
lets the station's user run that script, with no arguments, and nothing else. A real
power cut is not offered: a Pi 4 or 400 switches all its USB ports' power together, and
a Pi that boots from a USB disk would pull out its own disk."""
import subprocess
import tempfile
from pathlib import Path

PLATFORMS = ("pi", "debian")
SCRIPT = Path("/usr/local/libexec/cc0ac-weather/replug-radio")
SUDOERS = Path("/etc/sudoers.d/cc0ac-weather-radio")

REPLUG = """#!/bin/sh
# CC0AC-Weather: unplug and plug back in, in software, every RTL-SDR radio on USB.
# It takes no arguments, so the station's user may run it as root and nothing else
# (/etc/sudoers.d/cc0ac-weather-radio).
found=0
for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] && [ -f "$d/idProduct" ] && [ -f "$d/authorized" ] || continue
    case "$(cat "$d/idVendor"):$(cat "$d/idProduct")" in
        0bda:2838|0bda:2832) ;;
        *) continue ;;
    esac
    echo 0 > "$d/authorized" && sleep 2 && echo 1 > "$d/authorized" || { echo "could not replug ${d##*/}"; exit 1; }
    echo "replugged ${d##*/}"
    found=1
done
[ "$found" = 1 ] || { echo "no RTL-SDR radio on USB"; exit 2; }
"""


def rule(user: str, script: Path = SCRIPT) -> str:
    return f'{user} ALL=(root) NOPASSWD: {script} ""\n'     # "": with no arguments only


def apply(ctx, script: Path = SCRIPT, sudoers: Path = SUDOERS):
    user = getattr(ctx, "user", None)
    if not user or user == "root":
        ctx.log.info("  the station runs as root (or no user is named); no sudoers rule needed")
        user = None
    if ctx.ours_or_absent(script):
        ctx.write_ours(script, REPLUG, 0o755)
    if user is None or not ctx.ours_or_absent(sudoers):
        return
    text = rule(user, script)
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as f:
        f.write(text)
    try:
        checked = subprocess.run(["visudo", "-cf", f.name], capture_output=True, text=True)
    except FileNotFoundError:
        checked = None
    finally:
        Path(f.name).unlink(missing_ok=True)
    if checked is None or checked.returncode != 0:
        ctx.log.warning("  sudoers rule for the radio not written (%s); the station skips that step",
                        "no visudo" if checked is None else checked.stdout.strip() or checked.stderr.strip())
        return
    ctx.write_ours(sudoers, text, 0o440)
    ctx.log.info("  %s may unplug and replug the radio (%s)", user, script)
