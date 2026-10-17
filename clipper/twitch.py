"""Twitch: clip a live stream where its chat explodes, and list the moments
viewers already clipped from past broadcasts - through Twitch's own API.

WHAT TWITCH ALLOWS (its API reference, read 2026-09-30):

  Create Clip   any signed-in account (scope clips:edit), a LIVE stream only.
                Twitch keeps about 90 s around the call and publishes the
                last `duration` seconds (5-60) of it, under your name.
  Get Clips     any app: a channel's clips, most viewed first, each with its
                past broadcast (video_id) and where in it (vod_offset).
  Create Clip From VOD, Get Clips Download
                the streamer or their editors only. So Clip cannot clip a
                past broadcast or download a clip. You do those two: from
                the list of moments this writes, and from your Creator
                Dashboard (Content -> Clips -> Share -> Download).

CHAT is read over Twitch's IRC anonymously - read-only, like a logged-out
viewer - so there is no scope for it and nothing a lapsed sign-in can break.
Verified 2026-09-30: the anonymous login is welcomed and JOIN works
(tests/fixtures/twitch-irc-chat.txt is that session, names blanked).

SIGNING IN, ONCE, with Twitch's device flow for a "Public" app, which has
no secret: `clipper twitch-login` prints a code for twitch.tv/activate. The
refresh token it earns is ONE-TIME USE and lapses after 30 days unused
(Twitch's docs), so every refresh writes the new one back at once, under a
lock: two Clip processes spending the same token would sign Clip out.
"""

import fcntl
import json
import re
import socket
import ssl
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from clipper import config, db, youtube

API = "https://api.twitch.tv/helix"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPES = "clips:edit"
IRC_HOST, IRC_PORT = "irc.chat.twitch.tv", 6697

# Twitch asks apps that keep a sign-in to check it hourly.
VALIDATE_EVERY = 3600
# Is the stream still on? Asked this often while watching.
LIVE_CHECK = 300
# How long after asking for a clip Twitch has usually made it; after
# CONFIRM_GIVE_UP with no clip, Twitch's docs say assume it failed.
CONFIRM_AFTER, CONFIRM_GIVE_UP = 20, 90
# How long an explosion is measured after chat first crosses the bar. The
# crossing itself is always "just over 3x" - the first night's eight clips
# were all 3.04x-3.18x - so ranking on it ranks nothing. The peak in the
# seconds after is how big the moment really was.
PEAK_WINDOW = 20


def say_now(line):
    """print, flushed: under systemd stdout is a pipe and Python holds it
    until exit - the first night's log arrived all at once at 05:12."""
    print(line, flush=True)


class NotSetUp(RuntimeError):
    pass


class TwitchError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"Twitch {status}: {message}")
        self.status, self.message = status, message


# ------------------------------------------------------------------ signing in

def creds():
    return youtube.creds()


def _save(key, value):
    """The one writer for credentials.env (set-credential.py's), mode 600."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "set_credential_tw", config.ROOT / "scripts" / "set-credential.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.write(config.home() / "credentials.env", key, value)


@contextmanager
def _token_lock():
    path = config.home() / "twitch-token.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _message(d):
    """Twitch's OAuth errors come as {"status": 400, "message": "..."}."""
    return str(d.get("message") or d.get("error_description") or d.get("error") or d)


def _keep(t, now):
    """A refresh token first: it is the one that cannot be got back."""
    _save("TWITCH_REFRESH_TOKEN", t["refresh_token"])
    _save("TWITCH_ACCESS_TOKEN", t["access_token"])
    _save("TWITCH_ACCESS_EXPIRES", str(int(now() + int(t.get("expires_in") or 0))))


