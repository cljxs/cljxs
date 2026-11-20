"""The inbox: a video link pasted into Clip's house, clipped on the next
pickup (owner, 2026-10-14: "a spot in Clip's house where I can put the video
Dropbox link in and it will create clips on its next cycle").

    queue()       checks the link and the source now, and saves it - nothing
                  is downloaded while the page waits
    drain_one()   the pickup timer (every 15 minutes) takes the oldest queued
                  link, ingests it through ingest.py - the one place that
                  fetches, hashes and refuses duplicates - and the same run
                  clips it
    hooks()       the hunt list written from the video's title by a model,
                  checked by code; the owner can edit it before queueing

A video's hunt list is its own (videos.hunt), so two videos from one source
can each have theirs; a video without one uses its source's, as before.

One link a pickup: a long video is a long download, transcription and
render on a one-CPU server, and the next tick takes the next.
"""

import json
import re
import urllib.parse

from clipper import db, ingest, postcopy, score

YOUTUBE = ("youtube.com", "youtu.be")
MAX_TITLE = 200
HOOKS_ESTIMATE = 0.005          # the most one hooks draft is expected to cost (comments included)
HOOK_TOPICS = (2, 8)            # how many topics a usable list has
TERMS_PER_TOPIC = 6
# Words a transcript is full of: a topic hunted by these matches everything.
GENERIC = {"the", "and", "game", "games", "good", "great", "like", "really", "guy", "guys",
           "people", "thing", "things", "team", "teams", "play", "player", "players", "season",
           "year", "years", "time", "man", "know", "think", "right", "yeah", "basketball",
           "football", "video", "show", "episode", "podcast"}
TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 '\-.]{1,30}$")
# A commenter's reaction, never a speaker's words. Club 520's first draft
# (2026-10-20) hunted "uggh he a bandwagoneer" and "anthony black what" -
# the comments' wording, which the transcript will not hold. A term with one
# of these in it goes; so does one longer than a speaker's short phrase.
REACTIONS = {"lol", "lmao", "lmfao", "rofl", "haha", "hahaha", "omg", "wtf", "bruh", "bro", "uggh",
             "ugh", "ayo", "sheesh", "nah", "fr", "deadass", "smh", "what", "yo", "w", "l", "💀"}
TERM_WORDS = 3
NAME_WORDS = 4


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def check_link(url):
    """The link as it will be fetched, or ValueError saying what to paste
    instead. A Dropbox folder, a YouTube page and plain http are refused
    here, at the moment of pasting, not fifteen minutes later."""
    url = (url or "").strip()
    if not url.startswith("https://"):
        raise ValueError("paste an https:// link - a Dropbox file's Share -> Copy link")
    host = _host(url)
    if any(host == h or host.endswith("." + h) for h in YOUTUBE):
        raise ValueError("Clip does not download from YouTube (its terms forbid it) - download the "
                         "file from YouTube Studio or the campaign, put it in Dropbox and paste that link")
    if ingest.is_dropbox_folder(url):
        raise ValueError("that is a Dropbox folder link - open the folder, open the one video and copy "
                         "that file's link")
    return url


def queue(conn, source, url, title=None, hunt=None, youtube=None):
    """Save a link for the next pickup. Returns the inbox row. `youtube`, the
    video's YouTube page, is read for its comments when the video is picked
    up: the moments viewers pointed at are favoured when clips are chosen."""
    url = check_link(url)
    if youtube:
        video_id(youtube)                       # a bad link is refused now, not at pickup
    src = conn.execute("SELECT * FROM sources WHERE name = ? AND active = 1", (source,)).fetchone()
    if not src:
        raise ValueError(f"no active source named {source!r} - a video has to belong to a source "
                         f"you have permission for")
    key = ingest.link_key(url)
    have = conn.execute("SELECT * FROM inbox WHERE link_key = ? AND state IN ('queued', 'working', 'done')",
                        (key,)).fetchone()
    if have:
        raise ValueError(f"that link is already in the inbox (#{have['id']}, {have['state']})")
    topics = score.parse_hunt(hunt) if hunt else []
    with conn:
        cur = conn.execute(
            "INSERT INTO inbox (source, url, link_key, title, hunt, youtube, state, added_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (source, url, key, (title or "").strip()[:MAX_TITLE] or None,
             json.dumps(topics) if topics else None, (youtube or "").strip() or None, db.now(), db.now()))
    return conn.execute("SELECT * FROM inbox WHERE id = ?", (cur.lastrowid,)).fetchone()


