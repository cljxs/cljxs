# Clip — system design

The agent is called **Clip** (the code lives in `clipper/`). Its research
assistant is **Spotter**. Both live in Clip's studio in the pixel village.

Clip's job, stated as if it were an employee:

> Find the best moments in my approved video sources, turn them into
> high-quality short videos, publish them, measure the results, and get
> better at choosing.

This document is the whole system. **Phase 1 is built and verified** (see
[README.md](README.md)); everything after it is the plan, in the order it
should be built.

Throughout, **verified** means run and read the output, and **believed** means
it should hold but was not tested here — the same words the rest of this repo
uses.

---

## 0. The decisions that shape everything

| Decision | Choice | Why |
|---|---|---|
| Where it runs | The existing $12/month droplet (1 vCPU, 2 GB) | Already paid for. Measured: local transcription and rendering fit (§6). |
| What "agent" means here | A stage with one job, its own state, and a clear input/output, driven by a state machine. A language model is called **only for judgement** (is this funny? is this title honest?) and always returns JSON that code checks. | This ecosystem's own lesson: a model asked to do arithmetic eventually claims it did, and cost scales with turns. Fury's whole cycle is Python and wakes no model. |
| Orchestration | SQLite state + systemd timers. No Redis, n8n, Docker or Celery. | One process at a time on one CPU. A queue server would use more of the machine than the work it queues. Same pattern as Ace, Belfort and the task dispatcher. |
| Transcription | faster-whisper `base.en`, local, int8 | Measured here: **15x realtime on one thread, 540 MB peak**, word-level timestamps. $0. |
| Judgement model | A cheap API model through the OpenRouter key you already have (default `openai/gpt-5-mini`), text only | About **1–2 cents per hour of source video** (§6). A useful local LLM does not fit in 2 GB of RAM beside everything else. |
| Dashboard | A Clipper page in the existing Command Deck | Already running, already behind your VPN, already has the patterns (approve/decline, video playback that works on the iPad). A second web app is a second thing to keep alive. |
| Getting video in | Files and https links from the rights holder. **Not** downloading from YouTube. | YouTube's Terms of Service forbid downloading except through a download link YouTube shows (§17). Your own uploads come out of YouTube Studio; clipping campaigns hand out source files. |
| Publishing | Assisted first (you post the file Clipper prepared), then the official YouTube and TikTok APIs once their audits pass | Until audited, both APIs force every upload to **private** (§13). The audits take weeks, so they start in Phase 3. |

---

## 1. System architecture

Nine roles. Each role is a Python module with one responsibility; the
pipeline moves a video (and then each clip) through them by changing a
`stage` column in SQLite. Every stage writes its result to disk or the
database before the stage is marked done, so any crash costs only the stage
in progress.

| Role | Does | Code or model | Phase |
|---|---|---|---|
| **Source Manager** | Keeps the list of permitted sources with their evidence; watches channels for new uploads (RSS, free); ingests files/links; refuses duplicates by content hash | Code | 1 (ingest), 3 (watching) |
| **Content Analyzer** | Audio extraction, transcription with word timestamps, loudness per second, sentence segmentation; later scene cuts and faces | Code + local Whisper | 1, 2 |
| **Clip Hunter** | Builds every whole-sentence window of the right length, scores them, picks the best non-overlapping ones | Code (Phase 1) → code pre-filter + model rubric (Phase 2) | 1, 2 |
| **Hook Generator** | Decides whether the opening is strong enough; proposes truthful alternatives (earlier setup, verbatim cold-open), titles, captions, hashtags | Model, checked by code | 2 |
| **Clip Editor** | Cut, 9:16 crop (centre, blur, face-tracked), silence trimming, burned-in captions, loudness normalisation, export | Code (ffmpeg) | 1, 2 |
| **Quality Control** | Technical checks (code) + understandability and honesty checks (model); revise or reject | Both | 2 |
| **Publisher** | Calendar, caps, scheduling, uploads, duplicate prevention | Code + platform APIs | 4 |
| **Analyst** | Pulls performance at fixed ages (1 h, 24 h, 72 h, 7 d) | Code + platform APIs | 5 |
| **Learner** | Fits new scoring weights from outcomes; keeps them only if they would have ranked recent clips better | Code | 5 |

Why this and not a free-roaming agent: an LLM "employee" that decides what
to do next costs tokens every turn and fails in ways that are hard to see
(this ecosystem has seen agents post progress cards and exit, loop 184 times,
or ask a question nobody could answer). A state machine with model calls at
four judgement points costs cents, fails loudly and resumes.

---

## 2. Diagram

