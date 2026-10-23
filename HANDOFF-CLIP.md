# Handoff: Clip (permitted video into captioned short clips) and Spotter

Written 2026-10-21 by the Claude session that built most of this repo, for a
new chat that will look after **Clip**: it takes long videos the owner is
allowed to use (podcasts in paid clipping campaigns, mostly), finds the best
moments, cuts each into a captioned 9:16 short, and posts the ones the owner
approves. Its research assistant is **Spotter**. Read this top to bottom once;
the rest of the repo will make sense after.

- This repository, on the branch everything lives on:
  https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- This note as raw text (easiest for another AI to read):
  https://raw.githubusercontent.com/cljxs/cljxs/claude/ai-agent-ecosystem-setup-0280zz/HANDOFF-CLIP.md
- Read before changing anything: [CLAUDE.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md)
  (how to change things without breaking them),
  [ROUTING.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md)
  (its "Clipper" table maps each stage to its module - start there), and
  Clip's own [README](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/clipper/README.md)
  (setup and commands) and [DESIGN.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/clipper/DESIGN.md)
  (why it is built this way). In a Claude Code session on this repo,
  CLAUDE.md and ROUTING.md load by themselves.

The repository is **public**. Nothing secret is in it and nothing secret may
ever be put in it: no API keys, tokens, passwords, IP addresses, email
addresses, or live data (videos, clips, transcripts and the database are
gitignored and live only on the server).

**Clip never downloads from YouTube** (YouTube's terms forbid it) and never
cuts a video without a rights holder's permission on record. **Nothing is
posted until the owner approves it.** Keep all three.

---

## 1. Who the owner is, and how to work with them

- **Not a programmer.** Plain words; say what a timer, a commit or a render
  does. Outcome first, short.
- **Works from an iPad**: Termius (terminal) to reach the server, Safari for
  Clip's house in the village. So:
  - **Every command is ONE line, in a code block to paste.** The terminal
    flattens multi-line pastes, and the iPad keyboard turns a typed `--` into a
    long dash - they paste commands. A button in Clip's house beats a command.
  - Pages must work on a tablet and a phone.
- **They judge clips by watching them** and send screenshots (framing off,
  wrong subtitles, gibberish hooks). Treat each as a bug report: fix it *and*
  add the test that would have caught it.
- They also ask for **hooks** (attention-grabbing lines) for a clip's title -
  write those directly when asked, in their voice: short, bold, a little
  provocative, sports-podcast style.
- **They like to choose.** 2-4 options with a recommendation for anything big.
- **Money matters.** Say what something costs before building it.
- After each piece of work: tested, committed and pushed to the branch above,
  then the single-line commands to run on the server.
- **Say "verified" (you ran it and read the output) or "believed" (it should
  work).** Model output (hooks, titles) and real YouTube/Dropbox replies cannot
  be seen from a cloud session without keys - say so when you have not seen them.

---

## 2. The ecosystem, briefly

One Ubuntu server (a DigitalOcean droplet, **2 GB of RAM, one CPU** - Clip is
the heaviest thing on it), repo cloned at `/root/ecosystem`. Agents wake on
**systemd timers** and code judges their work by what is on disk. Other agents
share the server, the budget and the web app: Ace, Belfort, Bolt (paper
trading), Peter (sports props), Scout and Emily (Etsy shop), Paul (local
websites), Stan, the Agent Room, Fury and the GM. ROUTING.md has a row for each.

**Clip is mostly code, not a model.** Transcription is local (faster-whisper,
free); moments are scored by formulas; rendering is ffmpeg. A model is used
only for words: post titles/captions/hashtags and "Write hooks" - `copy_model`
in `clipper/config.py` (`openai/gpt-5-mini` through OpenRouter), inside Clip's
own cap (`daily_budget_usd`, **$0.25** a day) and the ecosystem's shared $1.50
(`tasks/limits.json`). A hooks draft is about a tenth of a cent.

**Clip's house** is in the village (`/village` on the Command Deck,
`mission-control-api/`, Node/Express, port 3001, reached over Tailscale) - there
is no separate page. Its routes are `mission-control-api/clip.js`; its screen is
in `mission-control-api/public/village.html` (anchors: `openClipPanel`,
`drawClip`, `drawPost`, `clipCard`, `editSubtitles`, `fixSubtitles`, `clipHooks`).
The JavaScript holds no rules: each route runs `python -m clipper ...` with an
argument array (the venv's Python at `clipper/.venv/bin/python`, which has
faster-whisper and OpenCV). A changed `village.html` needs only a reload; a
changed `clip.js` needs `python3 scripts/restart-deck.py` (never `systemctl
restart` - it kills jobs the Deck started).

