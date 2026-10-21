"""The tools upkeep.py uses: smartctl reads the disks' health (smartmontools), and
smbclient copies the backup to a Windows share without mounting it. Docker images
carry them in the Dockerfile; Windows and macOS do without (no SMART check there,
and a share is reached by Windows itself)."""

PLATFORMS = ("pi", "debian")


def apply(ctx):
    ctx.run(["apt-get", "install", "-y", "-qq", "--no-install-recommends", "smartmontools", "smbclient"],
            env={"DEBIAN_FRONTEND": "noninteractive", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