```
                         YOU (iPad)
                            │  approve / reject / edit / settings
                            ▼
                ┌────────────────────────┐        reads         ┌──────────────┐
                │  Command Deck (Node)   │◄─────────────────────│  clipper.db  │
                │  Clipper page (Ph. 3)  │──actions table──────►│   (SQLite)   │
                └────────────────────────┘                      └──────▲───────┘
                                                                       │ every stage
  PERMITTED SOURCES                                                    │ reads/writes
  file on droplet ─┐                                                   │
  https link ──────┼─► SOURCE MANAGER ──► CONTENT ANALYZER ──► CLIP HUNTER
  channel RSS ─────┘   rights gate,        audio, Whisper,      windows, score,
  (discovery only)     hash dedupe         loudness, sentences  choose
                                                                  │
                        ┌─────────────────────────────────────────┘
                        ▼
                  HOOK GENERATOR ──► CLIP EDITOR ──► QUALITY CONTROL ──fail──► revise (≤2) / reject
                  (model, Ph. 2)     ffmpeg 9:16,     code + model              │
                                     captions         checks                    │ pass
                                                                                ▼
                                                       approval mode: wait for YOU
                                                       auto mode (Ph. 6): gates
                                                                                │
                                                                                ▼
                  LEARNER ◄── ANALYST ◄── platforms ◄──────────────────── PUBLISHER
                  weekly weights   1h/24h/72h/7d   YouTube Shorts,        calendar, caps,
                  (holdout gate)   snapshots       TikTok                 dedupe, schedule
                        │
                        └──► new scorer version used by CLIP HUNTER

  Cross-cutting: cost ledger + daily hard cap (db.spend_guard, and the $1/day
  OpenRouter key limit), events log, run lock, systemd timers.
```

---

## 3. Technology stack — chosen, and what it beat

| Need | Chosen | Considered | Why chosen |
|---|---|---|---|
| Language | Python 3 (stdlib first) | — | Same as every other script here; no build step. |
| Media | FFmpeg 6 (already on the droplet) | MoviePy, OpenCV writers | FFmpeg does cutting, cropping, blur, subtitles (libass), loudness in one pass; MoviePy re-encodes frame by frame in Python and is several times slower. |
| Transcription | faster-whisper (CTranslate2) | whisper.cpp, OpenAI Whisper, WhisperX, Groq API | Word timestamps built in, int8 on CPU, pip-installable. WhisperX adds better alignment but pulls PyTorch (~2 GB) — too big here. whisper.cpp is comparable in speed but needs a compile step. Groq is the paid fast path (§5). |
| Captions | ASS subtitles rendered by libass inside ffmpeg | drawtext filters, Pillow frame drawing | One text file gives outlines, colour changes mid-line, per-word highlighting and positioning. Verified rendering on real frames. |
| Database | SQLite (WAL) | PostgreSQL | One writer, one machine, <1 GB of rows for years. Postgres adds a server to run and back up. The schema is plain SQL, so moving later is a dump/load. |
| Queue | `stage` column + file lock | Redis/RQ, Celery, n8n | Work is sequential by necessity (one CPU); a lock is the whole queue. |
| Scheduling | systemd timers | cron, n8n | Already how every agent here runs; logs go to the journal; health-check.py already reads them. |
| Web UI | Existing Command Deck (Express) | FastAPI + React/Next.js | Reuse beats rebuild; Safari range-request video playback already solved there. |
| Face detection (Ph. 2) | OpenCV YuNet (`FaceDetectorYN`, ~230 KB ONNX) | MediaPipe, YOLO | Tiny, CPU-fast, in `opencv-python-headless`. |
| Scene cuts (Ph. 2) | ffmpeg `select='gt(scene,0.3)'` | PySceneDetect | No extra dependency; PySceneDetect is the upgrade if cuts are missed. |
| Judgement LLM | OpenRouter, model configurable | Local Llama/Qwen via Ollama | Local models that fit in ~1 GB free RAM (1–3B) are poor judges of humour and context and would take minutes per video on one CPU. |
| Embeddings (Ph. 5, optional) | none at first; `all-MiniLM-L6-v2` if needed | API embeddings | Duplicate detection starts with word-shingle overlap (exact, free); embeddings only if near-duplicates slip through. |

---

## 4. AI models

| Task | Default | Measured / cited | Swap to |
|---|---|---|---|
| Speech → words | faster-whisper `base.en` int8, 1 thread | **Verified here**: 303 s of speech in 20.2 s (15x realtime), 540 MB peak, 547 words with timestamps. `small.en`: 5x realtime, 1,155 MB. | `small.en` on a 4 GB droplet (more accurate); Groq `whisper-large-v3-turbo` API at $0.04/hour of audio; multilingual `base`/`small` with `language: null`. |
| Clip judgement, hooks, titles, text QC | `openai/gpt-5-mini` via OpenRouter (the GM already uses it) | believed ≈ $0.25 / M input, $2 / M output tokens — check the model page before relying on it | Any OpenRouter model (a config value); Gemini Flash-Lite ($0.10/$0.40 per M) — note 2.5 Flash-Lite retires 16 Oct 2026, so never hard-code a model name; Groq/Gemini free tiers for experiments. |
| Visual QC (Ph. 2, optional) | 3 still frames per clip to a cheap vision model | ≈ $0.001 per clip (believed) | Skip entirely: face detection covers framing; vision only catches "something odd on screen". |
| Faces | YuNet | CPU real-time at small input sizes (cited) | MediaPipe. |

---

## 5. Free vs paid