def _set(conn, item_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols) + ", updated_at = ?"
    with conn:
        conn.execute(f"UPDATE inbox SET {sets} WHERE id = ?", (*cols.values(), db.now(), item_id))


def save_pins(conn, cfg, video_id_, url, yt=None):
    """The moments viewers pointed at, read now and kept on the video as
    numbers only. A failure is a note - the video is clipped either way."""
    try:
        pins = [list(p) for p in timestamps(read_comments(conn, cfg, url, yt))]
    except Exception as exc:
        return f"comments not read ({str(exc)[:120]}) - clipped without them"
    with conn:
        conn.execute("UPDATE videos SET pins = ? WHERE id = ?", (json.dumps(pins) if pins else None, video_id_))
    return (f"viewers pointed at {', '.join(clock_of(t) for t, _n, _l in pins)}" if pins
            else "no timestamps in the comments")


def drain_one(conn, paths, cfg, ingester=None, yt=None):
    """Ingest the oldest queued link. Returns the inbox row afterwards, or
    None when nothing is queued. A link that will not ingest is marked
    failed with the reason, and the next tick moves on."""
    item = conn.execute("SELECT * FROM inbox WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
    if not item:
        return None
    _set(conn, item["id"], state="working", note=None)
    ingester = ingester or ingest.ingest
    try:
        try:
            row, new = ingester(conn, paths, item["source"], item["url"], item["title"])
        except RuntimeError as exc:
            # Too big for the disk: a Dropbox file can be read where it is
            # instead (Dropbox answers range requests), so clips read only
            # their own seconds.
            if "not enough disk" not in str(exc) or not ingest.is_dropbox(item["url"]):
                raise
            row, new = ingester(conn, paths, item["source"], item["url"], item["title"], remote=True)
        if item["hunt"]:
            with conn:
                conn.execute("UPDATE videos SET hunt = ? WHERE id = ?", (item["hunt"], row["id"]))
        note = None if new else f"already had it: video {row['id']}"
        if item["youtube"]:
            note = "; ".join(x for x in (note, save_pins(conn, cfg, row["id"], item["youtube"], yt)) if x)
        _set(conn, item["id"], state="done", video_id=row["id"], note=note)
        db.event(conn, "inbox", f"inbox #{item['id']} -> video {row['id']}"
                                + ("" if new else " (already had it)"), video_id=row["id"])
    except Exception as exc:
        _set(conn, item["id"], state="failed", note=str(exc)[:300])
        db.event(conn, "inbox", f"inbox #{item['id']} failed: {str(exc)[:200]}", level="warn")
    return conn.execute("SELECT * FROM inbox WHERE id = ?", (item["id"],)).fetchone()


def retry(conn, item_id):
    """Put a failed link back in the queue."""
    n = conn.execute("UPDATE inbox SET state = 'queued', note = NULL, updated_at = ? "
                     "WHERE id = ? AND state = 'failed'", (db.now(), item_id)).rowcount
    conn.commit()
    if not n:
        raise ValueError(f"inbox #{item_id} is not a failed link")


def recent(conn, n=10):
    return [dict(r, hunt=json.loads(r["hunt"]) if r["hunt"] else [], progress=progress(conn, r))
            for r in conn.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (n,))]


# What a downloaded link's video is doing now, from videos.stage. 2026-10-21:
# the house said "in the studio" for an inbox "done", which only means
# downloaded - the Teague episode read as finished while it was still being
# transcribed, and its clips were nowhere.
STAGE_WORDS = {"ingested": "downloaded - writing down what is said",
               "transcribed": "transcribed - finding the moments",
               "found": "making the clips"}