def login(post=youtube._post, sleep=time.sleep, say=print, now=time.time):
    cid = creds().get("TWITCH_CLIENT_ID")
    if not cid:
        raise NotSetUp("no TWITCH_CLIENT_ID - see clipper/README.md, 'Twitch'")
    code, d = post(DEVICE_URL, {"client_id": cid, "scopes": SCOPES})
    if code != 200 or "device_code" not in d:
        raise RuntimeError(f"Twitch refused the sign-in request ({code}): {_message(d)} - is the "
                           f"app's Client Type 'Public'?")
    say(f"On your iPad, open {d.get('verification_uri', 'https://www.twitch.tv/activate')}\n"
        f"and check the code there is:  {d['user_code']}\n"
        f"Waiting for you to approve (this code lasts {int(d.get('expires_in', 1800)) // 60} minutes)...")
    interval = int(d.get("interval", 5))
    deadline = now() + int(d.get("expires_in", 1800))
    while now() < deadline:
        sleep(interval)
        code, t = post(TOKEN_URL, {"client_id": cid, "scopes": SCOPES,
                                   "device_code": d["device_code"], "grant_type": GRANT})
        if code == 200 and t.get("refresh_token"):
            with _token_lock():
                _keep(t, now)
            say("Signed in. Clip can make Twitch clips as you.")
            return True
        why = _message(t)
        if why == "authorization_pending":
            continue
        if why == "slow_down":
            interval += 5
            continue
        raise RuntimeError(f"Twitch did not sign in ({code}): {why}")
    raise RuntimeError("the code expired before it was approved - run twitch-login again")


def access_token(post=youtube._post, now=time.time, force=False):
    """A working access token, refreshed only when it is about to lapse.
    Under the lock, and re-read inside it: another process may have just
    spent the refresh token this one read."""
    with _token_lock():
        c = creds()
        if not c.get("TWITCH_CLIENT_ID") or not c.get("TWITCH_REFRESH_TOKEN"):
            raise NotSetUp("Twitch is not set up - see clipper/README.md, 'Twitch'")
        if (not force and c.get("TWITCH_ACCESS_TOKEN")
                and float(c.get("TWITCH_ACCESS_EXPIRES") or 0) > now() + 300):
            return c["TWITCH_ACCESS_TOKEN"]
        code, t = post(TOKEN_URL, {"client_id": c["TWITCH_CLIENT_ID"], "grant_type": "refresh_token",
                                   "refresh_token": c["TWITCH_REFRESH_TOKEN"]})
        if code != 200 or not t.get("access_token") or not t.get("refresh_token"):
            if code in (400, 401):
                raise NotSetUp(f"the Twitch sign-in has lapsed or was removed ({_message(t)}) - "
                               f"run: clipper/.venv/bin/python -m clipper twitch-login")
            raise RuntimeError(f"Twitch token refresh failed ({code}): {_message(t)}")
        _keep(t, now)
        return t["access_token"]


# ------------------------------------------------------------------ the API

def _send(req, timeout=30):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"message": str(e)}


class Api:
    """The calls Clip makes to Twitch. `send` and `token` are swappable so tests
    never touch the network."""

    def __init__(self, send=_send, token=access_token, now=time.time):
        self.send, self.token, self.now = send, token, now
        self.validated = 0.0

    def _call(self, method, path, params):
        for attempt in (1, 2):
            tok = self.token(force=attempt == 2)
            if self.now() - self.validated > VALIDATE_EVERY:
                code, _ = self.send(urllib.request.Request(
                    VALIDATE_URL, headers={"Authorization": f"OAuth {tok}"}))
                if code == 401 and attempt == 1:
                    continue
                self.validated = self.now()
            url = f"{API}{path}?{urllib.parse.urlencode(params, doseq=True)}"
            code, d = self.send(urllib.request.Request(url, method=method, headers={
                "Authorization": f"Bearer {tok}", "Client-Id": creds().get("TWITCH_CLIENT_ID", "")}))
            if code == 401 and attempt == 1:
                continue
            if code >= 400:
                raise TwitchError(code, _message(d))
            return d
        raise TwitchError(401, "the sign-in was refused twice")

    def me(self):
        return self._call("GET", "/users", {})["data"][0]

    def user(self, login):
        data = self._call("GET", "/users", {"login": login})["data"]
        return data[0] if data else None

    def stream(self, login):
        """The live stream, or None when the channel is offline."""
        data = self._call("GET", "/streams", {"user_login": login, "type": "live"})["data"]
        return data[0] if data else None

    def create_clip(self, broadcaster_id, duration):
        return self._call("POST", "/clips", {"broadcaster_id": broadcaster_id,
                                             "duration": duration})["data"][0]

    def videos(self, user_id):
        """The channel's past broadcasts, newest first."""
        return self._call("GET", "/videos", {"user_id": user_id, "type": "archive", "first": 20})["data"]

    def clips(self, **params):
        return self._call("GET", "/clips", params)["data"]