| Service | Cost | Needed because | Free alternative | Local replacement? |
|---|---|---|---|---|
| Droplet | $12/mo (already paid) | Somewhere to run | — | It *is* the local machine |
| OpenRouter LLM | ~$0.01–0.02 per source-hour (§6) | Judging humour, context, honesty needs a capable model | Groq / Gemini free tiers (rate-limited; Gemini's free tier may use your data for training) | Not usefully on 2 GB RAM |
| Groq Whisper (optional) | $0.04 per audio hour (cited) | Only if the droplet can't keep up | Local faster-whisper (default) | Yes — the default |
| YouTube Data + Analytics APIs | Free (quota) | Upload, stats | Manual upload | — |
| TikTok for Developers APIs | Free (needs app review + audit) | Upload, stats | Manual upload | — |
| Extra storage | $0.10/GB/mo DO volume if ever needed | Keeping sources long-term | Delete sources after clips are approved (default plan) | — |

Nothing in Phase 1 is paid. The first paid call arrives in Phase 2, and the
cost ledger and `spend_guard` (a call is refused *before* it is made if it
would cross the daily cap) already exist and are tested.

---

## 6. Monthly operating cost

Measured inputs: transcription 15x realtime on one thread (verified); render
≈ 45 s of CPU per 37 s clip on one core, blur mode, `veryfast` (verified).
The droplet's shared vCPU may be slower than the test machine — **believed**
range below allows 2x.

| Usage | Source hours / month | Clips / day | CPU time / month | Extra spend / month | Notes |
|---|---|---|---|---|---|
| Starter | 20 | ~5 | ~5 h | **$0.20–0.50** (LLM) | Runs overnight on the current droplet. |
| Growth | 100 | ~15 | ~25 h (≈50 min/day) | **$1–3** | Still the current droplet, `Nice=19`. |
| Scale | 500 | ~50 | ~125 h (≈4 h/day) | **$20–35** | Upgrade to 2 vCPU / 4 GB (+$12/mo, believed) **or** move transcription to Groq (~$20/mo). LLM ~$5–10. |

Per-clip cost at Growth: about **1–3 cents per published clip**, almost all
of it LLM. Storage stays flat because sources are deleted once their clips
are decided (a 1-hour 720p source is ~1–1.5 GB; a finished clip ~10–20 MB).

**Hard limits** (two layers): Clipper's own `daily_budget_usd` (default
$0.25), checked before every paid call; and the existing $1/day limit on the
OpenRouter key, which the provider enforces even if Clipper had a bug.

---

## 7. Database

SQLite at `clipper/var/clipper.db`, versioned with `PRAGMA user_version`;
each phase appends a migration and never edits an old one.

**Now (migration 1, built):**

```
sources     id, name, url, rights, evidence, attribution, active
videos      id, source_id, title, origin, sha256 UNIQUE, media_path, duration,
            stage, error, attempts
candidates  id, video_id, start, end, text, score, scorer, features(JSON),
            reasons(JSON), selected          ← every window, chosen or not
clips       id, video_id, candidate_id, rank, path, start, end, score,
            status, meta(JSON)
events      id, ts, video_id, clip_id, stage, level, msg
costs       id, ts, provider, kind, units, usd, video_id, note
```

**Later:**

```
Ph. 2  clip_versions   clip_id, version, start, end, crop, hook, title_options, qc(JSON)
Ph. 3  actions         id, clip_id, kind(approve|reject|edit|regenerate), args, by, ts, done
       reviews         clip_id, verdict, reason, ts        ← labels, faster than views
Ph. 4  publications    id, clip_id, platform, remote_id, status, scheduled_for,
                       published_at, title, description, UNIQUE(clip_id, platform)
Ph. 5  metrics         publication_id, age_hours, views, engaged_views, avg_view_pct,
                       avg_view_sec, likes, comments, shares, saves, subs_gained, ts
       weights         version, weights(JSON), fitted_on, holdout_spearman, active
```

The learning chain is a join across these tables:
`sources → videos → candidates → clips → publications → metrics → weights`.

---

## 8. Agent responsibilities (inputs → outputs)

**Source Manager** — in: `source add` (name, rights, evidence, attribution);
files/links; channel IDs to watch. Out: `sources`, `videos` rows; media in
`var/media/<id>/`. Refuses: no evidence, unknown rights, non-commercial
licence (unless allowed), same bytes twice. Phase 3 adds a watcher that reads
each channel's free RSS feed (`youtube.com/feeds/videos.xml?channel_id=…`, no
API quota) and lists new uploads on the Deck; how the file is obtained
depends on the rights path (§17).

**Content Analyzer** — in: source video. Out: `audio.wav` (16 kHz mono),
`transcript.json` (words, times, confidence), `loudness.json` (per second).
Phase 2 adds scene cuts and face tracks.

**Clip Hunter** — in: words, loudness. Out: `candidates` (all), `selected`
flags. Rule enforced by construction: a clip is a run of whole sentences, so
it cannot start or end mid-sentence.

**Hook Generator** (Ph. 2) — in: chosen clip + 3 sentences either side. Out:
opening decision (keep / start earlier for setup / start later / verbatim
cold-open), 3–5 titles, captions, 3–5 hashtags, with the pick and why. Code
enforces: a cold-open line must be a verbatim quote from inside the same
clip; titles must not introduce names, numbers or claims absent from the
transcript; platform length limits.

