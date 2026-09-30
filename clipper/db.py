"""Clipper's memory: one SQLite file, schema versioned by PRAGMA user_version.

The chain the whole system learns from is kept here, one table per link:

    sources -> videos -> candidates (every window scored, chosen or not)
            -> clips -> [publications -> metrics -> weights: later phases]

Candidates that were NOT chosen are kept on purpose. Learning which features
predict a good clip needs the losers as well as the winners.

Each phase adds a migration to MIGRATIONS; nothing is ever edited in place,
so a droplet on any earlier version upgrades by running the ones it lacks.
"""

import json
import sqlite3
from datetime import datetime, timezone

MIGRATIONS = [
    # 1 - Phase 1: ingest, transcribe, find, render.
    """
    CREATE TABLE sources (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      url TEXT,
      rights TEXT NOT NULL,           -- see rights.py
      evidence TEXT NOT NULL,         -- why we may use it: a link, an email, a campaign
      attribution TEXT,               -- credit line the description must carry
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );
    CREATE TABLE videos (
      id INTEGER PRIMARY KEY,
      source_id INTEGER NOT NULL REFERENCES sources(id),
      title TEXT,
      origin TEXT NOT NULL,           -- the path or URL it came from
      sha256 TEXT NOT NULL UNIQUE,    -- the duplicate check: same bytes, same video
      media_path TEXT NOT NULL,
      duration REAL,
      stage TEXT NOT NULL DEFAULT 'ingested',   -- ingested, transcribed, found, done, failed
      error TEXT,
      attempts INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE candidates (
      id INTEGER PRIMARY KEY,
      video_id INTEGER NOT NULL REFERENCES videos(id),
      start REAL NOT NULL,
      end REAL NOT NULL,
      text TEXT NOT NULL,
      score REAL NOT NULL,
      scorer TEXT NOT NULL,           -- which scorer and version produced the score
      features TEXT NOT NULL,         -- JSON: every feature, for learning later
      reasons TEXT NOT NULL,          -- JSON: the plain-English why
      selected INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX ix_candidates_video ON candidates(video_id, score);
    CREATE TABLE clips (
      id INTEGER PRIMARY KEY,
      video_id INTEGER NOT NULL REFERENCES videos(id),
      candidate_id INTEGER NOT NULL REFERENCES candidates(id),
      rank INTEGER NOT NULL,
      path TEXT NOT NULL,
      start REAL NOT NULL,
      end REAL NOT NULL,
      score REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'rendered',  -- rendered, approved, rejected, published, failed
      meta TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE events (
      id INTEGER PRIMARY KEY,
      ts TEXT NOT NULL,
      video_id INTEGER,
      clip_id INTEGER,
      stage TEXT NOT NULL,
      level TEXT NOT NULL,            -- info, warn, error
      msg TEXT NOT NULL
    );
    CREATE TABLE costs (
      id INTEGER PRIMARY KEY,
      ts TEXT NOT NULL,
      provider TEXT NOT NULL,
      kind TEXT NOT NULL,             -- transcribe, llm, storage ...
      units REAL,
      usd REAL NOT NULL,
      video_id INTEGER,
      note TEXT
    );
    """,
    # 2 - Spotter, Clip's research assistant: who is trending, which of their
    # moments are being clipped right now, and whether one of those moments
    # is in footage Clip is allowed to cut.
    """
    ALTER TABLE sources ADD COLUMN channel_id TEXT;   -- the creator's YouTube channel, if any
    CREATE TABLE creators (
      channel_id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      handle TEXT,
      subscribers INTEGER,
      total_views INTEGER,
      heat REAL NOT NULL DEFAULT 0,     -- views per hour across their videos on the trending charts
      trending_videos INTEGER NOT NULL DEFAULT 0,
      last_seen TEXT NOT NULL,          -- last day they were on a trending chart
      updated_at TEXT NOT NULL
    );
    CREATE TABLE trending_clips (
      id INTEGER PRIMARY KEY,
      yt_id TEXT NOT NULL UNIQUE,
      creator_id TEXT,                  -- whose moment it is
      uploader TEXT,                    -- who posted this clip
      uploader_id TEXT,
      title TEXT NOT NULL,
      description TEXT,
      published_at TEXT,
      views INTEGER,
      views_per_hour REAL,
      seen_at TEXT NOT NULL,
      match_video_id INTEGER,           -- the same moment in footage Clip may use
      match_start REAL,
      match_end REAL,
      match_score REAL,
      match_text TEXT,
      remade_clip_id INTEGER
    );
    CREATE TABLE spot_runs (
      id INTEGER PRIMARY KEY,
      ts TEXT NOT NULL,
      units INTEGER NOT NULL,
      creators INTEGER NOT NULL,
      clips INTEGER NOT NULL,
      matched INTEGER NOT NULL,
      note TEXT
    );
    """,
    # 3 - approving and posting. The words that go out with a clip, the owner's
    # verdict on it (a label for learning, and faster than views), and one row
    # per clip per platform: UNIQUE is the duplicate-post guard.
    """
    ALTER TABLE sources ADD COLUMN post_tags TEXT;   -- tags a campaign requires, e.g. "#clipping #creator"
    ALTER TABLE sources ADD COLUMN credit TEXT;      -- a line every post must carry, e.g. "Clip from @creator"
    CREATE TABLE post_copy (
      clip_id INTEGER PRIMARY KEY REFERENCES clips(id),
      title TEXT NOT NULL,
      caption TEXT NOT NULL,
      hashtags TEXT NOT NULL,           -- JSON list, without '#'
      generator TEXT NOT NULL,          -- "llm:<model>", "template", or "owner" once edited
      cost_usd REAL NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE reviews (
      id INTEGER PRIMARY KEY,
      clip_id INTEGER NOT NULL REFERENCES clips(id),
      verdict TEXT NOT NULL,            -- approve, reject
      reason TEXT,
      ts TEXT NOT NULL
    );
    CREATE TABLE publications (
      id INTEGER PRIMARY KEY,
      clip_id INTEGER NOT NULL REFERENCES clips(id),
      platform TEXT NOT NULL,           -- youtube, tiktok
      status TEXT NOT NULL,             -- queued, posted, manual, needs_setup, failed
      remote_id TEXT,
      url TEXT,
      privacy TEXT,
      detail TEXT,                      -- the last thing that happened, in words
      attempts INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      posted_at TEXT,
      UNIQUE (clip_id, platform)
    );
    """,
    # 4 - a campaign's watermark: the file Clip overlays, unchanged, on every
    # clip from that source (Curious Mike's "YT: @mpj"). A path under
    # CLIPPER_HOME/watermarks - a copy, so a moved download cannot drop it.
    """
    ALTER TABLE sources ADD COLUMN watermark TEXT;
    """,
    # 5 - where a source's watermark sits. Per source because it depends on
    # the footage: Curious Mike's goes low-middle, on the guest's black shirt,
    # where white text reads; NULL means config's watermark_top.
    """
    ALTER TABLE sources ADD COLUMN watermark_top INTEGER;
    """,
    # 6 - the moments a campaign says to hunt for ("Knicks fans and the chants",
    # "Pat Beverley", ...): JSON [{"name": .., "terms": [..]}], in the
    # campaign's order of priority. See score.parse_hunt.
    """
    ALTER TABLE sources ADD COLUMN hunt TEXT;
    """,
    # 7 - how a source's names are spelled when Whisper mishears them ("tray"
    # for Trae, "nicks" for Knicks): transcribe.parse_spellings as JSON.
    """
    ALTER TABLE sources ADD COLUMN spellings TEXT;
    """,
    # 8 - a campaign's own clip list (campaign.py): the moments it names on a
    # published episode, with its hook and caption. A video knows which
    # episode it is; a candidate cut from the list knows which entry.
    """
    ALTER TABLE videos ADD COLUMN episode TEXT;        -- the YouTube id of the published episode
    ALTER TABLE candidates ADD COLUMN cut_id INTEGER;  -- cutlist.id when cut from a campaign's list
    CREATE TABLE cutlist (
      id INTEGER PRIMARY KEY,
      source_id INTEGER NOT NULL REFERENCES sources(id),
      episode TEXT NOT NULL,
      key TEXT NOT NULL,                -- the campaign's own id: "C45", "2"
      priority INTEGER NOT NULL,        -- the campaign's order: its "start here" rows first
      first_batch INTEGER NOT NULL DEFAULT 0,
      start REAL NOT NULL,
      end REAL NOT NULL,
      title TEXT, topic TEXT,
      hook TEXT,                        -- on screen, the whole clip
      caption TEXT,                     -- the post, verbatim
      direction TEXT,                   -- how the campaign says to cut it
      imported_at TEXT NOT NULL,
      UNIQUE (source_id, episode, key)
    );
    """,
    # 9 - Twitch (twitch.py) and the Dropbox pickup (dropbox.py). A Twitch
    # clip here is one the watcher made on your account; a moment is one
    # viewers clipped from a past broadcast, for you to clip yourself. A
    # pickup is a file seen in a source's Dropbox folder, and what came of it.
    """
    ALTER TABLE sources ADD COLUMN twitch TEXT;          -- the Twitch channel the watcher follows
    ALTER TABLE sources ADD COLUMN dropbox_folder TEXT;  -- a shared folder link the pickup empties
    CREATE TABLE twitch_clips (
      id TEXT PRIMARY KEY,              -- Twitch's clip id
      channel TEXT NOT NULL,
      edit_url TEXT,                    -- Twitch's page to trim, publish and download it (24 h)
      url TEXT,                         -- clips.twitch.tv/<id>, once Twitch confirms it exists
      status TEXT NOT NULL,             -- asked, made, failed
      chat_rate REAL,                   -- messages a second when it was made
      chat_usual REAL,                  -- and what was usual just before
      chat_words TEXT,                  -- JSON [[word, count], ...]: what chat was saying
      stream_started_at TEXT,
      created_at TEXT NOT NULL,
      detail TEXT
    );
    CREATE TABLE twitch_moments (
      id TEXT PRIMARY KEY,              -- the viewer's clip id
      channel TEXT NOT NULL,
      title TEXT, views INTEGER, creator TEXT,
      video_id TEXT, vod_offset INTEGER, duration REAL,
      created_at TEXT, fetched_at TEXT NOT NULL
    );
    CREATE TABLE pickups (
      file_id TEXT NOT NULL,            -- Dropbox's id for the file
      content_hash TEXT NOT NULL,       -- a re-uploaded file with new content is a new pickup
      path TEXT NOT NULL,
      size INTEGER,
      status TEXT NOT NULL,             -- ingested, refused, failed
      video_id INTEGER REFERENCES videos(id),
      detail TEXT,
      seen_at TEXT NOT NULL,
      PRIMARY KEY (file_id, content_hash)
    );
    """,
]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    migrate(conn)
    return conn


