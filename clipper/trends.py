"""Spotter - Clip's research assistant.

Three questions, answered from YouTube's official Data API with a free key:

1. WHO is hot right now? The "most popular" charts, per category, collapsed
   to the channels on them, ranked by how fast their charting videos are
   gaining views. Only creators above `spot_min_subscribers` count - the brief
   is the top influencers, not everyone who charted once.
2. WHICH of their moments are being clipped? For the top creators, the
   most-viewed Shorts mentioning them from the last few days, with views per
   hour. Those are other people's clips, and they are evidence of demand.
3. IS THAT MOMENT IN FOOTAGE CLIP MAY USE? A trending clip's title and
   description are matched against the transcripts of videos Clip has
   ingested from that creator. A match is a moment Clip can cut ITSELF, from
   the permitted source, with its own edit and captions.

What Spotter never does is copy the trending clip. It does not download it,
re-upload it, or reuse its footage, edit or captions: those belong to
whoever made it, and reposting them is the thing TikTok and YouTube remove
accounts for. The trending clip tells Clip WHERE to look; the source it
cuts from is one the owner has permission for (rights.py). A creator
without a permitted source is still reported - as a lead to go and get
permission for, usually through their clipping campaign.

TikTok has no API that lists trending videos for an ordinary developer (its
Research API is for academic researchers), so research is YouTube-only.
YouTube Shorts demand is a fair proxy: the same moments trend on both.

Quota: each call's units are recorded in the cost ledger (usd 0, provider
youtube-data) and a run stops before passing `spot_daily_units`. A chart or
channel lookup is 1 unit; a search is 100.
"""

import importlib.util
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clipper import config, db, moments, score

API = "https://www.googleapis.com/youtube/v3/"
UNITS = {"videos": 1, "channels": 1, "search": 100}
PROVIDER = "youtube-data"


# ------------------------------------------------------------------ the key