# ------------------------------------------------------------------ chat

LINE = re.compile(r"^(?:@\S+ )?(?::(?P<prefix>\S+) )?(?P<cmd>\S+)(?: (?P<args>.*))?$")


def parse(line):
    """(command, text) from one IRC line: ("PRIVMSG", message),
    ("PING", its argument), ("NOTICE", why), ("RECONNECT", ""), or the
    command with its raw arguments for the ones Clip ignores."""
    m = LINE.match(line.rstrip("\r\n"))
    if not m:
        return None, ""
    cmd, args = m["cmd"], m["args"] or ""
    if cmd in ("PRIVMSG", "NOTICE"):
        return cmd, args.split(" :", 1)[1] if " :" in args else ""
    if cmd == "PING":
        return cmd, args.lstrip(":")
    return cmd, args


def irc_messages(channel, now=time.time, tick=5.0, say=say_now, host=IRC_HOST, port=IRC_PORT,
                 context=None, sleep=time.sleep):
    """(time, message) for each chat message, and (time, None) at least
    every `tick` seconds when chat is quiet, forever - reconnecting when
    Twitch asks or the connection drops. Anonymous and read-only."""
    backoff = 1
    while True:
        try:
            raw = socket.create_connection((host, port), timeout=30)
            sock = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=host)
        except OSError as exc:
            say(f"chat: cannot connect ({exc}) - trying again in {backoff}s")
            yield now(), None
            sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        try:
            sock.settimeout(tick)
            nick = f"justinfan{int(now()) % 80000 + 10000}"
            sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel.lower()}\r\n".encode())
            buf = b""
            while True:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    yield now(), None
                    continue
                if not data:
                    raise ConnectionError("Twitch closed the chat connection")
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw_line in lines:
                    cmd, text = parse(raw_line.decode("utf-8", "replace"))
                    if cmd == "PING":
                        sock.sendall(f"PONG :{text}\r\n".encode())
                    elif cmd == "PRIVMSG":
                        backoff = 1
                        yield now(), text
                    elif cmd == "RECONNECT":
                        raise ConnectionError("Twitch asked to reconnect")
                yield now(), None
        except OSError as exc:            # ConnectionError and ssl errors are OSErrors
            say(f"chat: {exc} - reconnecting in {backoff}s")
            yield now(), None
            sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            sock.close()