**Clip Editor** — in: clip plan. Out: `NN.mp4` 1080x1920 H.264/AAC,
captions burned in, `NN.json` metadata, `NN.ass` captions.

**Quality Control** (Ph. 2) — in: rendered clip + plan. Out: pass / revise
(with the fix) / reject (with the reason), recorded per version.

**Publisher** (Ph. 4), **Analyst** (Ph. 5), **Learner** (Ph. 5) — §13–15.

**Spotter, the research assistant** (built 2026-09-28) — answers three
questions from YouTube's official Data API with a free key:

1. *Who is hot?* The most-popular charts per category (Gaming, Entertainment,
   Comedy, People & Blogs, Sports), collapsed to channels. Only channels over
   `spot_min_subscribers` (default 500,000) count, because the brief is the
   top influencers. They are ranked by **heat**: the summed views per hour of
   their charting videos.
2. *Which of their moments are being clipped?* **Only for creators Clip
   has permission for** (a source linked to their channel), charting or not:
   the most-viewed Shorts mentioning them in the last 72 hours, with views
   per hour. Other trending creators are listed as leads and are never
   searched, because a moment Clip may not cut isn't worth 100 quota units.
3. *Is that moment in footage Clip may use?* The trending clip's title and
   description are matched against the transcripts of that creator's
   ingested videos. The match weights rare words, and ignores the creator's
   own name, since that appears in every clip about them.

A match can be **remade**: Clip cuts its own version of the same moment
**from the permitted source**, with its own crop, captions and score, and
records which trending clip inspired it. That record becomes a feature for
learning later. Spotter never downloads, reposts or reuses the trending
clip itself: that clip belongs to whoever made it, and reposting it is what
platforms remove accounts for.

Creators with no permitted source are still shown, as leads to get
permission for, which usually means joining their clipping campaign. TikTok
has no trending API open to ordinary developers (its Research API is for
academic researchers), so research is YouTube-only. Quota: charts and
channel lookups cost 1 unit, a search 100. Every call is recorded in the
cost ledger, and a run stops before `spot_daily_units` (default 3,000 of
the free 10,000/day).

Tables (migration 2): `creators`, `trending_clips` (with the match and
`remade_clip_id`), `spot_runs`, and `sources.channel_id`, which links a
permitted source to the creator's channel.

---

## 9. Workflow and orchestration

Video stages (built): `ingested → transcribed → found → done`, or `failed`
after 3 attempts at the same stage. `retry` puts a failed video back at the
stage that failed; everything before it is reused from disk.

Clip stages (Ph. 2–4): `rendered → qc_passed → awaiting_approval → approved →
scheduled → published`, or `rejected` / `failed`.

Timers (Ph. 3 onward; all `Nice=19`, `IOSchedulingClass=idle`, one run at a
time via `run.lock`):

| Timer | When | Does |
|---|---|---|
| `clipper-watch` | hourly | RSS check for monitored channels |
| `clipper-run` | every 2 h, 23:00–07:00 Central | pipeline for anything pending |
| `clipper-publish` | every 15 min | posts whatever is due |
| `clipper-stats` | every 6 h | metrics snapshots at 1 h/24 h/72 h/7 d |
| `clipper-learn` | weekly | fit, evaluate, maybe activate new weights |

Livestreams are processed from their VOD once the stream ends. Clipping in
real time would need the CPU for hours during the stream.

---

## 10. Clip scoring methodology

**Phase 1 (built): `heuristic-v1`.** Eight features, each 0–1, stored on
every candidate:

| Feature | Weight | Measured as |
|---|---|---|
| hook | 0.22 | first sentence is a question, contains a hook phrase, is short, has a number |
| standalone | 0.18 | 0.25 if it opens on "and / so / he / that…" (needs earlier context), else 1 |
| ending | 0.14 | ends on . ? ! and is followed by a pause ≥ 0.6 s |
| energy | 0.14 | 90th-percentile loudness vs this video's median; +10 dB = 1.0 |
| emotion | 0.10 | exclamations and strong words per 100 words |
| info | 0.08 | numbers, reasons, steps per 100 words |
| pace | 0.08 | 2.2–3.8 words/second = 1 |
| length | 0.06 | closeness to the target length (35 s) |

`score = 100 × Σ(weight × feature) / Σ weight`. Honest limit: it cannot tell
a funny story from a dull one. On the test reading it produced 54–62/100
clips that were clean cuts — correct boundaries, not necessarily viral.

**Phase 2: two stages.**

1. *Pre-filter (code):* heuristic score, keep the top ~25 non-overlapping
   windows per source hour. This is what keeps model cost at cents.
2. *Rubric (model):* the model gets each window **with a sentence of context
   either side** and returns JSON, 0–10 per dimension, plus a one-line
   reason and a flag if the meaning depends on missing context:
   `hook, humor, surprise, emotion, story, information, controversy_truthful,
   standalone, ending, shareability, audience_fit`.
   Code validates the JSON, rejects out-of-range values, and retries once.

`final = Σ w_f × feature_f` over heuristic features **and** rubric
dimensions. Weights start as a hand-set prior; Phase 5 learns them.
**Predicted potential** is shown as High / Medium / Low by where the score
falls among this source's last 100 candidates (top 10% / next 30% / rest),
not as an invented view count.