---

## 3. How Clip works

**The timer:** `clip-pickup` every 15 minutes runs `python -m clipper pickup --run`:
one queued link from Clip's house a tick, any new video in a source's shared
Dropbox folder, then every stage for whatever is waiting. Runtime data lives
under `CLIPPER_HOME` (default `clipper/var/`): `clipper.db` (SQLite),
`media/<video>/` (the source video and its transcript), `clips/<video>/NN.mp4`.

**The stages** (`clipper/pipeline.py`, a state machine on `videos.stage`):

1. **Rights gate** (`clipper/rights.py`): a video belongs to a *source* that
   names its permission (`own`, `permission` - a campaign or written OK, `cc-by`,
   `cc-by-sa`, `public-domain`) with written evidence; no source, no clip.
2. **Ingest** (`clipper/ingest.py`, `clipper/dropbox.py`, `clipper/inbox.py`):
   a Dropbox file link (swapped to `dl=1`), a file on the server, or a huge
   video read in place with `--remote` (range requests, nothing stored).
3. **Transcribe** (`clipper/transcribe.py`): faster-whisper `base.en`, ten
   minutes at a time (the whole 81-minute episode at once was killed on 2 GB),
   word timestamps, cached; per-source **spellings** fix names Whisper mishears.
4. **Find moments** (`clipper/moments.py`, `clipper/score.py`): every
   whole-sentence window 20-60 s, scored on measured features (strong opening
   line, stands alone, ends on a pause, energy, laughter...). Up to 5 clips a
   video **plus 2 funny ones** picked for the laugh they get (a pause as loud as
   the talking). **Hooks to hunt for** (`Topic: term, term; ...`) give each
   topic its own clip first; the source's **avoid** list and sponsor reads are
   never clipped. **YouTube comments' timestamps** ("pins") favour the moments
   viewers pointed at (`PIN_BONUS` in `score.py`).
5. **Render** (`clipper/render.py`, `clipper/framing.py`, `clipper/captions.py`):
   1080x1920, each camera shot framed on its people (YuNet face detection: crop,
   split screen, or full frame on a blur; a wall of faces or a still picture
   is not "the people"), captions burned in 3 words at a time with the spoken
   word lit, loudness evened, a campaign's watermark placed where nothing covers
   it. **Since 2026-10-20 each clip also keeps a copy without subtitles**
   (`NN.clean.mp4`), so a subtitle fix redoes only the subtitles.
6. **Words for the post** (`clipper/postcopy.py`): title, caption ending in a
   question, hashtags, plus the source's required tags and credit line; code
   refuses a number the clip never says.

**In Clip's house, each clip card:** play it, **Approve & post** (YouTube by
code; TikTok gets Download / Copy caption / **Mark posted**, because automatic
TikTok posting needs an approved developer app), **Edit text**, **New draft**,
**Reject** (asks why - kept for learning), **Fix subtitles**, and **Delete**.

- **Fix subtitles** (`editSubtitles`): the clip plays beside one box per
  caption; tapping a caption's time plays just that moment. **Save** keeps the
  fixes without touching the video; **Update video** burns them in (subtitles
  only, about half the time of a full remake). A clip with saved fixes not yet
  in the video cannot be approved.
- **Delete**: the clip's video goes and it leaves the list; its record stays
  so the moment is never cut again and a past post is never repeated. Not a
  rejection - nothing is learned from it.
- **Storage:** a clip's video files are deleted automatically once it is posted
  everywhere or rejected. `python -m clipper storage` shows where the disk goes;
  `python -m clipper cleanup` lists, and with `--yes` deletes, finished clips'
  files; `--sources` also deletes original videos with no clip left waiting
  (no more clips can then be cut from them). It freed 1.05 GB on 2026-10-20.

