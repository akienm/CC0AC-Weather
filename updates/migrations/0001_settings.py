"""The station's user (Linux), its folders, and config.ini from config.ini.example.
An existing config.ini is never touched."""
import os
import subprocess
import sys

PLATFORMS = ("pi", "debian", "docker", "windows", "macos")
GROUPS = ("plugdev", "systemd-journal")


def apply(ctx):
    if ctx.platform in ("pi", "debian") and ctx.user:
        if subprocess.run(["id", ctx.user], capture_output=True).returncode != 0:
            ctx.run(["useradd", "--system", "--home-dir", str(ctx.home), "--create-home",
                     "--shell", "/usr/sbin/nologin", ctx.user])
        # plugdev opens the radio dongle; systemd-journal lets diagnostics read the service's log.
        for group in GROUPS:
            if subprocess.run(["getent", "group", group], capture_output=True).returncode == 0:
                ctx.run(["usermod", "--append", "--groups", group, ctx.user])
    readings = ctx.home / ("AcuRite" if ctx.platform in ("pi", "debian") else "readings")
    for d in (ctx.home, ctx.config.parent, readings):
        d.mkdir(parents=True, exist_ok=True)
    if not ctx.config.exists():
        sys.path.insert(0, str(ctx.version_dir))
        try:
            from cc0ac import wizard
        finally:
            sys.path.pop(0)
        text = (ctx.version_dir / "config.ini.example").read_text()
        text = wizard.set_values(text, "device", {"write_path": str(readings)})
        # Only the Pi and Debian can take the hub's cable, and only they may open ports 80 and 443.
        hub = ctx.platform in ("pi", "debian") and ctx.hub_network
        text = wizard.set_values(text, "hub", {"enabled": "true" if hub else "false"})
        text = wizard.set_values(text, "web", {"enabled": "true",
                                               "port": "80, 12345" if ctx.platform in ("pi", "debian") else "12345"})
        ctx.config.write_text(text)
        os.chmod(ctx.config, 0o600)
        ctx.log.info("  wrote %s", ctx.config)
    if ctx.platform in ("pi", "debian") and ctx.user:
        for d in (ctx.home, ctx.config.parent, readings):
            ctx.run(["chown", "-R", f"{ctx.user}:{ctx.user}", str(d)])
