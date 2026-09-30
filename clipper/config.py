"""Where Clipper keeps things, and every setting it runs on - in one place.

Defaults live here. Overrides live in <home>/config.json, which is untracked
and which the Deck will edit in Phase 3; an unknown key there is an error,
not a silent no-op, because a misspelt setting that changes nothing is the
forgotten-edit failure this repo keeps meeting.

Runtime data lives under CLIPPER_HOME (default clipper/var/), not under
agents/: the dispatcher, preflight and the Deck treat every folder there as an
agent, and a media folder would read as a broken one.
"""

import json
import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", PACKAGE.parent))

DEFAULTS = {
    # --- transcription ------------------------------------------------------
    # base.en, measured 2026-09-28 on one Xeon thread: 15x realtime, 540 MB
    # peak. small.en was 5x and 1,155 MB - too much beside the Deck and the
    # agents on a 2 GB droplet. Set "language" to null for non-English audio
    # and pick a multilingual model ("base", "small").
    "transcriber": "faster-whisper",
    "whisper_model": "base.en",
    "whisper_threads": 1,
    # Transcribed ten minutes at a time: the whole 81-minute Trae Young
    # episode at once peaked at 4.9 GB and was killed on the 2 GB droplet.
    "whisper_chunk_seconds": 600,
    "language": "en",

    # --- finding moments ----------------------------------------------------
    "min_clip_seconds": 20.0,
    "max_clip_seconds": 60.0,
    "target_clip_seconds": 35.0,
    "max_clips_per_video": 5,
    "min_score": 40.0,
    # A gap this long between two words ends a sentence even with no
    # punctuation - Whisper leaves plenty out.
    "sentence_pause_seconds": 0.8,

    # --- editing ------------------------------------------------------------
    # "center": crop the middle of the frame (one speaker, centred).
    # "blur":   whole frame on a blurred copy of itself (wide shots, two people).
    "crop_mode": "center",
    "caption_style": "bold-yellow",
    "caption_uppercase": True,
    "caption_max_words": 3,
    "caption_max_chars": 18,
    "x264_preset": "veryfast",
    "x264_crf": 21,
    "loudness_lufs": -14.0,          # what TikTok and Shorts normalise toward
    "fps": 30,
    # A source's watermark goes centred, its top edge this far down the
    # 1920-high frame: under the status bar and TikTok's Following/For You
    # tabs, well above the captions. Never a corner, never under the
    # captions - render.watermark_box refuses a spot that would be.
    "watermark_top": 300,
    # Shown at its own size; a file wider than this is scaled down evenly
    # (never stretched) so it stays clear of TikTok's buttons on the right.
    "watermark_max_width": 600,

    # --- rights -------------------------------------------------------------
    # Clipping campaigns pay for views, which is commercial use - so a
    # non-commercial Creative Commons licence is refused unless this is on.
    "allow_noncommercial": False,

    # --- Spotter, the research assistant ------------------------------------
    # YouTube category ids: 20 Gaming, 24 Entertainment, 23 Comedy,
    # 22 People & Blogs, 17 Sports. A category with no chart in the region is
    # skipped, not an error.
    "spot_region": "US",
    "spot_categories": ["20", "24", "23", "22", "17"],
    "spot_min_subscribers": 500000,  # "top influencers": smaller channels are not ranked
    "spot_search_creators": 8,       # 100 quota units each
    "spot_lookback_hours": 72,
    "spot_daily_units": 3000,        # of the free 10,000/day; the rest is left for uploads later
    "spot_min_match": 0.5,           # share of a trending clip's words found in the footage

    # --- Twitch: the chat watcher (twitch.py) --------------------------------
    # A clip is asked for when chat runs twitch_spike_ratio times faster than
    # usual AND at least twitch_spike_min_rate messages a second - believed
    # values, from three minutes of one busy chat (5-7 a second); tune them
    # on what the first streams produce.
    "twitch_spike_ratio": 3.0,
    "twitch_spike_min_rate": 2.0,
    "twitch_clip_seconds": 60,        # 5-60: Twitch keeps the last this-many of ~90 s
    # Asked this long after chat explodes, so the reaction is in the clip.
    "twitch_clip_delay_seconds": 5,
    "twitch_cooldown_seconds": 180,   # between two clips of one stream
    "twitch_max_clips_per_stream": 8,  # a handful a stream, never a flood
    "twitch_moments_hours": 48,       # past broadcasts' top clips from this far back

    # --- posting ------------------------------------------------------------
    "platforms": ["youtube", "tiktok"],
    # Asked for; YouTube itself keeps uploads private until the Google Cloud
    # project passes its API audit, and says so in the upload's reply.
    "youtube_privacy": "public",
    "youtube_category": "24",        # Entertainment
    "max_daily_uploads": 6,          # per platform
    "copy_model": "openai/gpt-5-mini",

    # --- money --------------------------------------------------------------
    # Phase 1 spends nothing: transcription is local. The ledger and the cap
    # exist now so the first paid provider cannot be added without them.
    "daily_budget_usd": 0.25,
}


def home():
    return Path(os.environ.get("CLIPPER_HOME", PACKAGE / "var"))


def load(path=None):
    """Defaults, overlaid with config.json. Raises on an unknown key."""
    cfg = dict(DEFAULTS)
    path = Path(path) if path else home() / "config.json"
    if path.exists():
        user = json.loads(path.read_text())
        unknown = sorted(set(user) - set(DEFAULTS))
        if unknown:
            raise ValueError(f"{path}: unknown setting(s) {', '.join(unknown)} - "
                             f"known: {', '.join(sorted(DEFAULTS))}")
        cfg.update(user)
    return cfg


def paths():
    h = home()
    return {
        "home": h,
        "db": h / "clipper.db",
        "media": h / "media",          # one folder per source video
        "clips": h / "clips",          # finished shorts, one folder per video
        "lock": h / "run.lock",
    }