class Chat:
    """How fast chat is moving, and whether it just exploded.

    'Now' is the last NOW seconds. 'Usual' is the median BUCKET-second count
    over the USUAL seconds before that - a median, so one earlier burst does
    not raise the bar for the next. Nothing fires in the first WARMUP
    seconds: there is no 'usual' yet."""

    BUCKET, NOW, USUAL, WARMUP = 5, 10, 300, 120

    def __init__(self, ratio=3.0, min_rate=2.0):
        self.ratio, self.min_rate = ratio, min_rate
        self.msgs = deque()        # (time, message)
        self.first = None

    def add(self, t, text):
        if self.first is None:
            self.first = t
        self.msgs.append((t, text))
        while self.msgs and self.msgs[0][0] < t - self.NOW - self.USUAL:
            self.msgs.popleft()

    def rates(self, t):
        """(messages a second now, usual messages a second)."""
        recent = [m for m in self.msgs if m[0] >= t - self.NOW]
        start = t - self.NOW - self.USUAL
        counts = [0] * (self.USUAL // self.BUCKET)
        for m_t, _ in self.msgs:
            if start <= m_t < t - self.NOW:
                counts[int((m_t - start) // self.BUCKET)] += 1
        # Only buckets chat has existed for: the minutes before Clip joined
        # were not quiet, they were unwatched.
        seen = counts[max(0, int(((self.first or t) - start) // self.BUCKET)):]
        usual = statistics.median(seen) / self.BUCKET if seen else 0.0
        return len(recent) / self.NOW, usual

    def measure(self, t):
        """What chat is doing now: its speed, its usual speed, its words."""
        rate, usual = self.rates(t)
        return {"rate": round(rate, 2), "usual": round(usual, 2), "words": self.words(t)}

    def spike(self, t):
        """None, or what chat was doing when it exploded."""
        if self.first is None or t - self.first < self.WARMUP:
            return None
        m = self.measure(t)
        if m["rate"] < self.min_rate or m["rate"] < self.ratio * m["usual"]:
            return None
        return m

    def words(self, t, n=5):
        """What chat was saying: the commonest words in the last NOW
        seconds, each counted once per message ("LUL LUL LUL" is one LUL)."""
        c = Counter()
        for m_t, text in self.msgs:
            if m_t >= t - self.NOW:
                c.update(set(text.split()))
        return c.most_common(n)


# ------------------------------------------------------------------ watching

def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def vod_link(video_id, offset):
    """A past broadcast at a moment: twitch.tv/videos/<id>?t=1h02m03s."""
    m, s = divmod(int(offset or 0), 60)
    h, m = divmod(m, 60)
    return f"https://www.twitch.tv/videos/{video_id}?t={h}h{m:02d}m{s:02d}s"


def make_clip(conn, api, login, broadcaster_id, cfg, what, stream):
    """Ask Twitch for a clip of the last twitch_clip_seconds. Returns its id,
    or None when Twitch refused (recorded as an event, with the reason)."""
    try:
        c = api.create_clip(broadcaster_id, cfg["twitch_clip_seconds"])
    except TwitchError as exc:
        hint = (" - follow the channel on Twitch (it may only let followers clip)"
                if exc.status == 403 else "")
        db.event(conn, "twitch", f"{login}: Twitch would not make a clip: {exc.message}{hint}",
                 level="warn")
        return None
    with conn:
        conn.execute("INSERT OR REPLACE INTO twitch_clips (id, channel, edit_url, status, chat_rate, "
                     "chat_usual, chat_words, stream_started_at, created_at) "
                     "VALUES (?, ?, ?, 'asked', ?, ?, ?, ?, ?)",
                     (c["id"], login, c.get("edit_url"), what["rate"], what["usual"],
                      db.as_json(what["words"]), stream.get("started_at"), db.now()))
    db.event(conn, "twitch", f"{login}: chat hit {what['rate']}/s (usual {what['usual']}/s) - "
                             f"asked Twitch for clip {c['id']}")
    return c["id"]


def confirm(conn, api, ids, give_up=False):
    """Mark asked-for clips made once Twitch lists them - or failed, when
    give_up and it still does not. Returns the ids still unconfirmed."""
    if not ids:
        return []
    found = {c["id"]: c for c in api.clips(id=list(ids))}
    left = []
    with conn:
        for i in ids:
            if i in found:
                conn.execute("UPDATE twitch_clips SET status = 'made', url = ? WHERE id = ?",
                             (found[i]["url"], i))
            elif give_up:
                conn.execute("UPDATE twitch_clips SET status = 'failed', detail = ? WHERE id = ?",
                             (f"Twitch never listed it within {CONFIRM_GIVE_UP}s", i))
            else:
                left.append(i)
    return left


def refresh_moments(conn, api, login, broadcaster_id, hours, me=None, now=time.time):
    """The channel's most-viewed clips from the last `hours`, made by other
    viewers, as moments of past broadcasts for you to clip yourself. Two
    clips of the same moment (the same broadcast, starting within 30 s) are
    one moment. Returns how many are kept."""
    got = api.clips(broadcaster_id=broadcaster_id, started_at=_iso(now() - hours * 3600),
                    ended_at=_iso(now()), first=50)
    kept = []
    for c in sorted(got, key=lambda c: -(c.get("view_count") or 0)):
        if me and c.get("creator_id") == me:
            continue
        if not c.get("video_id") or c.get("vod_offset") is None:
            continue
        if any(k["video_id"] == c["video_id"] and abs(k["vod_offset"] - c["vod_offset"]) < 30
               for k in kept):
            continue
        kept.append(c)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute("DELETE FROM twitch_moments WHERE channel = ? AND (created_at < ? OR created_at IS NULL)",
                     (login, cutoff))
        for c in kept:
            conn.execute("INSERT OR REPLACE INTO twitch_moments (id, channel, title, views, creator, "
                         "video_id, vod_offset, duration, created_at, fetched_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (c["id"], login, c.get("title"), c.get("view_count"), c.get("creator_name"),
                          c["video_id"], c["vod_offset"], c.get("duration"), c.get("created_at"),
                          db.now()))
    return len(kept)


def jump(clip):
    """How many times faster than usual chat got at a clip's peak - the one
    measure clips are ranked by. Raw speed would favour the end of every
    stream, when the most people are watching. Takes a twitch_clips row or
    a Chat.measure() reading."""
    rate = clip["chat_rate"] if "chat_rate" in clip.keys() else clip["rate"]
    usual = clip["chat_usual"] if "chat_usual" in clip.keys() else clip["usual"]
    return (rate or 0) / max(usual or 0, 0.1)


def best_of(clips, keep):
    """Clips of one stream, best first, each with "best" (in the top `keep`)
    and "jump". Clips Twitch never made are not ranked."""
    made = sorted((dict(c) for c in clips if c["status"] == "made"), key=jump, reverse=True)
    return [dict(c, best=i < keep, jump=round(jump(c), 2)) for i, c in enumerate(made)]


def record_peak(conn, peak, say):
    """A clip's chat numbers become the peak of its explosion."""
    if not peak["cid"]:
        return
    b = peak["best"]
    with conn:
        conn.execute("UPDATE twitch_clips SET chat_rate = ?, chat_usual = ?, chat_words = ? WHERE id = ?",
                     (b["rate"], b["usual"], db.as_json(b["words"]), peak["cid"]))
    say(f"  {peak['cid']} peaked at {b['rate']}/s, {jump(b):.1f}x usual")


def watch(conn, cfg, source, api=None, chat=irc_messages, now=time.time, say=say_now):
    """Watch one live stream's chat until the stream ends, clipping where it
    explodes. Returns {"live": bool, "clips": n}. Offline is not an error:
    the timer asks again in a few minutes."""
    login = source["twitch"]
    api = api or Api()
    stream = api.stream(login)
    if not stream:
        return {"live": False, "clips": 0}
    bid = stream["user_id"]
    say(f"{login} is live ({(stream.get('title') or '')[:60]}) - watching chat")
    db.event(conn, "twitch", f"{login} is live - watching chat")
    meter = Chat(cfg["twitch_spike_ratio"], cfg["twitch_spike_min_rate"])
    made, last_clip, pending, asked = 0, float("-inf"), None, {}   # asked: clip id -> when
    peak = None          # the explosion being measured: {"until", "best", "cid"}
    next_look = next_confirm = 0.0
    next_live = now() + LIVE_CHECK
    for t, text in chat(login):
        if text is not None:
            meter.add(t, text)
        # Once a second is plenty: a busy chat is dozens of messages a second.
        if t >= next_look:
            next_look = t + 1
            if peak:
                m = meter.measure(t)
                if jump(m) > jump(peak["best"]):
                    peak["best"] = m
            elif pending is None and made < cfg["twitch_max_clips_per_stream"] \
                    and t - last_clip >= cfg["twitch_cooldown_seconds"]:
                what = meter.spike(t)
                if what:
                    # Wait a moment: the reaction is half of what makes it a clip.
                    pending = t + cfg["twitch_clip_delay_seconds"]
                    peak = {"until": t + PEAK_WINDOW, "best": what, "cid": None}
        if pending and t >= pending:
            made, last_clip, pending = made + 1, t, None
            cid = make_clip(conn, api, login, bid, cfg, peak["best"], stream)
            say(f"clip {made}: {cid or 'Twitch refused - see clipper status'} "
                f"(chat {peak['best']['rate']}/s so far, usual {peak['best']['usual']}/s)")
            peak["cid"] = cid
            if cid:
                asked[cid] = t
        if peak and pending is None and t >= peak["until"]:
            record_peak(conn, peak, say)
            peak = None
        if asked and t >= next_confirm:
            next_confirm = t + 10
            due = [i for i, at in asked.items() if t - at >= CONFIRM_AFTER]
            late = [i for i in due if t - asked[i] >= CONFIRM_GIVE_UP]
            left = set(confirm(conn, api, [i for i in due if i not in late]))
            left |= set(confirm(conn, api, late, give_up=True))
            asked = {i: at for i, at in asked.items() if i not in due or i in left}
        if t >= next_live:
            next_live = t + LIVE_CHECK
            if not api.stream(login):
                break
    if peak:
        record_peak(conn, peak, say)
    confirm(conn, api, list(asked), give_up=True)
    try:
        n = refresh_moments(conn, api, login, bid, cfg["twitch_moments_hours"], api.me()["id"], now)
    except TwitchError as exc:
        n = 0
        db.event(conn, "twitch", f"{login}: could not list past moments: {exc.message}", level="warn")
    try:
        link_to_broadcast(conn, api, login, bid, cfg)
    except TwitchError as exc:
        db.event(conn, "twitch", f"{login}: could not find the past broadcast: {exc.message}", level="warn")
    ranked = best_of(conn.execute("SELECT * FROM twitch_clips WHERE channel = ? AND stream_started_at = ?",
                                  (login, stream.get("started_at"))).fetchall(), cfg["twitch_keep_best"])
    top = [c for c in ranked if c["best"]]
    db.event(conn, "twitch", f"{login} went offline: {made} clip(s) asked for, {len(ranked)} made; "
                             f"best {len(top)}: " + ", ".join(f"{c['jump']}x" for c in top)
             + f"; {n} past moment(s) listed")
    return {"live": True, "clips": made}


# A past broadcast starts when its stream does; Twitch's created_at for the
# two can differ by a little. Anything further apart is another stream.
SAME_STREAM = 15 * 60


def _utc(ts):
    """Seconds since the epoch from Twitch's RFC 3339 or SQLite's UTC text."""
    ts = str(ts).strip().replace("T", " ").rstrip("Z")
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def archive_for(videos, started_at):
    """The past broadcast of the stream that started at `started_at`, or None."""
    best = min(videos, key=lambda v: abs(_utc(v["created_at"]) - _utc(started_at)), default=None)
    if best and abs(_utc(best["created_at"]) - _utc(started_at)) <= SAME_STREAM:
        return best
    return None


def link_to_broadcast(conn, api, login, user_id, cfg):
    """Give each of a channel's watcher clips its moment in the past
    broadcast: the same minute the watcher clipped, which ends when the clip
    was asked for. Returns how many were linked."""
    rows = conn.execute("SELECT * FROM twitch_clips WHERE channel = ? AND vod_id IS NULL "
                        "AND stream_started_at IS NOT NULL", (login,)).fetchall()
    if not rows:
        return 0
    videos, n = api.videos(user_id), 0
    with conn:
        for r in rows:
            vod = archive_for(videos, r["stream_started_at"])
            if not vod:
                continue
            offset = _utc(r["created_at"]) - _utc(vod["created_at"]) - cfg["twitch_clip_seconds"]
            conn.execute("UPDATE twitch_clips SET vod_id = ?, vod_offset = ? WHERE id = ?",
                         (vod["id"], max(0, int(offset)), r["id"]))
            n += 1
    return n


def moments(conn, cfg, api=None, now=time.time):
    """Refresh the past-broadcast moments of every watched channel, live or
    not. Returns {channel: moments kept}."""
    api = api or Api()
    me = api.me()["id"]
    out = {}
    for src in watched(conn):
        user = api.user(src["twitch"])
        if not user:
            raise ValueError(f"Twitch has no channel called {src['twitch']!r}")
        out[src["twitch"]] = refresh_moments(conn, api, src["twitch"], user["id"],
                                             cfg["twitch_moments_hours"], me, now)
        link_to_broadcast(conn, api, src["twitch"], user["id"], cfg)
    return out


def watched(conn):
    return conn.execute("SELECT * FROM sources WHERE active = 1 AND twitch IS NOT NULL "
                        "ORDER BY id").fetchall()