Controversy is scored only when truthful: the model is asked whether the
clip *misrepresents* the speaker once cut; if yes, the clip is excluded,
not down-weighted.

---

## 11. Video-processing pipeline

Built (Phase 1), per clip, one ffmpeg call:

1. Fast seek to the start (`-ss` before `-i`), duration `-t`.
2. Crop: **centre** (largest 9:16 box, computed from the probed size) or
   **blur** (whole frame over a quarter-resolution blurred copy of itself —
   one sixteenth of the pixels for the same look).
3. Scale to 1080x1920 (lanczos), 30 fps.
4. Burn in ASS captions.
5. Loudness: `loudnorm` single pass to −14 LUFS. Verified output measured
   −15.5 LUFS, close enough for platforms that normalise anyway; Phase 2
   switches to two-pass for ±0.5 LU.
6. x264 `veryfast` CRF 21, yuv420p, AAC 128 kbps 48 kHz, `+faststart`.

Cut points: 0.15 s before the first word, 0.35 s after the last, but never
into the next word.

Phase 2 additions:

* **Speaker tracking:** sample 2 frames/s, YuNet faces, scene cuts from
  ffmpeg. Per scene: one static crop centred on the dominant face (largest,
  or the one nearest the previous crop). Static-per-scene rather than
  panning, because slow drift reads as amateur and a jitter bug is worse
  than no tracking. Two faces too far apart for one 9:16 box → blur mode or
  stacked split-screen.
* **Silence trimming:** gaps > 0.5 s between words become cuts. The caption
  timings are remapped through the same keep-list (a pure function, tested).
  On for talk, off for reaction/visual content (config).
* **Pacing:** optional 1.05–1.1x speed-up for slow speakers (pace feature < 0.5).
* **Visual emphasis:** a 1.08x punch-in on the highlighted peak sentence;
  on-screen hook text for the first 2–3 s (from the Hook Generator,
  QC-checked).

---

## 12. Captioning system

Built: word timings come straight from Whisper. Words are grouped into
chunks of up to 3 words / 18 characters, breaking after sentence ends and at
pauses ≥ 0.5 s, so a caption never runs one sentence into the next. Each
word gets its own event in which it is highlighted (and enlarged 8%). The
chunk stays up 0.25 s after its last word but never overlaps the next.
Position: bottom margin 560 px of 1920, above the part of the screen that
TikTok and Shorts cover with the username and buttons. Styles:
`bold-yellow`, `clean-white`, `boxed`. Uppercase optional. Verified visually
on rendered frames.

Phase 2: the model marks 1–2 emphasis words per sentence (drawn in a second
colour); QC flags any caption word with Whisper confidence < 0.5 for review;
a word list fixes recurring mis-hearings per source (names, jargon).

---

## 13. Publishing architecture

Facts that decide the design (checked 2026-09-28):

