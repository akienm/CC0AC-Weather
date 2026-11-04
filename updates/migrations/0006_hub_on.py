"""The hub on for a station that records nothing. Before 1.4.2 a Docker, Windows or Mac
install started with [hub] enabled = false, and nothing on the setup page could turn it
on, so a hub pointed at the station by its server name went unheard. A station with the
radio ([capture]) on was set up on purpose and is left alone."""
import configparser
import sys

PLATFORMS = ("pi", "debian", "docker", "windows", "macos")


def apply(ctx):
    if not ctx.config.exists():
        return
    text = ctx.config.read_text()
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    cfg.read_string(text)
    if cfg.getboolean("hub", "enabled", fallback=True) is True or cfg.getboolean("capture", "enabled", fallback=False):
        return
    sys.path.insert(0, str(ctx.version_dir))
    try:
        from cc0ac import wizard
    finally:
        sys.path.pop(0)
    ctx.config.write_text(wizard.set_values(text, "hub", {"enabled": "true"}))
    ctx.log.info("  [hub] switched on: nothing else was recording")
