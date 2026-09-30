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

## Approving and posting

Every clip gets its words when it is rendered: a title, a caption and
hashtags, drafted by a model from the clip's own transcript. Code refuses
a title with a number the clip never says, cuts everything to each
platform's limits, and adds your source's required tags and credit line to
every post. With no model available (no key, or today's $1 spent), the draft
is the clip's first sentence.

In Clip's studio each clip card shows those words, with four buttons:
- **Approve & post** posts it to YouTube, and sets up TikTok for you to post.
- **Edit text** lets you write the words yourself (they're never
  overwritten).
- **New draft** asks the model for another.
- **Reject** asks why, and keeps the reason so Clip can learn from it.

A campaign's rules (required tags, and a credit line such as "Clip from
@creator") are set once per source:

```
cd /root/ecosystem && python3 -m clipper source rules mysource --tags "#clipping #creatorname" --credit "Clip from @creatorname"
```

A campaign that requires a watermark (Curious Mike's "YT: @mpj") gets its
PNG set once, from a file or an https link, and Clip burns it into every clip
from that source, first frame to last, at its own shape and colour:

```
cd /root/ecosystem && clipper/.venv/bin/python -m clipper source rules curious-mike --watermark "https://...watermark.png"
```

It goes centred near the top (`watermark_top`, 300 of 1920 px), under the
status bar and TikTok's tabs and far above the captions. Clip refuses a
spot where anything would cover it, and a clip is never rendered without
it: if the file goes missing, the render fails instead.

Every caption asks the viewer a question, because comments are what the
campaigns are judged on. A draft without one gets "Agree or disagree?".

The watermark must have a transparent background. The image on a
guidelines web page is often a flattened copy on a white canvas: Curious
Mike's Notion image is one, and would put a white box over every clip. Clip
refuses a PNG without transparency. If the campaign can't supply one,
`--cut-out-white` keeps its pixels and removes only the white canvas
(`clipper/cutout.py`). Over white, the result matches the supplied file to
within one level on every pixel. It is not retyped, which the rules forbid.
`--watermark-top` sets where it sits for that source. For Curious Mike that
is 880, on the guest's black shirt, where the white lettering reads.

A campaign's list of moments to hunt for goes in `--hunt`, in the
campaign's order. Each topic gets its own clip before the rest are chosen
by score. `--spell` fixes the names Whisper mishears ("tray" for Trae,
"nicks" for Knicks), in the captions and in the topic search. Both are set
per source. If a video was already searched before the list was set,
`refind VIDEO` searches it again (only before it has clips). The find
event in `status` names any topic it could not find:

```
cd /root/ecosystem && clipper/.venv/bin/python -m clipper source rules curious-mike --hunt "Knicks: knicks, nicks, chant; Pat Beverley: pat bev, beverley" --spell "Trae Young: try young; Knicks: nicks"
```

A Dropbox share link can be passed to `ingest` as it is. Clip swaps `dl=0`
for `dl=1` to get the file rather than the preview page. It refuses folder
links, because they download as a zip.

Footage too big for the disk stays where it is with `--remote`. Curious
Mike's episodes are 27 GB of 4K each, and the droplet has about 44 GB free.
With `--remote`, the audio is streamed once for the transcript, and each
clip reads only its own seconds, using range requests:

```
cd /root/ecosystem && clipper/.venv/bin/python -m clipper ingest curious-mike "DROPBOX_LINK" --remote --title "Trae Young"
```

Measured 2026-09-29 on the real episode, on one core: a 30-second clip from
minute 50 took 80 s, and nothing was stored but the clip. The certificate
is checked on every read (ffmpeg skips this check unless told), and a server
that ignores range requests is refused, because it would give broken clips
rather than an error. If the campaign removes the link, clips from that
video can no longer be made.

**TikTok** has no automatic posting yet: that needs an approved TikTok
developer app, which needs a website and a review. Until then an approved
clip shows **Download**, **Copy caption** and **Mark posted**. Marking it
posted means it's never offered again.

## Posting to YouTube (one-time setup)

Use the same Google Cloud project as Spotter's key.

1. **Menu → Google Auth Platform.**
   - **Branding** (or *Get started*): the app name is `Clip`, and both
     emails are yours.
   - **Audience**: choose *External*, then **Publish app** so it reads *In
     production*. While it stays in *Testing*, Google ends the sign-in every
     7 days.
2. **Google Auth Platform → Clients → Create client.** For *Application
   type* pick **TVs and Limited Input devices**, name it `Clip`, then choose
   Create. **The client secret is shown only this once** (Google's rule since
   2025), so set it on the droplet before closing that screen. If it's
   lost, add a new secret to the same client.
3. Put the two values on the droplet, one at a time:

```
cd /root/ecosystem && python3 scripts/set-credential.py clip YOUTUBE_CLIENT_ID
```
```
cd /root/ecosystem && python3 scripts/set-credential.py clip YOUTUBE_CLIENT_SECRET
```

4. Sign in. The command prints a code; enter it at google.com/device on the
   iPad and pick your channel:

```
cd /root/ecosystem && python3 -m clipper youtube-login
```

Anything approved before you signed in is waiting. Send it with
`python3 -m clipper publish`.

**Private until audited.** YouTube keeps uploads from a new Google Cloud
project *private* until the project passes YouTube's API compliance audit,
whatever Clip asks for. The studio shows the privacy YouTube actually
applied. To make approved clips go out public, apply for the audit (the
"YouTube API Services - Audit and Quota Extension" form in Google's
YouTube API documentation). It takes weeks. Until it passes, a private
upload can't be switched to public.

## Commands

| Command | Does |
|---|---|
| `python -m clipper status` | Videos, stages, errors, and today's spend. |
| `python -m clipper clips` | Clips with score, timestamps, reasons and file. |
| `python -m clipper retry <video>` | Puts a failed video back at the stage that failed. |
| `python -m clipper forget <video>` | Removes a video and every clip made from it, such as a test run. It refuses if a clip was posted, unless you add `--including-posted`; the post stays online. |
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
