# Clip — Phase 1, plus Spotter

The agent is **Clip**; its code is the `clipper` package. Its research
assistant is **Spotter**. Both live in Clip's studio in the village, north of
the town square between Emily and Fury.

Permitted long video in, captioned 9:16 shorts out, saved on the droplet.
The whole plan, including why it is built this way, is in
[DESIGN.md](DESIGN.md).

What it does today, per video:

1. **Rights gate.** Refuses any video whose source has no named permission
   and written evidence.
2. **Transcribe** locally (faster-whisper `base.en`, word timestamps, $0).
   The result is cached, so it is never redone.
3. **Find moments.** Tries every whole-sentence window 20–60 s long, scores
   each on 8 measured features, and keeps the best 5 that don't overlap.
4. **Render** each clip: 1080x1920, captions burned in with the spoken word
   highlighted, loudness evened out.
5. **Write a review sheet**, `clips/<video>/README.md`: score, timestamps,
   why each clip was chosen, and what it says.

## Install on the droplet (once)

```
cd /root/ecosystem && git pull && apt-get install -y python3-venv && python3 -m venv clipper/.venv && clipper/.venv/bin/pip install -r clipper/requirements.txt
```

The first `run` also downloads the speech model (about 140 MB, once).

## Try it on a public-domain recording

This makes a 5-minute test video from a LibriVox reading (public domain) and
runs it through everything:

```
cd /root/ecosystem && curl -sL -o /tmp/story.mp3 https://archive.org/download/short_stories14_librivox/peach_blossom_shangri_la_Tao_ps_64kb.mp3 && ffmpeg -v error -y -f lavfi -i testsrc2=size=1280x720:rate=30 -i /tmp/story.mp3 -shortest -c:v libx264 -preset ultrafast -c:a aac /tmp/story.mp4 && clipper/.venv/bin/python -m clipper source add librivox --rights public-domain --evidence "LibriVox: all recordings are public domain" && clipper/.venv/bin/python -m clipper ingest librivox /tmp/story.mp4 && nice -n 19 clipper/.venv/bin/python -m clipper run && clipper/.venv/bin/python -m clipper clips
```

## Your own videos

Add the source once, with the evidence that you may use it:

```
cd /root/ecosystem && clipper/.venv/bin/python -m clipper source add mychannel --rights own --evidence "my channel: youtube.com/@..."
```

The `--rights` options are `own`, `permission` (a campaign or a written
OK), `cc-by`, `cc-by-sa` (both need `--attribution`), and `public-domain`.

Then ingest a file on the droplet, or an https link the rights holder gave
you, and run:

```
cd /root/ecosystem && clipper/.venv/bin/python -m clipper ingest mychannel /root/videos/episode12.mp4 --title "Episode 12" && nice -n 19 clipper/.venv/bin/python -m clipper run
```

Your own YouTube uploads can be downloaded from YouTube Studio (open the
video's menu, then Download). Clipper does not download from YouTube itself,
because YouTube's terms forbid it (DESIGN.md §17).

## Spotter, the research assistant

Spotter finds the top creators who are trending, the moments of theirs that
other people's clips are getting views on right now, and whether that
moment is in footage Clip is allowed to cut. It never reposts anyone's clip.
It tells Clip which moment to cut from your permitted source.

**1. A free YouTube API key (once).** In the Google Cloud console, create a
project, open APIs & Services → Library, and enable **YouTube Data API
v3**. Then go to Credentials → Create credentials → API key. Put it on the
droplet (it is never shown):

```
cd /root/ecosystem && python3 scripts/set-credential.py clip YOUTUBE_API_KEY
```

**2. Link permitted sources to the creator's channel.** Spotter can only
remake moments from creators whose source has a channel ID. The ID starts
with `UC`; find it on the channel page under About → Share → Copy channel
ID.

```
cd /root/ecosystem && python3 -m clipper source channel mysource --channel UCxxxxxxxxxxxxxxxxxxxxxx
```

**3. Research.** Use **Research now** in Clip's studio, or run:

```
cd /root/ecosystem && python3 -m clipper spot && python3 -m clipper trends
```

`trends` marks each trending clip with what Clip can do about it:
- **in your footage**: `python3 -m clipper remake <id>` (or the button)
  cuts Clip's own version;
- **not in the footage ingested yet**: ingest that stream or video first;
- **no permission**: a lead, and usually the creator runs a clipping
  campaign you can join.

A run uses about 800 of the free 10,000 daily YouTube units, and it stops
itself at 3,000.

## Commands

| Command | Does |
|---|---|
| `python -m clipper status` | Videos, stages, errors, and today's spend. |
| `python -m clipper clips` | Clips with score, timestamps, reasons and file. |
| `python -m clipper retry <video>` | Puts a failed video back at the stage that failed. |
| `python -m clipper source list` | Sources and their evidence. |

Finished clips are saved in `clipper/var/clips/<video>/`, as `01.mp4` (best
first) plus `01.json` (everything about it). Viewing them on the iPad comes
with the Deck page in Phase 3. Until then, download them with your terminal
app's SFTP.

Settings are changed in `clipper/var/config.json`. Only the keys you change
are needed, for example `{"max_clips_per_video": 3, "crop_mode": "blur"}`.
The full list is in `clipper/config.py`, and a misspelt key is refused
rather than ignored.

## Tests

```
python3 -m unittest tests.test_clipper -v
```

They need no Python packages. The speech model is replaced by real
faster-whisper output captured from the LibriVox reading. The end-to-end
test needs ffmpeg.
