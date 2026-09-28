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
