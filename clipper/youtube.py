"""Posting to YouTube Shorts through the official Data API.

SIGNING IN, ONCE. The droplet has no browser, so this uses Google's flow for
devices without one: `python3 -m clipper youtube-login` prints a short code,
you enter it at google.com/device on the iPad, and the refresh token it
earns is written to clipper/var/credentials.env (mode 600) by
set-credential.py's own writer. The flow only allows the full `youtube`
scope (checked in Google's documentation 2026-09-28), which covers uploads.

UPLOADING. The resumable protocol: one POST with the metadata returns an
upload URL, one PUT sends the file. A vertical video under three minutes is
a Short; "#Shorts" goes in the description as well.

THE LIMIT GOOGLE SETS. Uploads from a Google Cloud project that has not
passed YouTube's API audit are kept private, whatever privacy is asked for.
The privacy YouTube actually applied is read from its reply and recorded, so
the page never says "public" about a private video.

The request and reply shapes here follow Google's documentation; there was
no Google account to test against when this was written. The first
`youtube-login` and the first approved clip are the checks.
"""

import importlib.util
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from clipper import config

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")
SCOPE = "https://www.googleapis.com/auth/youtube"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class UploadLimit(RuntimeError):
    """YouTube's own cap on how many videos the channel may upload in a day -
    not ours. Seen 2026-10-08: "(400): The user has exceeded the number of
    videos they may upload." Waiting is the only fix; publish holds the post."""


LIMIT_REASON = "uploadLimitExceeded"
LIMIT_TEXT = "exceeded the number of videos"


def _reasons(raw):
    try:
        return [e.get("reason") for e in (json.loads(raw).get("error") or {}).get("errors") or []]
    except Exception:
        return []


class NotSetUp(RuntimeError):
    pass


def creds():
    from clipper import trends
    return trends._read_env_file(config.home() / "credentials.env")


def _post(url, fields, timeout=30):
    """(status, json) - an HTTP error's JSON body is an answer, not a crash."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": str(e)}


def save_refresh_token(token):
    """Written by set-credential.py's own writer: mode 600 before anything
    is in the file, and every other key in it kept."""
    spec = importlib.util.spec_from_file_location(
        "set_credential_yt", config.ROOT / "scripts" / "set-credential.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.write(config.home() / "credentials.env", "YOUTUBE_REFRESH_TOKEN", token)


def login(post=_post, sleep=time.sleep, say=print):
    """The device flow. Returns once a refresh token is saved."""
    c = creds()
    cid, secret = c.get("YOUTUBE_CLIENT_ID"), c.get("YOUTUBE_CLIENT_SECRET")
    if not cid or not secret:
        raise NotSetUp("no YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET - see clipper/README.md, "
                       "'Posting to YouTube'")
    code, d = post(DEVICE_URL, {"client_id": cid, "scope": SCOPE})
    if code != 200 or "device_code" not in d:
        raise RuntimeError(f"Google refused the sign-in request ({code}): {d.get('error_description') or d}")
    say(f"On your iPad, open {d.get('verification_url', 'https://www.google.com/device')} "
        f"and enter:  {d['user_code']}\nWaiting for you to approve (this code lasts "
        f"{int(d.get('expires_in', 1800)) // 60} minutes)...")
    interval = int(d.get("interval", 5))
    deadline = time.time() + int(d.get("expires_in", 1800))
    while time.time() < deadline:
        sleep(interval)
        code, t = post(TOKEN_URL, {"client_id": cid, "client_secret": secret,
                                   "device_code": d["device_code"], "grant_type": GRANT})
        err = t.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            raise RuntimeError({"access_denied": "you declined on the Google page",
                                "expired_token": "the code expired - run youtube-login again"}
                               .get(err, f"{err}: {t.get('error_description', '')}"))
        if not t.get("refresh_token"):
            raise RuntimeError("Google signed in but sent no refresh token - "
                               "remove Clip's access at myaccount.google.com/permissions and retry")
        save_refresh_token(t["refresh_token"])
        say("Signed in. Clip can post to this YouTube channel.")
        return True
    raise RuntimeError("the code expired before it was approved - run youtube-login again")


def access_token(post=_post):
    c = creds()
    need = [k for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN") if not c.get(k)]
    if need:
        raise NotSetUp(f"YouTube is not set up: missing {', '.join(need)}")
    code, t = post(TOKEN_URL, {"client_id": c["YOUTUBE_CLIENT_ID"], "client_secret": c["YOUTUBE_CLIENT_SECRET"],
                               "refresh_token": c["YOUTUBE_REFRESH_TOKEN"], "grant_type": "refresh_token"})
    if code != 200 or not t.get("access_token"):
        if t.get("error") == "invalid_grant":
            raise NotSetUp("the YouTube sign-in has expired or was revoked - run "
                           "python3 -m clipper youtube-login")
        raise RuntimeError(f"token refresh failed ({code}): {t.get('error_description') or t}")
    return t["access_token"]


def _send(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def upload(path, meta, privacy, category, token=None, send=_send, timeout=600):
    """Upload one video. Returns {"id", "url", "privacy"} - privacy as
    YouTube applied it."""
    token = token or access_token()
    path = Path(path)
    size = path.stat().st_size
    body = {"snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": meta["tags"], "categoryId": str(category)},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    req = urllib.request.Request(UPLOAD_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    code, headers, raw = send(req, 60)
    where = headers.get("Location") or headers.get("location")
    if code != 200 or not where:
        if LIMIT_REASON in _reasons(raw) or LIMIT_TEXT in _why(raw):
            raise UploadLimit(f"YouTube's daily upload limit for this channel ({code}): {_why(raw)}")
        raise RuntimeError(f"YouTube refused the upload ({code}): {_why(raw)}")
    with open(path, "rb") as f:
        put = urllib.request.Request(where, data=f, method="PUT", headers={
            "Content-Type": "video/mp4", "Content-Length": str(size)})
        code, headers, raw = send(put, timeout)
    if code not in (200, 201):
        raise RuntimeError(f"YouTube did not accept the file ({code}): {_why(raw)}")
    v = json.loads(raw)
    return {"id": v["id"], "url": f"https://youtube.com/shorts/{v['id']}",
            "privacy": (v.get("status") or {}).get("privacyStatus") or privacy}


def _why(raw):
    try:
        e = json.loads(raw).get("error") or {}
        return (e.get("message") or str(e))[:300]
    except Exception:
        return (raw or b"")[:300].decode("utf-8", "replace")
