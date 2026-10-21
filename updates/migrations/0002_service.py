"""The service that starts the boot loader when the machine starts: systemd on the Pi and
Debian, launchd on macOS, a scheduled task at logon on Windows. Docker's is the container.
On a machine installed with the old setup/install-pi.sh, its station service and timer
are replaced: the boot loader now runs the station and the 5-minute check."""
import os
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

PLATFORMS = ("pi", "debian", "windows", "macos")
UNIT = Path("/etc/systemd/system/cc0ac-weather.service")
OLD = [Path("/etc/systemd/system/cc0ac-weather-check.timer"), Path("/etc/systemd/system/cc0ac-weather-check.service")]
LABEL = "com.github.akienm.cc0ac-weather"


def apply(ctx):
    {"pi": systemd, "debian": systemd, "macos": launchd, "windows": scheduled_task}[ctx.platform](ctx)


def systemd(ctx):
    if not ctx.image:
        subprocess.run(["systemctl", "disable", "--now", "cc0ac-weather-check.timer"], capture_output=True)
    for f in OLD:
        if f.exists() and "setup/install-pi.sh" in f.read_text()[:200]:   # the old installer's mark
            f.unlink()
    ctx.write_ours(UNIT, f"""[Unit]
Description=CC0AC-Weather (the boot loader runs the station and keeps it patched)
Documentation=https://github.com/akienm/CC0AC-Weather/blob/main/UPDATES.md
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={' '.join(ctx.bootloader_cmd)}
Restart=always
RestartSec=10
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
""")
    ctx.run(["systemctl", "enable", "cc0ac-weather.service"], capture_output=True)
    if not ctx.image:
        ctx.run(["systemctl", "daemon-reload"])
        ctx.run(["systemctl", "restart", "cc0ac-weather.service"])


def launchd(ctx):
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    args = "".join(f"\n    <string>{escape(a)}</string>" for a in ctx.bootloader_cmd)
    ctx.write_ours(plist, f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{ctx.root / 'bootloader.err'}</string>
</dict>
</plist>
""", comment=("<!--", "-->"))
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    ctx.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])


def scheduled_task(ctx):
    python = ctx.bootloader_cmd[0]
    pythonw = Path(python).with_name("pythonw.exe")
    exe = str(pythonw) if pythonw.exists() else python
    cmd = f'"{exe}" "{ctx.bootloader_cmd[1]}" run'
    ctx.run(["schtasks", "/Create", "/F", "/TN", "CC0AC-Weather", "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", cmd])
    ctx.run(["schtasks", "/Run", "/TN", "CC0AC-Weather"])