def progress(conn, item):
    if item["state"] != "done" or not item["video_id"]:
        return None
    v = conn.execute("SELECT stage, error FROM videos WHERE id = ?", (item["video_id"],)).fetchone()
    if not v:
        return "its video was forgotten"
    n = conn.execute("SELECT COUNT(*) FROM clips WHERE video_id = ? AND status != 'deleted'",
                     (item["video_id"],)).fetchone()[0]
    if v["stage"] == "failed":
        return f"failed: {(v['error'] or 'no reason kept')[:200]}"
    if v["stage"] == "done":
        return f"{n} clip(s) ready" if n else "finished - no moment scored high enough"
    words = STAGE_WORDS.get(v["stage"], v["stage"])
    if n:
        words += f" ({n} made so far)"
    if v["error"]:
        words += f" - last try: {v['error'][:160]}; it tries again at the next pickup"
    return words


# ------------------------------------------------------------------ hooks

HOOKS_PROMPT = """A long video is about to be cut into short clips. From its title, list
the moments a viewer would most want clipped, in order of priority - the
people, teams, claims, arguments and funny bits the title promises.

For each moment give the WORDS THE SPEAKERS ON THE VIDEO WILL SAY when it
comes up: names as they are spoken (surname, first name, nickname, team,
city), and the plain nouns of the subject. The list is matched against a
transcript of their speech word for word, so:
- never a viewer's wording, reaction or joke (lol, what, uggh, bro) - viewers
  are not on the video;
- each term 1 to 3 words; never generic words (game, good, team, season);
- the name is a plain label a reader understands, 1 to 4 words, e.g.
  "Sixers Season", "Anthony Black", "Theme Song".
2 to 8 moments, 2 to 6 terms each, terms lower case.

EVERGREENSEARCH
Reply with JSON only:
{"topics": [{"name": "short label", "terms": ["word", "two words"]}]}

Example, for "Trae Young on the Knicks chants and Pat Beverley":
{"topics": [{"name": "Knicks", "terms": ["knicks", "chant", "msg"]},
            {"name": "Pat Beverley", "terms": ["beverley", "beverly", "pat bev"]}]}

Source: SOURCE
Title: TITLE"""


# ------------------------------------------------------------------ comments
#
# Owner, 2026-10-20: "We need to create better hooks. Can you access comments
# of the video to see what they are talking about?" A title says what the
# video promises; its comments say what people actually came for - and they
# write timestamps ("14:32 when he said...") pointing at the moments they
# cared about. Read through YouTube's official API with Spotter's key, at one
# quota unit a hundred comments, counted against the same daily cap. No video
# is fetched, and nothing anyone wrote is kept: the comments go into the one
# hooks question and the timestamps are shown to the owner, then both are gone.
VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")
COMMENT_PAGES = 2               # 100 comments a page, most relevant first
COMMENTS_SHOWN = 40             # the most liked of them go into the question
COMMENT_CHARS = 220
TIMESTAMP_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?![\d:])(?!\s*[ap]\.?m\b)", re.I)
STAMP_GAP = 20                  # s: timestamps this close are one moment
STAMPS_SHOWN = 8


def video_id(url):
    """The 11-character id from any YouTube link, or ValueError."""
    m = VIDEO_ID_RE.search(str(url or ""))
    if not m or not any(_host(url) == h or _host(url).endswith("." + h) for h in YOUTUBE):
        raise ValueError("that is not a YouTube video link")
    return m.group(1)