def _read_env_file(path):
    """The repo's one credentials.env parser, imported rather than restated."""
    spec = importlib.util.spec_from_file_location(
        "emily_assets", config.ROOT / "scripts" / "emily-assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.read_env_file(path)


def api_key():
    return (os.environ.get("YOUTUBE_API_KEY")
            or _read_env_file(config.home() / "credentials.env").get("YOUTUBE_API_KEY"))


# ------------------------------------------------------------------ the API

class QuotaSpent(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status, reason, message):
        super().__init__(f"YouTube API {status} {reason}: {message}")
        self.status, self.reason = status, reason


def units_today(conn):
    row = conn.execute("SELECT COALESCE(SUM(units), 0) FROM costs WHERE provider = ? "
                       "AND substr(ts, 1, 10) = ?", (PROVIDER, db.now()[:10])).fetchone()
    return int(row[0])


class YouTube:
    """One place that talks to the API, counts units, and stops at the cap.
    `fetch` is swappable so tests never touch the network."""

    def __init__(self, conn, cfg, key=None, fetch=None):
        self.conn, self.cfg = conn, cfg
        self.key = key or api_key()
        self.fetch = fetch or self._http
        self.used = 0
        if not self.key and fetch is None:
            raise RuntimeError("no YOUTUBE_API_KEY. Make one (free): Google Cloud console -> "
                               "enable 'YouTube Data API v3' -> Credentials -> API key. Then: "
                               "python3 scripts/set-credential.py clip YOUTUBE_API_KEY")

    @staticmethod
    def _http(url):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                body = {}
            return body or {"error": {"code": e.code, "message": str(e)}}

    def get(self, endpoint, **params):
        cost = UNITS[endpoint]
        if units_today(self.conn) + cost > int(self.cfg["spot_daily_units"]):
            raise QuotaSpent(f"Spotter has used {units_today(self.conn)} of its "
                             f"{self.cfg['spot_daily_units']} YouTube units today")
        params["key"] = self.key
        body = self.fetch(API + endpoint + "?" + urllib.parse.urlencode(params))
        db.record_cost(self.conn, PROVIDER, "quota", 0.0, units=cost, note=endpoint)
        self.used += cost
        err = body.get("error")
        if err:
            reason = ((err.get("errors") or [{}])[0]).get("reason", "")
            raise ApiError(err.get("code"), reason, err.get("message", ""))
        return body


# ------------------------------------------------------------------ pure parts

def as_int(v):
    """The API sends counts as strings ("1234"), and omits a hidden count."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_time(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def views_per_hour(views, published, now):
    t = parse_time(published)
    if views is None or t is None:
        return None
    return round(views / max(1.0, (now - t).total_seconds() / 3600), 1)


def rank_creators(videos, channels, now, min_subs):
    """Charting videos + their channels' stats -> creators, hottest first.
    Heat is the summed views-per-hour of a channel's charting videos: a
    creator with two videos climbing fast outranks one old giant."""
    by = {}
    for v in videos:
        sn, st = v.get("snippet") or {}, v.get("statistics") or {}
        cid = sn.get("channelId")
        if not cid:
            continue
        vph = views_per_hour(as_int(st.get("viewCount")), sn.get("publishedAt"), now) or 0
        c = by.setdefault(cid, {"channel_id": cid, "title": sn.get("channelTitle") or cid,
                                "heat": 0.0, "trending_videos": 0})
        c["heat"] += vph
        c["trending_videos"] += 1
    out = []
    for cid, c in by.items():
        ch = channels.get(cid) or {}
        st, sn = ch.get("statistics") or {}, ch.get("snippet") or {}
        subs = None if st.get("hiddenSubscriberCount") else as_int(st.get("subscriberCount"))
        if subs is None or subs < min_subs:
            continue
        c.update(subscribers=subs, total_views=as_int(st.get("viewCount")),
                 handle=sn.get("customUrl"), heat=round(c["heat"], 1))
        out.append(c)
    out.sort(key=lambda c: c["heat"], reverse=True)
    return out


STOP = set("""a an the and or but so of to in on at for with from by is are was were be been
it its it's this that these those i you he she we they me him her us them my your his our
their what who how why when where not no yes just like really very too also than then there
here about into out up down over after before again all any some more most much many can
could would should will won't don't doesn't didn't isn't aren't wasn't i'm you're he's she's
we're they're i've you've do does did have has had get got go goes going gone one two new
shorts short clip clips video viral funny moment moments live stream podcast full episode
reacts reaction best ever official vs""".split())


def tokens(text, drop=()):
    return [t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if t not in STOP and t not in drop and len(t) > 1 and not t.startswith("#")]


def match(query, sents, drop=(), span=2):
    """Where in a transcript does a trending clip's title/description point?

    Each run of up to `span` sentences is scored by the rarity-weighted share
    of the query's words it contains (a word in every sentence, like the
    host's catchphrase, counts for little). Returns (score 0..1, i, j) for the
    best run, or (0, None, None)."""
    q = set(tokens(query, drop))
    if not q or not sents:
        return 0.0, None, None
    sets = [set(tokens(s["text"])) for s in sents]
    n = len(sets)
    idf = {t: math.log((n + 1) / (1 + sum(1 for s in sets if t in s))) + 1 for t in q}
    total = sum(idf.values())
    best = (0.0, None, None)
    for i in range(n):
        seen = set()
        for j in range(i, min(n, i + span)):
            seen |= sets[j] & q
            if len(seen) < 2:
                continue
            sc = sum(idf[t] for t in seen) / total
            if sc > best[0]:
                best = (round(sc, 3), i, j)
    return best


def widen(sents, i, j, min_len, target, max_len):
    """Grow a matched run of sentences into a clip: whole sentences only,
    alternating before and after so the moment sits in the middle, until it
    reaches the target length without passing the maximum."""
    dur = lambda a, b: sents[b]["end"] - sents[a]["start"]
    before = True
    while dur(i, j) < target:
        grew = False
        for side in ((0, 1) if before else (1, 0)):
            if side == 0 and i > 0 and dur(i - 1, j) <= max_len:
                i, grew = i - 1, True
                break
            if side == 1 and j + 1 < len(sents) and dur(i, j + 1) <= max_len:
                j, grew = j + 1, True
                break
        if not grew:
            break
        before = not before
    if dur(i, j) < min_len or dur(i, j) > max_len:
        return None
    return i, j


# ------------------------------------------------------------------ a run

def trending_videos(yt, cfg):
    out = []
    for cat in cfg["spot_categories"]:
        try:
            body = yt.get("videos", part="snippet,statistics", chart="mostPopular",
                          regionCode=cfg["spot_region"], videoCategoryId=cat, maxResults=50)
        except ApiError as e:
            if e.reason in ("quotaExceeded", "keyInvalid", "forbidden", "accessNotConfigured"):
                raise
            continue                      # a category with no chart in this region
        out += body.get("items") or []
    return out


def channel_stats(yt, ids):
    out, ids = {}, list(dict.fromkeys(ids))
    for k in range(0, len(ids), 50):
        body = yt.get("channels", part="snippet,statistics", id=",".join(ids[k:k + 50]))
        for ch in body.get("items") or []:
            out[ch["id"]] = ch
    return out


def permitted_channels(conn):
    return {r["channel_id"]: r for r in conn.execute(
        "SELECT * FROM sources WHERE active = 1 AND channel_id IS NOT NULL")}


def clips_about(yt, creator, cfg, now):
    since = (now - timedelta(hours=int(cfg["spot_lookback_hours"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = yt.get("search", part="snippet", type="video", videoDuration="short",
                  order="viewCount", publishedAfter=since, q=creator["title"],
                  regionCode=cfg["spot_region"], maxResults=25)
    ids = [it["id"]["videoId"] for it in body.get("items") or [] if (it.get("id") or {}).get("videoId")]
    if not ids:
        return []
    return (yt.get("videos", part="snippet,statistics", id=",".join(ids)).get("items") or [])


def sentences_for(paths, video_id, cfg):
    f = paths["media"] / str(video_id) / "transcript.json"
    if not f.exists():
        return None, None
    words = json.loads(f.read_text())["words"]
    return words, moments.sentences(words, cfg["sentence_pause_seconds"])


def match_in_footage(conn, paths, cfg, source, text, creator_title):
    """Best match for a trending clip across every transcript of this source."""
    drop = set(tokens(creator_title))
    best = None
    for v in conn.execute("SELECT id FROM videos WHERE source_id = ? AND stage IN ('found', 'done')",
                          (source["id"],)):
        words, sents = sentences_for(paths, v["id"], cfg)
        if not sents:
            continue
        sc, i, j = match(text, sents, drop)
        if i is not None and sc >= float(cfg["spot_min_match"]) and (not best or sc > best[0]):
            best = (sc, v["id"], i, j, sents)
    return best


def run(conn, paths, cfg, yt=None, now=None):
    now = now or datetime.now(timezone.utc)
    yt = yt or YouTube(conn, cfg)
    note = []
    videos = trending_videos(yt, cfg)
    chans = channel_stats(yt, [(v.get("snippet") or {}).get("channelId") for v in videos
                               if (v.get("snippet") or {}).get("channelId")])
    creators = rank_creators(videos, chans, now, int(cfg["spot_min_subscribers"]))
    stamp = db.now()
    with conn:
        for c in creators:
            conn.execute(
                "INSERT INTO creators (channel_id, title, handle, subscribers, total_views, heat, "
                "trending_videos, last_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, handle=excluded.handle, "
                "subscribers=excluded.subscribers, total_views=excluded.total_views, heat=excluded.heat, "
                "trending_videos=excluded.trending_videos, last_seen=excluded.last_seen, "
                "updated_at=excluded.updated_at",
                (c["channel_id"], c["title"], c.get("handle"), c["subscribers"], c.get("total_views"),
                 c["heat"], c["trending_videos"], stamp[:10], stamp))

    # Search the creators Clip can act on first: a trending moment in
    # footage Clip may cut is worth more than one it can only report.
    allowed = permitted_channels(conn)
    order = sorted(creators, key=lambda c: (c["channel_id"] not in allowed, -c["heat"]))
    n_clips = n_matched = 0
    for c in order[:int(cfg["spot_search_creators"])]:
        try:
            found = clips_about(yt, c, cfg, now)
        except QuotaSpent as e:
            note.append(str(e))
            break
        source = allowed.get(c["channel_id"])
        for v in found:
            sn, st = v.get("snippet") or {}, v.get("statistics") or {}
            views = as_int(st.get("viewCount"))
            row = {"yt_id": v["id"], "creator_id": c["channel_id"], "uploader": sn.get("channelTitle"),
                   "uploader_id": sn.get("channelId"), "title": sn.get("title") or "",
                   "description": (sn.get("description") or "")[:1000],
                   "published_at": sn.get("publishedAt"), "views": views,
                   "views_per_hour": views_per_hour(views, sn.get("publishedAt"), now)}
            m = None
            if source:
                m = match_in_footage(conn, paths, cfg, source, row["title"] + " " + row["description"],
                                     c["title"])
            with conn:
                conn.execute(
                    "INSERT INTO trending_clips (yt_id, creator_id, uploader, uploader_id, title, "
                    "description, published_at, views, views_per_hour, seen_at) "
                    "VALUES (:yt_id, :creator_id, :uploader, :uploader_id, :title, :description, "
                    ":published_at, :views, :views_per_hour, :seen) "
                    "ON CONFLICT(yt_id) DO UPDATE SET views=excluded.views, "
                    "views_per_hour=excluded.views_per_hour, seen_at=excluded.seen_at",
                    dict(row, seen=stamp))
                if m:
                    sc, vid, i, j, sents = m
                    conn.execute("UPDATE trending_clips SET match_video_id=?, match_start=?, match_end=?, "
                                 "match_score=?, match_text=? WHERE yt_id=?",
                                 (vid, sents[i]["start"], sents[j]["end"], sc,
                                  " ".join(s["text"] for s in sents[i:j + 1])[:500], row["yt_id"]))
            n_clips += 1
            n_matched += 1 if m else 0
    with conn:
        conn.execute("INSERT INTO spot_runs (ts, units, creators, clips, matched, note) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (stamp, yt.used, len(creators), n_clips, n_matched, "; ".join(note) or None))
    db.event(conn, "spot", f"{len(creators)} creators, {n_clips} trending clips, {n_matched} "
                           f"in permitted footage, {yt.used} units")
    return {"creators": len(creators), "clips": n_clips, "matched": n_matched, "units": yt.used,
            "note": note}


# ------------------------------------------------------------------ remake

def remake(conn, paths, cfg, trend_id):
    """Cut Clip's own version of a trending moment, from permitted footage.
    Refuses unless the moment was found in a source with permission - the
    trending clip itself is never used."""
    from clipper import pipeline
    t = conn.execute("SELECT * FROM trending_clips WHERE id = ?", (trend_id,)).fetchone()
    if not t:
        raise ValueError(f"no trending clip #{trend_id}")
    if t["remade_clip_id"]:
        raise ValueError(f"trending clip #{trend_id} was already remade as clip {t['remade_clip_id']}")
    if not t["match_video_id"]:
        raise ValueError(f"#{trend_id} was not found in any footage Clip has permission for. "
                         f"Ingest that creator's source video first (a campaign's files, or your own).")
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (t["match_video_id"],)).fetchone()
    src = conn.execute("SELECT * FROM sources WHERE id = ? AND active = 1", (video["source_id"],)).fetchone()
    if not src:
        raise ValueError("that footage's source is no longer active - permission withdrawn?")
    words, sents = sentences_for(paths, video["id"], cfg)
    i = next(k for k, s in enumerate(sents) if s["start"] >= t["match_start"] - 0.01)
    j = max(k for k, s in enumerate(sents) if s["end"] <= t["match_end"] + 0.01)
    span = widen(sents, i, j, cfg["min_clip_seconds"], cfg["target_clip_seconds"], cfg["max_clip_seconds"])
    if not span:
        raise ValueError("the matched moment cannot be made into a clip of the allowed length")
    i, j = span
    loud_file = paths["media"] / str(video["id"]) / "loudness.json"
    loud = json.loads(loud_file.read_text()) if loud_file.exists() else []
    c = score.score_windows(sents, [(i, j)], words, loud, cfg)[0]
    start, end = moments.cut_points(words, sents[i]["first"], sents[j]["last"])
    why = [f"same moment as a trending clip: \"{t['title'][:80]}\" "
           f"({t['views_per_hour'] or 0:,.0f} views/hour)"] + c["reasons"]
    with conn:
        cur = conn.execute(
            "INSERT INTO candidates (video_id, start, end, text, score, scorer, features, reasons, selected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (video["id"], start, end, c["text"], c["score"], "trend-match+" + score.SCORER,
             db.as_json(dict(c["features"], trend_match=t["match_score"])), db.as_json(why)))
        conn.execute("UPDATE videos SET stage = 'found' WHERE id = ?", (video["id"],))
    cand = cur.lastrowid
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video["id"],)).fetchone()
    if not pipeline.advance(conn, paths, cfg, video):
        v = conn.execute("SELECT error FROM videos WHERE id = ?", (video["id"],)).fetchone()
        raise RuntimeError(f"render failed: {v['error']}")
    clip = conn.execute("SELECT * FROM clips WHERE candidate_id = ?", (cand,)).fetchone()
    with conn:
        conn.execute("UPDATE trending_clips SET remade_clip_id = ? WHERE id = ?", (clip["id"], trend_id))
    return clip