**Add a video** (top of Clip's house): pick the source, paste the Dropbox link
to the video **file**, the title, optionally the episode's **YouTube link**
(only its comments are read - official API, nothing downloaded or stored), then
**Write hooks** (drafts the hooks to hunt for from the title and comments;
always read and edit them) and **Add to Clip's queue**.

**Spotter** (`clipper/trends.py`): with a free YouTube Data API key, finds
trending creators and the moments of theirs other people's clips are getting
views on, and whether that moment is in footage Clip may cut (then **remake**
cuts Clip's own version). About 800 of the free 10,000 daily units a run; stops
itself at 3,000. Comments for hooks cost 1 unit per 100 comments.

**Posting:** YouTube through the official API, signed in once with
`python -m clipper youtube-login`. **YouTube keeps uploads private until the
Google Cloud project passes YouTube's API audit** - the owner has to apply; it
takes weeks. Max 6 uploads a day per platform.

---

## 4. Sources (as last known - `python -m clipper source list` on the server is the truth)

- **gils-arena** - Gil's Arena (permission: clipping campaign). Funny clips
  plus arguments; avoid list "politics, ex-wife"; per-episode spellings
  (LaMelo, KAT...). Most clips so far.
- **club-520** - Club 520 / 520 in the Morning (Jeff Teague, DJ Wells, B Hen,
  Mook), added 2026-10-20 (permission: clipping campaign; no watermark, tags or
  credit required). Same mix as Gil's Arena.
- **curious-mike** - a campaign with a required watermark ("YT: @mpj",
  cut out of white, placed at 880 on the guest's shirt) and a clip list (Notion,
  `--cutlist`) with its own hooks; huge 4K episodes read with `--remote`.
- The Twitch auto-clipper (`twitch.py`, the `clip-watch` timer) was **removed
  on 2026-10-20** at the owner's request ("I don't use it anymore"); its old
  settings are skipped by `config.load` so they cannot stop Clip.

Source rules are set with `python -m clipper source rules NAME` and `--tags`,
`--credit`, `--watermark` (`--cut-out-white`, `--watermark-top`), `--hunt`,
`--avoid`, `--spell`, `--dropbox-folder`, `--cutlist`.

---

## 5. Where things are, and tests

    clipper/                 the package - one module per stage (ROUTING.md "Clipper" table)
    clipper/cli.py           every command (grep `def cmd_<name>`); `python -m clipper -h`
    clipper/config.py        DEFAULTS - every setting; a config.json key it does not know stops Clip
    clipper/db.py            schema and numbered migrations (add a migration, never edit an old one)
    clipper/storage.py       what is kept on disk and when it goes
    deploy/clip-pickup.*     the 15-minute timer
    mission-control-api/clip.js     Clip's routes
    on the server only: clipper/.venv/, and clipper/var/ (CLIPPER_HOME) - the
      database, media, clips, and credentials.env (YOUTUBE_API_KEY,
      YOUTUBE_CLIENT_ID/SECRET, the YouTube sign-in, DROPBOX_APP_KEY/SECRET),
      written by `scripts/set-credential.py clip <NAME>` - never under agents/

Useful commands (all `clipper/.venv/bin/python -m clipper ...` from `/root/ecosystem`):
`status` (videos, stages, errors, today's spend), `clips`, `inbox`,
`retry <video>`, `refind <video>`, `framing <clip>` (why a clip is framed the
way it is - every face in every shot), `subtitles <clip>`, `storage`, `cleanup`,
`forget <video>` (a test run, and its clips), `publish`.

**Tests:** `tests/test_clipper.py` - over 220, and slow (about 30 minutes;
most render real video). Run the class you touched
(`python3 -B -m unittest tests.test_clipper.Posting -b`) and the fast suite
(`python3 -B -m unittest tests.test_parsers -b` - its `ClipHasAStudio` class
covers Clip's house), then the whole clipper file in the background before a
push that changes rendering or scoring. Fixtures are real: a LibriVox reading's
words, five minutes of "Our Miss Brooks" (1948) for laughter, real face images
for framing. `python3 scripts/check-secrets.py` before every push.

---

## 6. The rules this ecosystem paid for (apply them to Clip)

1. **Rights first.** No source with evidence, no clip; never download from
   YouTube; a campaign's watermark is never covered, cropped or retyped - a clip
   with a covered watermark is a clip the campaign does not pay for.
2. **Anything a script can measure, a script measures** - scores, framing,
   timings, loudness. The model only writes words, and code checks those
   (no invented numbers; hooks keep the speakers' words, never commenters'
   reactions - 2026-10-20).
3. **Test against the real thing.** The framing bugs ("a wall of faces", "a
   picture of a face is not a person") were fixed only once a real frame was a
   fixture. Each test names its bug and was checked by putting the bug back.
4. **One fact, one place.** `clip_ass` writes a clip's subtitles for both a
   full cut and a subtitles-only update; `storage.py` owns what is kept.
5. **Mind the 2 GB server.** Transcribe in chunks, render one clip at a time,
   nice the heavy jobs; a bigger Whisper model was too much memory.
6. **Outward actions wait for the owner**: posting is the Approve button.
7. **Credentials never leave the server** (`scripts/set-credential.py clip <NAME>`,
   typed by the owner). Never ask for a key in chat; never print one.
8. **Two functions with one name in `village.html` means one never runs** -
   Write hooks silently ran the wrong one until 2026-10-20; a test now fails on
   any duplicate.
9. **Never `pkill -f` a pattern** on the server; stop processes by PID.
10. **Every commit ends with the session's attribution lines; no model names in
    commits, code or pull requests. Pull requests only when the owner asks.**

---

## 7. Where things stand (2026-10-21)

- **Club 520** was just added. For its first episode (the 2026-10-20 "520 in
  the Morning" NBA opening-day show) the owner was given hooks written from the
  episode description: Sixers vs Knicks, Celtics vs Pistons, Thunder vs Spurs,
  MVP Picks, New-Look Sixers, Championship Pick, NFL Week 6. Ask whether it was
  queued and how its clips came out.
- **Write hooks** was fixed twice on 2026-10-20 (the result now fills the box;
  the model is told to use the speakers' words and code drops comment
  reactions). The new prompt has not been seen running against a real
  episode - check the first drafts with the owner. If they are still poor, the
  next step offered was moving hooks to Sonnet on the owner's Claude plan
  (no cost per press; a few seconds slower).
- **Comment timestamps ("pins")** favour moments viewers pointed at; the owner
  wanted to "see how well it works over the next couple of days".
- **YouTube audit** - uploads stay private until the owner applies and passes.

---

### Since this note was written

- 2026-10-21: the Teague episode was killed for memory three times - the
  copy without subtitles had no `-t` and ran on to the end of the episode
  (fixed, `TheCleanCopyStopsWithTheClip`). The Add a video list now shows
  each video's real stage.
- 2026-10-22: **complete thoughts** - `clipper/stories.py` has Sonnet on
  the plan mark where each story and argument starts and ends; marked
  moments run to 90 s and windows cutting into one are skipped. Write hooks
  searches the web on the plan and adds the evergreen debates. The owner
  chose not to re-cut Teague: judge it on the next episodes.
- 2026-10-23: captions skipped sentences - Whisper's voice filter dropped
  crosstalk (clip 106: 24 s uncaptioned; `clipper holes 106 --listen` heard
  it). Each transcript is now filled (`transcribe.fill`); older videos with
  `clipper fill VIDEO --recut`. Update video shows a progress bar.

## 8. Links

- Repo (branch): https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- CLAUDE.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md
- ROUTING.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md
- Clip's README and DESIGN: https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz/clipper
- YouTube Data API: https://developers.google.com/youtube/v3 - audit form: search "YouTube API Services - Audit and Quota Extension"
- Dropbox API apps: https://www.dropbox.com/developers/apps
- faster-whisper: https://github.com/SYSTRAN/faster-whisper - ffmpeg: https://ffmpeg.org/documentation.html
- Claude Code on the web: https://code.claude.com/docs/en/claude-code-on-the-web