* **YouTube Data API.** Uploads from an unverified API project created after
  28 Jul 2020 are **restricted to private** until the project passes an API
  compliance audit (Google's revision history). Upload quota cost fell from
  ~1,600 to ~100 units on 4 Dec 2025 (Google); third-party write-ups say
  uploads moved to their own daily bucket in June 2026 (believed). Shorts:
  vertical and up to 3 minutes. Scheduling is supported (`status.publishAt`).
* **TikTok Content Posting API.** Direct Post needs the `video.publish` scope
  and a separate audit. Until audited, posts are **SELF_ONLY** (only you can
  see them), and at most 5 users can post in 24 h. Direct Post has a
  per-creator cap of ~15 posts/day across all apps. The alternative
  `video.upload` scope sends the video to your TikTok **inbox as a draft**,
  and you finish posting in the app — a natural fit for approval mode. No
  scheduling in the API.

So publishing arrives in three steps:

1. **Assisted (day one of Phase 4):** the Deck shows the approved clip with a
   Download button and copy buttons for the title, description and
   hashtags. You post from the phone. This works immediately and costs
   nothing.
2. **Drafts via API:** TikTok inbox upload; YouTube uploads (private until
   the audit, then public/scheduled).
3. **Direct, scheduled:** after both audits. The calendar, caps and
   duplicate checks are Clipper's own, identical in all three steps.

Scheduling: posting windows per platform (config), filled from the approved
queue best-first, respecting `max_daily_uploads` and platform caps. Duplicate
prevention: `UNIQUE(clip_id, platform)`; before scheduling, reject any clip
overlapping a published clip from the same video in time, or sharing > 60% of
word 5-grams with one. Descriptions always carry the source's attribution
line.

---

## 14. Analytics architecture

| Platform | Source | Metrics available |
|---|---|---|
| YouTube | Analytics API `reports.query`, `dimensions=video` | views, engagedViews, averageViewDuration, averageViewPercentage, likes, comments, shares, subscribersGained |
| YouTube | Data API `videos.list` (1 unit) | near-real-time view/like/comment counts |
| TikTok | Display API `video.list` / `video.query` | view_count, like_count, comment_count, share_count (and collect_count for some apps). **No watch time or completion rate** through this API; those are in TikTok Studio (manual), or the Business API for business accounts (believed). |

Note that since 2025 a Shorts "view" counts every start or replay with no
minimum. `engagedViews` is the old, stricter count and is the better signal.

Snapshots are taken at fixed ages (1 h, 24 h, 72 h, 7 d), so clips are compared at
the same age. The target metric is **lift**: `log(views at 72 h) −
log(median views at 72 h of this account's last 30 posts)`. It measures the
clip rather than the account's growth. Retention (`averageViewPercentage`)
is the second target where available.

---

## 15. Feedback / learning system

Two kinds of label, both kept:

* **Your reviews** (Ph. 3): approve / reject + reason. Plentiful and fast. The
  model learns to predict what you would approve.
* **Outcomes** (Ph. 5): lift at 72 h per platform. Slow and noisy, but the
  real goal.

Weekly job:

1. Build rows: features (heuristic + rubric + categorical: hook type, topic,
   source, duration bucket, caption style, crop mode, posting hour,
   platform) → lift.
2. Wait for enough data: nothing is fitted before **30 published clips per
   platform**.
3. Fit a ridge regression of lift on features, **shrunk toward the current
   weights** (it learns corrections, not a new model from scratch), with each
   weight's change capped at 25% per week.
4. **Holdout gate:** score the most recent 20% of clips with old and new
   weights. Activate the new version only if its Spearman rank correlation
   with actual lift is higher. Otherwise keep the old one and record why.
5. Every candidate stores the scorer version that scored it, so you can
   always ask "which version picked the winners?"

Categorical choices with few options (caption style, crop mode, posting
hour, hook type) use shrunk averages instead:
`(n × mean_lift) / (n + 10)`, so one lucky post cannot move a setting.
**Exploration:** 1 in 5 daily slots goes to the best clip from an
under-tested category, so the system does not lock into its first guess.
Nothing is hard-coded as "questions work". If they do, the data will show
it.

---

## 16. Security

* Credentials in `clipper/var/credentials.env`, mode 600, already covered by
  the repo's `**/credentials.env` ignore rule and `check-secrets.py`. Never
  printed; diagnose with `scripts/check-credentials.py` patterns.
* OAuth with least scopes: YouTube `youtube.upload`, `youtube.readonly`,
  `yt-analytics.readonly`; TikTok `user.info.basic`, `video.upload` (later
  `video.publish`), `video.list`. Refresh tokens rotate automatically;
  expiry shows on the Deck, not as a silent failure.
* Media from outside is untrusted input to ffmpeg: https-only downloads, a
  free-disk check before copying, argument lists (never shell strings), and
  ffmpeg kept updated with `apt`.
* Deck actions stay behind the VPN, like the rest of the Deck. The Deck
  writes *requests* to an `actions` table; only Clipper executes them.

---

## 17. Content rights and platform compliance

* **The rights gate (built).** Every source records one of `own`,
  `permission`, `cc-by`, `cc-by-sa`, `cc-by-nc`, `public-domain` **and written
  evidence**. Non-commercial licences are refused by default, because clips
  posted for views or campaign pay are commercial. CC-BY sources must carry
  an attribution line, and it goes into every description.
* **YouTube downloading.** YouTube's Terms of Service prohibit downloading
  except where YouTube shows a download option, and that applies even to
  Creative Commons videos. Clipper therefore does not include a YouTube
  downloader. The permitted routes:
  * your own uploads, via YouTube Studio's download;
  * clipping campaigns, which usually provide source files or links;
  * files a creator sends you.
  yt-dlp is legal software, but using it on YouTube breaks YouTube's terms.
  Whether to accept that risk for a creator who has personally told you to
  is your decision, and it would be a separate, clearly-labelled adapter.
* **Clipping campaigns** (e.g. Whop Content Rewards, where creators pay
  roughly $0.20–$6 per 1,000 views, cited) are the clean version of the
  business in your example: the creator grants permission and sets rules.
  Store the campaign page as the evidence and its rules in the source notes.
  QC checks the rules that can be checked by code, such as required tags.
* **No misleading edits.** A clip is always whole sentences (built). A
  cold-open must be verbatim from the same clip. Titles may not add names,
  numbers or claims that aren't in the transcript. QC asks "does cutting
  here change what the speaker meant?", and a yes excludes the clip.
* **Monetisation reality.** YouTube's reused-content policy makes unedited
  clips of other creators ineligible for Shorts revenue unless "clearly
  transformed". Captions, framing and hooks help, but campaign pay is the
  more reliable income for this kind of channel.
* **Takedowns.** Every clip keeps its source, evidence and timestamps. If a
  rights holder objects, the source is deactivated and the Publisher deletes
  its posts.

---

## 18. Failure handling and recovery

| Failure | Handling (built ✓ / planned) |
|---|---|
| Crash / reboot mid-run | ✓ stages resume from their saved files; the transcript is never redone |
| A stage keeps failing | ✓ 3 attempts, then `failed` with the error shown; `retry` resumes at that stage |
| Two runs at once | ✓ `run.lock`; the second exits "already running" |
| Disk full | ✓ ingest refuses without 2x the file size free; planned: source retention and a Deck warning below 5 GB |
| Same video twice | ✓ SHA-256 of the bytes |
| Paid call over budget | ✓ refused before the call (`spend_guard`), plus the OpenRouter key's own daily cap |
| Model returns bad JSON | planned: validate, retry once, then fall back to the heuristic score for that window |
| Platform API error / quota | planned: exponential backoff; quota errors park the post until the next day; token expiry is shown on the Deck |
| Out of memory | base.en peaks at 540 MB; `whisper_threads=1`; systemd `MemoryMax` on the unit |

---

## 19. Monitoring and logging

* `events` table: every stage writes what it did, and errors include a short
  traceback.
* `python3 -m clipper status`: videos, stages, errors, today's spend.
* The Deck's Clipper page (Ph. 3): processing queue, clips found / waiting /
  published / failed, top performers, cost today / this month, per clip and
  per published clip, and "needs attention".
* Integration with what exists: `preflight.py` gets a Clipper check (venv,
  ffmpeg, model cached, disk, DB migrates). `health-check.py` gets "did the
  timers run". Fury's collector gets Clipper facts, so the GM can propose
  improvements from them.

---

## 20. Folder structure

```
clipper/
  DESIGN.md  README.md  requirements.txt
  __main__.py cli.py config.py db.py rights.py        ✓ Phase 1
  ingest.py media.py transcribe.py moments.py
  score.py captions.py render.py pipeline.py           ✓ Phase 1
  llm.py hooks.py qc.py track.py trim.py               Phase 2
  watch.py                                             Phase 3
  publish/ youtube.py tiktok.py calendar.py            Phase 4
  analytics.py learn.py                                Phase 5
  var/            (untracked) clipper.db, config.json, credentials.env,
                  media/<video>/, clips/<video>/, run.lock
  .venv/          (untracked) faster-whisper
deploy/clipper-*.service, clipper-*.timer              Phase 3
mission-control-api/clipper.js + a Clipper house       Phase 3
tests/test_clipper.py, tests/fixtures/clipper-*        ✓ Phase 1
```

Runtime data lives in `clipper/var/`, not `agents/clipper/`. The dispatcher,
preflight and the Deck treat every folder under `agents/` as an agent, and
a media folder there would show up as a broken one.

---

## 21. Configuration and environment

Environment variables: `CLIPPER_HOME` (default `clipper/var`),
`ECOSYSTEM_ROOT`. Credentials go in `credentials.env` (Ph. 2+):
`OPENROUTER_API_KEY` (existing), optional `GROQ_API_KEY`, and
`YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN` and
`TIKTOK_CLIENT_KEY/SECRET/ACCESS_TOKEN/REFRESH_TOKEN` (Ph. 4).

Settings go in `clipper/var/config.json`. Only the keys you change are
needed, and an unknown key is an error rather than a silent no-op. Phase 1
keys: `transcriber, whisper_model, whisper_threads, language,
min/max/target_clip_seconds, max_clips_per_video, min_score,
sentence_pause_seconds, crop_mode, caption_style, caption_uppercase,
caption_max_words, caption_max_chars, x264_preset, x264_crf, loudness_lufs,
fps, allow_noncommercial, daily_budget_usd`. Later phases add: `mode`
(approval|auto), `platforms`, `max_daily_uploads`, `posting_windows`,
`llm_model`, `trim_silence`, `editing_style`.

---

## 22. API requirements

| API | What you do | Cost | Wait |
|---|---|---|---|
| OpenRouter | nothing — the existing key | per use, capped $1/day | — |
| YouTube Data API v3 + Analytics API | Google Cloud project → enable both → OAuth consent screen → OAuth client (desktop) → one-time sign-in to get a refresh token → **apply for the API compliance audit** to lift private-only | free | audit: weeks (believed) |
| YouTube RSS | none | free | — |
| TikTok for Developers | developer account → app with Login Kit + Content Posting API + Display API → privacy policy and terms URLs → app review (demo video) → **Direct Post audit** | free | review and audit: weeks (believed) |
| Groq (optional) | account + key | $0.04/audio hour | — |

---

## 23. Implementation roadmap

Each phase ends with a **done when** that can be checked, not a feeling.

### Phase 1 — permitted video in, captioned shorts out ✓ built

Built: rights gate; ingest from file or https link with hash dedupe;
16 kHz audio extraction; local transcription with word timestamps (cached);
loudness per second; whole-sentence windows; `heuristic-v1` scoring with
stored features and plain-English reasons; non-overlapping selection;
ffmpeg render (centre or blur crop, 1080x1920, captions with word
highlighting, loudness normalised); per-clip JSON metadata;
`clips/<video>/README.md` as the review sheet; resumable stages; retries;
run lock; cost ledger with a pre-spend cap; 26 tests, each rule
mutation-checked.

**Done when** (verified here on a public-domain LibriVox reading): a
5-minute video became 5 clips in 88 s. Each clip is 1080x1920 30 fps
H.264/AAC 48 kHz, captions render with the spoken word highlighted, and the
same file is refused a second time.

**Your part:** install on the droplet and run one video you have rights to
(README).

### Phase 2 — smarter choosing and editing

Build `llm.py` (OpenRouter client through `spend_guard`, JSON validation,
cost recorded per call), the two-stage scorer, `hooks.py`, `qc.py` (code
checks: duration, resolution, audio, loudness, black and frozen frames,
caption confidence, duplicates; model checks: standalone, honest title,
context), a revision loop (at most 2), `track.py` (YuNet faces, per-scene
crop), `trim.py` (silence removal with caption remapping), and two-pass
loudness.

**Done when:** on 3 real videos from your sources, you mark at least 3 of
each video's top 5 as "would post" (recorded in `reviews`). QC rejects each
of 6 deliberately broken clips: a mid-sentence start, a false title, a
black segment, a cropped-off face, silence and a duplicate. Model spend is
under $0.03 per source hour, read from the `costs` table.

### Phase 3 — dashboard and approval

*Started early (2026-09-28):* Clip's studio in the village shows the clips
(playable on the iPad), Spotter's research with **Research now** and
**Remake from my footage** buttons, and the queue. It is served by
`mission-control-api/clip.js`, which only runs `python3 -m clipper deck`, so
the page holds no rules of its own. Still to build in this phase: approve,
reject and edit, the settings panel, and the timers.

Build the Deck's Clipper page (queue, clip cards with preview, score,
timestamps, reasons, predicted potential, Approve / Reject + reason / Edit /
Regenerate), the settings panel (allowlisted keys to `config.json`), the
cost panel, the `actions` table consumed by Clipper, the RSS watcher, and
the timers. **Start both API audits now**, because they take weeks.

