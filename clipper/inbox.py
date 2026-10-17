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
HOOKS_ESTIMATE = 0.003          # the most one hooks draft is expected to cost
HOOK_TOPICS = (2, 8)            # how many topics a usable list has
TERMS_PER_TOPIC = 6
# Words a transcript is full of: a topic hunted by these matches everything.
GENERIC = {"the", "and", "game", "games", "good", "great", "like", "really", "guy", "guys",
           "people", "thing", "things", "team", "teams", "play", "player", "players", "season",
           "year", "years", "time", "man", "know", "think", "right", "yeah", "basketball",
           "football", "video", "show", "episode", "podcast"}
TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 '\-.]{1,30}$")


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


def queue(conn, source, url, title=None, hunt=None):
    """Save a link for the next pickup. Returns the inbox row."""
    url = check_link(url)
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
            "INSERT INTO inbox (source, url, link_key, title, hunt, state, added_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (source, url, key, (title or "").strip()[:MAX_TITLE] or None,
             json.dumps(topics) if topics else None, db.now(), db.now()))
    return conn.execute("SELECT * FROM inbox WHERE id = ?", (cur.lastrowid,)).fetchone()


def _set(conn, item_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols) + ", updated_at = ?"
    with conn:
        conn.execute(f"UPDATE inbox SET {sets} WHERE id = ?", (*cols.values(), db.now(), item_id))


def drain_one(conn, paths, cfg, ingester=None):
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
    return [dict(r, hunt=json.loads(r["hunt"]) if r["hunt"] else [])
            for r in conn.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (n,))]


# ------------------------------------------------------------------ hooks

HOOKS_PROMPT = """A long video is about to be cut into short clips. From its title, list
the moments a viewer would most want clipped, in order of priority - the
people, teams, claims, arguments and funny bits the title promises.

For each moment give the WORDS SPEAKERS WILL ACTUALLY SAY when it comes up:
names as they are spoken (surnames, nicknames, common mishearings), and
distinctive words. The list is matched against the transcript word for word,
so generic words (game, good, team, season, really) are useless - never use
them. 2 to 8 moments, 2 to 6 words or short phrases each, lower case.

Reply with JSON only:
{"topics": [{"name": "short label", "terms": ["word", "two words"]}]}

Example, for "Trae Young on the Knicks chants and Pat Beverley":
{"topics": [{"name": "Knicks", "terms": ["knicks", "chant", "msg"]},
            {"name": "Pat Beverley", "terms": ["beverley", "beverly", "pat bev"]}]}

Source: SOURCE
Title: TITLE"""


def hooks_text(topics):
    """[{name, terms}] -> 'Name: t1, t2; Name2: t3' - what score.parse_hunt reads."""
    return "; ".join(f"{t['name']}: {', '.join(t['terms'])}" for t in topics)


def check_hooks(doc):
    """[{name, terms}] from a model's JSON, cleaned, or ValueError. Code
    decides what is usable: generic words are dropped, a topic left with no
    terms goes, and too few topics is a failure."""
    out = []
    for t in (doc or {}).get("topics") or []:
        if not isinstance(t, dict):
            continue
        name = re.sub(r"[;:,]", " ", str(t.get("name") or "")).strip()[:40]
        terms = []
        for term in t.get("terms") or []:
            term = re.sub(r"\s+", " ", str(term).lower().replace(";", " ").replace(",", " ")).strip()
            if TERM_RE.match(term) and term not in GENERIC and term not in terms:
                terms.append(term)
        if name and terms:
            out.append({"name": name, "terms": terms[:TERMS_PER_TOPIC]})
    out = out[:HOOK_TOPICS[1]]
    if len(out) < HOOK_TOPICS[0]:
        raise ValueError(f"the draft had {len(out)} usable topic(s) - write the hooks by hand")
    return out


def hooks(conn, cfg, title, source="", call=None, key=None, left=None):
    """(hunt text, cost) for a video title. Checked against both caps before
    the call - the shared day (budget.py) and Clip's own - and its real cost
    recorded in Clip's ledger, the way post copy is."""
    title = (title or "").strip()[:MAX_TITLE]
    if not title:
        raise ValueError("paste the video's title first")
    slug = cfg.get("copy_model")
    if not slug:
        raise ValueError("drafting by model is switched off (copy_model is null) - write the hooks by hand")
    if left is None:
        left = postcopy.allowance_left()
    if left is None or left < postcopy.MIN_LEFT:
        raise ValueError("today's shared AI allowance is spent - write the hooks by hand, or try tomorrow")
    db.spend_guard(conn, cfg, HOOKS_ESTIMATE)
    key = key if key is not None else postcopy._script("preflight_hooks", "preflight.py").openrouter_key()
    if not key:
        raise ValueError("no OpenRouter key")
    prompt = HOOKS_PROMPT.replace("SOURCE", source or "").replace("TITLE", title)
    doc, cost = (call or call_hooks)(prompt, key, slug)
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