def migrate(conn):
    have = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, sql in enumerate(MIGRATIONS[have:], start=have + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {n}")
    return len(MIGRATIONS)


def event(conn, stage, msg, level="info", video_id=None, clip_id=None):
    with conn:
        conn.execute("INSERT INTO events (ts, video_id, clip_id, stage, level, msg) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (now(), video_id, clip_id, stage, level, msg))


def spent_today(conn):
    """US dollars recorded today (UTC day, the same day budget.py uses)."""
    row = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM costs WHERE substr(ts, 1, 10) = ?",
                       (now()[:10],)).fetchone()
    return float(row[0])


def record_cost(conn, provider, kind, usd, units=None, video_id=None, note=None):
    with conn:
        conn.execute("INSERT INTO costs (ts, provider, kind, units, usd, video_id, note) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (now(), provider, kind, units, usd, video_id, note))


class OverBudget(RuntimeError):
    pass


def spend_guard(conn, cfg, estimate_usd):
    """Call BEFORE any paid request. Refuses if it would cross today's cap -
    checking after the money is spent is not a limit, it is a report."""
    spent = spent_today(conn)
    cap = float(cfg["daily_budget_usd"])
    if spent + estimate_usd > cap:
        raise OverBudget(f"Clipper has spent ${spent:.4f} today; this call (~${estimate_usd:.4f}) "
                         f"would pass the ${cap:.2f} daily cap. It resets at midnight UTC.")


def as_json(value):
    return json.dumps(value, separators=(",", ":"))
