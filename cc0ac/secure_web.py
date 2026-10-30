"""
secure_web.py — https:// on the dashboard's own port, and a DuckDNS name for it.

With a certificate in [web], the web server answers both ways on the same port:
a connection that opens with a TLS handshake (first byte 0x16) is wrapped with
the certificate, and anything else is plain HTTP as before, so old bookmarks and
the house kiosks keep working. The certificate files are re-read when they
change, so a renewal (acme.sh, certbot, anything that rewrites them) takes
effect without a restart.

[duckdns] keeps <domain>.duckdns.org pointed at this house: every few minutes it
asks DuckDNS to use the address the request came from. README: "https and a
name of your own".
"""

from __future__ import annotations

import configparser
import logging
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("acurite")

TLS_HANDSHAKE = b"\x16"
HANDSHAKE_TIMEOUT = 15      # seconds a client may sit silent before saying which it speaks
DUCKDNS = "https://www.duckdns.org/update"


class Certificate:
    """The certificate and key in [web], loaded again whenever either file changes."""

    def __init__(self, cert: Path, key: Path):
        self.cert, self.key = cert, key
        self._stamp: tuple[int, int] | str | None = None
        self._context: ssl.SSLContext | None = None
        self._lock = threading.Lock()

    def context(self) -> ssl.SSLContext | None:
        """The TLS context to answer with, or None while there is no usable certificate."""
        try:
            stamp: tuple[int, int] | str = (os.stat(self.cert).st_mtime_ns, os.stat(self.key).st_mtime_ns)
        except OSError as e:
            stamp = str(e)
        with self._lock:
            if stamp != self._stamp:
                self._stamp = stamp     # a bad file is reported once, not on every connection
                try:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(self.cert, self.key)
                except (OSError, ssl.SSLError) as e:
                    log.error("https: cannot load the certificate %s: %s%s", self.cert, e,
                              "; still using the one loaded before" if self._context else "; plain http only")
                else:
                    log.info("https: certificate %s loaded", self.cert)
                    self._context = context
            return self._context


def certificate(cfg: configparser.ConfigParser) -> Certificate | None:
    cert = cfg.get("web", "certificate", fallback="").strip()
    key = cfg.get("web", "key", fallback="").strip()
    if not cert:
        return None
    if not key:
        log.error("https: [web] certificate is set but key is not; plain http only")
        return None
    return Certificate(Path(cert).expanduser(), Path(key).expanduser())


class WebServer(ThreadingHTTPServer):
    """Plain HTTP, or HTTPS when a connection opens with a TLS handshake.

    The look at the first byte and the handshake happen in the connection's own
    thread, so a client that connects and says nothing holds up only itself.
    """
    # The stock queue holds 5 waiting connections; a phone opening a page plus its charts
    # can briefly be more than that, and the kernel then logs it as a SYN flood.
    request_queue_size = 64
    certificate: Certificate | None = None

    def finish_request(self, request, client_address):
        if self.certificate is not None:
            request.settimeout(HANDSHAKE_TIMEOUT)
            context = self.certificate.context()
            if context is not None and request.recv(1, socket.MSG_PEEK) == TLS_HANDSHAKE:
                request = context.wrap_socket(request, server_side=True)
            request.settimeout(None)
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        # A browser giving up on a connection, or a scanner's bad handshake, is not a fault here.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, ssl.SSLError)):
            log.debug("Web connection from %s dropped: %s", client_address[0], sys.exc_info()[1])
        else:
            super().handle_error(request, client_address)


def duckdns_update(domain: str, token: str, opener=urllib.request.urlopen) -> str:
    """One update; DuckDNS's answer in a word: OK, KO (name or token wrong), or why it failed."""
    url = f"{DUCKDNS}?{urllib.parse.urlencode({'domains': domain, 'token': token})}"
    try:
        with opener(url, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace").strip().split("\n")[0] or "empty answer"
    except OSError as e:
        return f"unreachable ({type(e).__name__})"     # never the address: it carries the token


def run_duckdns(cfg: configparser.ConfigParser, opener=urllib.request.urlopen, sleep=time.sleep) -> None:
    domain = cfg.get("duckdns", "domain", fallback="").strip().lower().removesuffix(".duckdns.org")
    token = cfg.get("duckdns", "token", fallback="").strip()
    if not domain or not token:
        log.error("[duckdns] needs both domain and token; not updating")
        return
    every = 60 * max(1, cfg.getint("duckdns", "every_minutes", fallback=5))
    said = None
    while True:
        answer = duckdns_update(domain, token, opener)
        if answer != said:      # each change once, not every five minutes
            if answer == "OK":
                log.info("DuckDNS: the name points here")   # not the name: logs go into diagnostics files
            elif answer == "KO":
                log.error("DuckDNS refused the update: check [duckdns] domain and token")
            else:
                log.warning("DuckDNS update failed: %s", answer)
            said = answer
        sleep(every)