**Done when:** a week of nightly runs, all decisions made on the iPad, no
terminal needed.

### Phase 4 — publishing

*Started early (2026-09-28):* **Approve & post** on each clip card. Each
clip's words are drafted at render (`postcopy.py`): a model draft that code
checks (no invented numbers, platform limits, the source's required tags
and credit always added), or the first sentence when no model is
available. YouTube posting goes through the Data API (`youtube.py`), signed
in once with Google's device-code flow from the iPad; uploads stay private
until the project's audit passes, and the page shows what YouTube applied.
TikTok is manual (Download, Copy caption, Mark posted) until a TikTok app
is approved. Permission is re-checked at posting time; `reviews` keeps
every verdict and reason; `publications` is UNIQUE per clip and platform.

Build assisted publishing (download + copy buttons) first, then the YouTube
upload (private until the audit) and TikTok inbox drafts, then direct and
scheduled posting after the audits. Add the calendar, caps, posting windows
and duplicate guards.

**Done when:** two weeks of posts with zero duplicates, every post traceable
to its clip and source, and no file handling by hand once the APIs are
live.

### Phase 5 — analytics and learning

Build the snapshot jobs, the lift metric, Deck stats (top clips, lift by
hook type, source and length), the weekly learner with the holdout gate,
and the shrunk averages for categorical settings.

