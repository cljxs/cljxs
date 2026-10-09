"""What Clip and Spotter have to show, as one JSON document.

The Deck reads this (python3 -m clipper deck) rather than opening the
database itself, and the CLI's `trends` uses the same trend_state(): the
rule for "can Clip act on this trending moment?" exists once.
"""

import json

from clipper import db, postcopy, trends, twitch, youtube


def trend_state(t, allowed):
    """(code, words) for a trending clip: what, if anything, Clip can do."""
    if t["remade_clip_id"]:
        return "remade", f"remade as clip {t['remade_clip_id']}"
    if t["match_video_id"]:
        return "matched", "same moment is in your permitted footage"
    if t["creator_id"] in allowed:
        return "not_in_footage", "creator is permitted, moment not in footage ingested yet"
    return "no_permission", "no permission for this creator yet"


def post_words(conn, clip):
    """What would go out with this clip, exactly as publish sends it."""
    row = conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (clip["id"],)).fetchone()
    if not row:
        return None
    src = conn.execute("SELECT s.* FROM sources s JOIN videos v ON v.source_id = s.id WHERE v.id = ?",
                       (clip["video_id"],)).fetchone()
    words = postcopy.compose(row, src)
    return {"title": row["title"], "caption": row["caption"], "hashtags": json.loads(row["hashtags"]),
            "generator": row["generator"], "youtube": words["youtube"], "tiktok": words["tiktok"]}


def twitch_state(conn, cfg):
    """The watcher's clips, the past moments to clip by hand, and the
    Dropbox folders picked up from - one reader for the Deck and the CLI."""
    c = youtube.creds()
    # The last three streams, each best first (twitch.best_of): the ranking
    # is the watcher's, read here rather than restated.
    clips, streams = [], []
    for st in conn.execute("SELECT channel, stream_started_at FROM twitch_clips GROUP BY channel, "
                           "stream_started_at ORDER BY MAX(created_at) DESC LIMIT 3").fetchall():
        rows = conn.execute("SELECT * FROM twitch_clips WHERE channel = ? AND stream_started_at IS ?",
                            (st["channel"], st["stream_started_at"])).fetchall()
        ranked = twitch.best_of(rows, cfg["twitch_keep_best"])
        streams.append({"channel": st["channel"], "started_at": st["stream_started_at"],
                        "asked": len(rows), "made": len(ranked),
                        "best": sum(1 for c in ranked if c["best"])})
        for r in ranked + [dict(r) for r in rows if r["status"] != "made"]:
            clips.append({k: r[k] for k in ("id", "channel", "edit_url", "url", "status", "chat_rate",
                                             "chat_usual", "created_at", "detail", "stream_started_at")}
                         | {"words": json.loads(r["chat_words"] or "[]"),
                            "best": bool(r.get("best")), "jump": r.get("jump"),
                            "vod_link": twitch.vod_link(r["vod_id"], r["vod_offset"]) if r["vod_id"] else None,
                            "vod_offset": r["vod_offset"]})
    moments = [dict(m, link=twitch.vod_link(m["video_id"], m["vod_offset"])) for m in conn.execute(
        "SELECT * FROM twitch_moments ORDER BY views DESC LIMIT 12")]
    pickups = [dict(r) for r in conn.execute("SELECT path, status, video_id, detail, seen_at FROM pickups "
                                              "ORDER BY seen_at DESC LIMIT 10")]
    return {
        "ready": bool(c.get("TWITCH_CLIENT_ID") and c.get("TWITCH_REFRESH_TOKEN")),
        "channels": [s["twitch"] for s in twitch.watched(conn)],
        "clips": clips, "streams": streams, "moments": moments,
        "dropbox_ready": bool(c.get("DROPBOX_APP_KEY") and c.get("DROPBOX_APP_SECRET")),
        "folders": [s["name"] for s in conn.execute(
            "SELECT name FROM sources WHERE active = 1 AND dropbox_folder IS NOT NULL ORDER BY id")],
        "pickups": pickups,
    }


def snapshot(conn, paths, cfg):
    allowed = trends.permitted_channels(conn)
    last = conn.execute("SELECT * FROM spot_runs ORDER BY id DESC LIMIT 1").fetchone()
    videos = [dict(v, clips=conn.execute("SELECT COUNT(*) FROM clips WHERE video_id = ?",
                                         (v["id"],)).fetchone()[0])
              for v in conn.execute("SELECT v.id, v.title, v.stage, v.error, v.duration, v.created_at, "
                                    "s.name AS source FROM videos v JOIN sources s ON s.id = v.source_id "
                                    "ORDER BY v.id DESC LIMIT 20")]
    clips = []
    for c in conn.execute("SELECT * FROM clips ORDER BY video_id DESC, rank LIMIT 24"):
        m = json.loads(c["meta"])
        clips.append({"id": c["id"], "video_id": c["video_id"], "rank": c["rank"],
                      "start": c["start"], "end": c["end"], "score": c["score"],
                      "status": c["status"], "reasons": m.get("reasons") or [],
                      "text": (m.get("text") or "")[:300], "source": m.get("source"),
                      "video_title": m.get("video_title"), "created_at": c["created_at"],
                      "post": post_words(conn, c),
                      "publications": [dict(p) for p in conn.execute(
                          "SELECT platform, status, url, privacy, detail, posted_at FROM publications "
                          "WHERE clip_id = ? ORDER BY platform DESC", (c["id"],))]})
    creators, trending = [], []
    if last:
        for c in conn.execute("SELECT * FROM creators WHERE last_seen = ? ORDER BY heat DESC LIMIT 15",
                              (last["ts"][:10],)):
            creators.append(dict(c, permitted=c["channel_id"] in allowed))
        for t in conn.execute("SELECT t.*, c.title AS creator FROM trending_clips t LEFT JOIN creators c "
                              "ON c.channel_id = t.creator_id WHERE t.seen_at >= ? "
                              "ORDER BY t.views_per_hour DESC LIMIT 25", (last["ts"],)):
            code, words = trend_state(t, allowed)
            trending.append({k: t[k] for k in ("id", "yt_id", "creator", "uploader", "title", "views",
                                                "views_per_hour", "published_at", "match_video_id",
                                                "match_start", "match_end", "match_score",
                                                "remade_clip_id")}
                            | {"state": code, "state_words": words})
    return {
        "videos": videos, "clips": clips,
        "sources": [{"name": s["name"], "rights": s["rights"], "channel_id": s["channel_id"],
                     "active": bool(s["active"])} for s in conn.execute("SELECT * FROM sources ORDER BY id")],
        "spend_today": db.spent_today(conn), "daily_budget": cfg["daily_budget_usd"],
        "platforms": cfg["platforms"],
        "youtube_ready": all(youtube.creds().get(k) for k in
                             ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")),
        "twitch": twitch_state(conn, cfg),
        "spotter": {"last_run": dict(last) if last else None, "units_today": trends.units_today(conn),
                    "units_cap": cfg["spot_daily_units"], "has_key": bool(trends.api_key()),
                    "min_subscribers": cfg["spot_min_subscribers"],
                    "creators": creators, "trending": trending},
    }
