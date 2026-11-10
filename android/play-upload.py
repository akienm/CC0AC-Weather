#!/usr/bin/env python3
"""Upload the app bundle to Google Play.

    android/play-upload.py                      # build/cc0ac-weather.aab to internal testing
    android/play-upload.py --track production --notes "Kiosk mode from the phone."
    android/play-upload.py --dry-run            # everything but the commit: Play checks it, nothing changes

It talks to the Google Play Developer API as a service account: open an edit, upload the
bundle, put it on a track, commit. Python's standard library and openssl only.

Once, by hand, before this can work:
  1. Create the app in the Play Console and upload its first .aab there; the API can't
     create an app.
  2. In Google Cloud, turn on the "Google Play Android Developer API", make a service
     account, and download its JSON key to ~/.cc0ac-weather/play-service-account.json
     (or give --key, or set CC0AC_PLAY_KEY). Keep it 0600 and out of every repo.
  3. In the Play Console, Users and permissions: invite the service account's email with
     permission to release this app.

An app Play hasn't published yet takes only draft releases: use --status draft until it has.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PACKAGE = "org.cc0ac.weatherkiosk"
HERE = Path(__file__).resolve().parent
API = os.environ.get("CC0AC_PLAY_API", "https://androidpublisher.googleapis.com")   # a test points this elsewhere
SCOPE = "https://www.googleapis.com/auth/androidpublisher"
TRACKS = ("internal", "alpha", "beta", "production")   # alpha is closed testing, beta open testing


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_jwt(key: dict, now: int) -> str:
    """The service account's login: a JWT signed with its private key (RS256, by openssl)."""
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64url(json.dumps({"iss": key["client_email"], "scope": SCOPE, "aud": key["token_uri"],
                                "iat": now, "exp": now + 3600}).encode())
    body = f"{head}.{claims}".encode()
    with tempfile.TemporaryDirectory() as d:   # the key touches disk only here, 0600, removed at once
        pem = Path(d) / "k.pem"
        pem.touch(mode=0o600)
        pem.write_text(key["private_key"])
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(pem)],
                             input=body, capture_output=True, check=True).stdout
    return f"{head}.{claims}.{b64url(sig)}"


def call(method: str, url: str, token: str = "", data: bytes = None, kind: str = "application/json"):
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", kind)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            text = r.read().decode()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Play said {e.code} to {method} {url.split('?')[0]}: {e.read().decode()[:2000]}")
    return json.loads(text) if text.strip() else {}


def access_token(key: dict) -> str:
    form = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": signed_jwt(key, int(time.time()))}).encode()
    return call("POST", key["token_uri"], data=form, kind="application/x-www-form-urlencoded")["access_token"]


def upload(aab: Path, key: dict, track: str, status: str, notes: str, dry_run: bool, say=print) -> int:
    token = access_token(key)
    app = f"{API}/androidpublisher/v3/applications/{PACKAGE}"
    edit = call("POST", f"{app}/edits", token, b"{}")["id"]
    done = False
    try:
        say(f"Uploading {aab.name} ({aab.stat().st_size // 1024} KB) …")
        got = call("POST", f"{API}/upload/androidpublisher/v3/applications/{PACKAGE}/edits/{edit}/bundles"
                           "?uploadType=media", token, aab.read_bytes(), "application/octet-stream")
        code = got["versionCode"]
        release = {"versionCode": [str(code)], "status": status}
        if notes:
            release["releaseNotes"] = [{"language": "en-US", "text": notes}]
        call("PUT", f"{app}/edits/{edit}/tracks/{track}", token,
             json.dumps({"track": track, "releases": [release]}).encode())
        if dry_run:
            call("POST", f"{app}/edits/{edit}:validate", token, b"{}")
            say(f"Dry run: Play accepts version {code} on {track} ({status}). Nothing was released.")
            return code
        call("POST", f"{app}/edits/{edit}:commit", token, b"{}")
        done = True
        say(f"Version {code} is on {track} ({status}).")
        return code
    finally:
        if not done:   # a dry run, or a failure: leave no edit open in the Console
            try:
                call("DELETE", f"{app}/edits/{edit}", token)
            except SystemExit:
                pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Upload the app bundle to Google Play.")
    p.add_argument("--aab", type=Path, default=HERE / "build" / "cc0ac-weather.aab")
    p.add_argument("--key", type=Path, default=Path(os.environ.get(
        "CC0AC_PLAY_KEY", Path.home() / ".cc0ac-weather" / "play-service-account.json")))
    p.add_argument("--track", choices=TRACKS, default="internal")
    p.add_argument("--status", choices=("completed", "draft"), default="completed")
    p.add_argument("--notes", default="", help="what's new, shown in Play (500 characters at most)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if not a.aab.is_file():
        p.error(f"no bundle at {a.aab}: run ./build.sh first (it needs bundletool for the .aab)")
    if not a.key.is_file():
        p.error(f"no service account key at {a.key}; see the top of this file for making one")
    if len(a.notes) > 500:
        p.error(f"--notes is {len(a.notes)} characters; Play takes 500")
    upload(a.aab, json.loads(a.key.read_text()), a.track, a.status, a.notes, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
