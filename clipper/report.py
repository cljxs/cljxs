"""What Clip and Spotter have to show, as one JSON document.

The Deck reads this (python3 -m clipper deck) rather than opening the
database itself, and the CLI's `trends` uses the same trend_state(): the
rule for "can Clip act on this trending moment?" exists once.
"""

import json

from clipper import db, postcopy, trends, youtube


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
        "spotter": {"last_run": dict(last) if last else None, "units_today": trends.units_today(conn),
                    "units_cap": cfg["spot_daily_units"], "has_key": bool(trends.api_key()),
                    "min_subscribers": cfg["spot_min_subscribers"],
                    "creators": creators, "trending": trending},
    }