**Done when:** at least 30 published clips per platform, a first weight
update that is either activated or refused with a recorded reason, and the
Deck showing which scorer version picked each clip.

### Phase 6 — autonomy

Add `mode: auto`, per source. It is allowed only where you approved at least
90% of that source's last 30 clips. Every auto post still passes QC, the
score threshold and the daily caps. Add a kill switch on the Deck, and a
weekly report into the GM's briefing.

**Done when:** two weeks fully automatic on one trusted source with at most
one post per week you would have rejected.

---

## Sources (checked 2026-09-28)

- [TikTok Content Posting API guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines) · [Get started: Direct Post](https://developers.tiktok.com/docs/en/content-posting-api-get-started) · [Get started: Upload](https://developers.tiktok.com/docs/en/content-posting-api-get-started-upload-content) · [Vorp Labs on private-until-audited](https://vorplabs.com/agent-tools/tiktok-content-posting-api)
- [YouTube Data API revision history](https://developers.google.com/youtube/v3/revision_history) · [Quota and compliance audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits) · [Phyllo on 2026 quotas](https://www.getphyllo.com/post/youtube-api-limits-how-to-calculate-api-usage-cost-and-fix-exceeded-api-quota)
- [YouTube Analytics metrics](https://developers.google.com/youtube/analytics/metrics) · [Analytics revision history (engagedViews)](https://developers.google.com/youtube/analytics/revision_history)
- [TikTok Display API overview](https://developers.tiktok.com/docs/en/display-api-overview)
- [Three-minute Shorts](https://support.google.com/youtube/answer/15424877) · [Shorts monetization policies](https://support.google.com/youtube/answer/12504220)
- [Groq Whisper Large v3 Turbo](https://console.groq.com/docs/model/whisper-large-v3-turbo) · [Groq pricing](https://www.cloudzero.com/blog/groq-pricing/)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [OpenCV YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- [Gemini API pricing (Flash-Lite, retirement date)](https://www.cloudzero.com/blog/gemini-pricing/)
- [Whop Content Rewards](https://docs.whop.com/memberships-and-access/third-party-apps/content-rewards) · [Whop clipping rates](https://openclip.app/guides/whop-clipping-guide)
- Prior art: [OpenShorts](https://github.com/mutonby/openshorts) (face tracking, blur and split modes), [AI-Youtube-Shorts-Generator](https://github.com/Anil-matcha/AI-Youtube-Shorts-Generator)
- On downloading: [yt-dlp legality overview](https://audioutils.com/blog/is-yt-dlp-legal) — the licence and the platform's terms are separate questions