def read_comments(conn, cfg, url, yt=None):
    """[{text, likes}] - the video's top comments, most relevant first."""
    from clipper import trends
    vid = video_id(url)
    yt = yt or trends.YouTube(conn, cfg)
    out, token = [], None
    for _ in range(COMMENT_PAGES):
        params = dict(part="snippet", videoId=vid, order="relevance", maxResults=100, textFormat="plainText")
        if token:
            params["pageToken"] = token
        try:
            body = yt.get("commentThreads", **params)
        except trends.ApiError as exc:
            if exc.reason == "commentsDisabled":
                raise ValueError("comments are turned off on that video") from None
            raise
        for item in body.get("items") or []:
            sn = (((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {})
            text = " ".join(str(sn.get("textOriginal") or sn.get("textDisplay") or "").split())
            if text:
                out.append({"text": text, "likes": trends.as_int(sn.get("likeCount")) or 0})
        token = body.get("nextPageToken")
        if not token:
            break
    return out


def clock_of(seconds):
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def timestamps(comments):
    """[(seconds, comments, likes)] - the moments viewers pointed at, the
    most pointed-at first. A comment counts once per moment."""
    marks = []
    for c in comments:
        seen = set()
        for h, m, s in TIMESTAMP_RE.findall(c["text"]):
            t = int(h or 0) * 3600 + int(m) * 60 + int(s)
            if t not in seen:
                seen.add(t)
                marks.append((t, c["likes"]))
    groups = []
    for t, likes in sorted(marks):
        if groups and t - groups[-1][-1][0] <= STAMP_GAP:
            groups[-1].append((t, likes))
        else:
            groups.append([(t, likes)])
    out = [(g[0][0], len(g), sum(l for _t, l in g)) for g in groups]
    return sorted(out, key=lambda x: (-x[1], -x[2], x[0]))[:STAMPS_SHOWN]


def comments_block(comments):
    """The comments as the hooks question shows them: the most liked, then
    the timestamps code counted."""
    top = sorted(comments, key=lambda c: -c["likes"])[:COMMENTS_SHOWN]
    lines = ["", "What viewers wrote under the video, most liked first. Use it to decide WHICH",
             "moments to list - the people, arguments and funny bits they react to matter more",
             "than the title. Never copy their words as terms or names: give the words the",
             "speakers will say about those moments."]
    lines += [f"- ({c['likes']} likes) {c['text'][:COMMENT_CHARS]}" for c in top]
    stamps = timestamps(comments)
    if stamps:
        lines += ["", "Moments viewers pointed at (time in the video, comments, likes): "
                  + "; ".join(f"{clock_of(t)} ({n}, {l})" for t, n, l in stamps)]
    return "\n".join(lines)


def hooks_text(topics):
    """[{name, terms}] -> 'Name: t1, t2; Name2: t3' - what score.parse_hunt reads."""
    return "; ".join(f"{t['name']}: {', '.join(t['terms'])}" for t in topics)


# Owner, 2026-10-22: "like whenever Jordan vs LeBron is debated, common
# knowledge things that would be discussed". Shows like these keep coming
# back to the same arguments whatever the title says; the hosts of Gil's
# Arena and Club 520 played, so their own stories come up too.
EVERGREEN = """
Shows like this keep coming back to the same arguments, whatever the title
says. Add the one to three this episode is most likely to have, as topics
of their own, in the words the hosts would say - for example the GOAT
debate (jordan, lebron, goat, kobe), rings and legacy (rings, legacy), this
era against back then (back in the day, this era, soft), overrated and
underrated, and the hosts' own playing days (when i played, my rookie year,
in the league).
"""

# Write hooks on the owner's Claude plan searches the web first (owner,
# 2026-10-22: "ask another AI to search the web on what would be funny or
# talked about"). The cheap model it falls back to cannot search.
SEARCH = """
Before answering, search the web for what is being said this week about the
people, teams and games in the title - trades, beefs, injuries, results,
viral moments - and add the moments this episode is likely to cover. The
terms are still the words the speakers will say, not a headline's.
"""
HOOKS_TIMEOUT = 150             # seconds the plan may take, searching included


def _plan(q):
    """(answered, text) from Sonnet on the plan, allowed to search the web."""
    return postcopy._script("wake_clip_hooks", "wake.py").ask("clip", q, HOOKS_TIMEOUT, tools=("WebSearch",))


def check_hooks(doc):
    """[{name, terms}] from a model's JSON, cleaned, or ValueError. Code
    decides what is usable: generic words are dropped, a topic left with no
    terms goes, and too few topics is a failure."""
    out = []
    for t in (doc or {}).get("topics") or []:
        if not isinstance(t, dict):
            continue
        # The label is read by a person: "theme song / volume" is two labels,
        # "anthony black WHAT" a reaction stuck to a name.
        name = re.sub(r"[;:,]", " ", str(t.get("name") or "").split("/")[0])
        name = " ".join(w for w in name.split() if w.lower() not in REACTIONS)[:40]
        name = " ".join(name.split()[:NAME_WORDS])
        if name == name.lower():
            name = name.title()
        terms = []
        for term in t.get("terms") or []:
            term = re.sub(r"\s+", " ", str(term).lower().replace(";", " ").replace(",", " ")).strip()
            words = term.split()
            if (TERM_RE.match(term) and term not in GENERIC and term not in terms
                    and len(words) <= TERM_WORDS and not REACTIONS & set(words)):
                terms.append(term)
        if name and terms:
            out.append({"name": name, "terms": terms[:TERMS_PER_TOPIC]})
    out = out[:HOOK_TOPICS[1]]
    if len(out) < HOOK_TOPICS[0]:
        raise ValueError(f"the draft had {len(out)} usable topic(s) - write the hooks by hand")
    return out


def hooks(conn, cfg, title, source="", call=None, key=None, left=None, youtube=None, yt=None, notes=None,
          ask=None):
    """(hunt text, cost) for a video title - and its YouTube comments, when a
    link to it is given. Asked of Sonnet on the owner's Claude plan first,
    which searches the web (no cost per press); when the plan cannot answer,
    of the cheap model, checked against both caps before the call - the
    shared day (budget.py) and Clip's own - and its real cost recorded in
    Clip's ledger, the way post copy is. `notes`, a list, gets what the
    comments said for the owner: how many were read, and the timestamps,
    and which model wrote the hooks."""
    notes = notes if notes is not None else []
    title = (title or "").strip()[:MAX_TITLE]
    if not title:
        raise ValueError("paste the video's title first")
    slug = cfg.get("copy_model")
    if not slug:
        raise ValueError("drafting by model is switched off (copy_model is null) - write the hooks by hand")
    prompt = HOOKS_PROMPT.replace("SOURCE", source or "").replace("TITLE", title)
    if youtube:
        try:
            comments = read_comments(conn, cfg, youtube, yt)
        except Exception as exc:
            comments = []
            notes.append(f"comments not read: {str(exc)[:160]} - the hooks come from the title alone")
        if comments:
            prompt += "\n" + comments_block(comments)
            notes.append(f"read {len(comments)} comments")
            stamps = timestamps(comments)
            if stamps:
                notes.append("viewers pointed at " + ", ".join(
                    f"{clock_of(t)} ({n} comment{'s' if n != 1 else ''})" for t, n, _l in stamps))
    ok, text = (ask or _plan)(prompt.replace("EVERGREENSEARCH", EVERGREEN + SEARCH))
    if ok:
        try:
            topics = check_hooks(postcopy._script("emily_assets_hooks", "emily-assets.py").read_proof(text))
            notes.append("written on your Claude plan, after a web search")
            return hooks_text(topics), 0.0
        except ValueError as exc:
            notes.append(f"the plan's draft was not usable ({exc}) - written by the cheap model instead")
    else:
        notes.append(f"the Claude plan could not answer ({text[:120]}) - written by the cheap model, "
                     "without a web search")
    if left is None:
        left = postcopy.allowance_left()
    if left is None or left < postcopy.MIN_LEFT:
        raise ValueError("today's shared AI allowance is spent - write the hooks by hand, or try tomorrow")
    db.spend_guard(conn, cfg, HOOKS_ESTIMATE)
    key = key if key is not None else postcopy._script("preflight_hooks", "preflight.py").openrouter_key()
    if not key:
        raise ValueError("no OpenRouter key")
    doc, cost = (call or call_hooks)(prompt.replace("EVERGREENSEARCH", EVERGREEN), key, slug)
    db.record_cost(conn, "openrouter", "hooks", float(cost or 0), note=title[:80])
    return hooks_text(check_hooks(doc)), cost


def call_hooks(prompt, key, slug, timeout=60):
    """One model call, through the request postcopy already makes."""
    import urllib.request
    ea = postcopy._script("emily_assets_hooks", "emily-assets.py")
    body = {"model": slug, "max_tokens": 1500, "reasoning": {"effort": "low"},
            "response_format": {"type": "json_object"}, "usage": {"include": True},
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(ea.OR_URL, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cljxs/cljxs", "X-Title": "clip-hooks"})
    data = ea.post(req, timeout=timeout)
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    doc = ea.read_proof(text)
    if doc is None:
        raise ValueError("the reply holds no JSON object")
    return doc, ea.cost_of(data.get("usage") or {})
