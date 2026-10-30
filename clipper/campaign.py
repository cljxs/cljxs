"""A campaign's own clip list: which moments to cut, with its hook and caption.

Curious Mike's guidelines (a public Notion page) list 50 moments per episode:
a timestamp on the published YouTube episode, a hook for the screen, a caption
for the post, and for Chandler Hutchison's episode a note on how to cut each
one ("MUST keep Chandler's correction at 65:25-65:37"). The campaign also says
where to start ("Start with these 6"). When a campaign has done that work, its
list is better evidence than any score Clip can compute - it is the client
saying what performs and what is safe to say - so Clip cuts those moments and
posts those words, verbatim.

The page is read through Notion's public page API (the same calls the page
makes in a browser), then parsed by column header, not by position:

  a clip table      has a time column (Time / Timestamp), a Hook column and a
                    Caption column; its key is the ID column (C45) or # (01)
  a priority table  has a time column and a key but no hook - its row order
                    is the campaign's "start here" order
  a direction table has a "How to cut" column

Everything under one sub-heading is one episode; the episode is the YouTube
id its timestamp links point at. The YouTube timeline is the file's timeline:
checked 2026-09-29 on Trae Young, where the campaign's C45 (67:12-68:21) and
the Dropbox file's own Knicks conversation (67:20-68:10) line up.
"""

import json
import re
import urllib.parse
import urllib.request
from collections import Counter

from clipper import db

UA = "Mozilla/5.0 (compatible; clipper/1; +https://github.com/cljxs/cljxs)"
TIME = re.compile(r"(\d{1,2}(?::\d{2}){1,2})\s*[-–]\s*(\d{1,2}(?::\d{2}){1,2})")
YT = re.compile(r"(?:youtu\.be/|youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})")


# ------------------------------------------------------------------ fetching

def _post(base, endpoint, body):
    # Notion answers Python's default user agent with 403 (seen 2026-09-30).
    req = urllib.request.Request(f"{base}/api/v3/{endpoint}", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "user-agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _value(rec):
    v = rec.get("value") or {}
    return v.get("value", v)            # newer responses wrap the block once more


def page_id(url):
    """The page's id from any form of its link (with or without dashes)."""
    hexes = re.findall(r"[0-9a-f]{32}", urllib.parse.urlsplit(url).path.replace("-", ""))
    if not hexes:
        raise ValueError(f"{url} does not look like a Notion page link")
    h = hexes[-1]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def fetch(url):
    """(page id, {block id: block}) for a public Notion page, every block."""
    if not url.startswith("https://"):
        raise ValueError("only an https:// Notion link")
    parts = urllib.parse.urlsplit(url)
    base, pid = f"https://{parts.netloc}", page_id(url)
    blocks, cursor, chunk = {}, {"stack": []}, 0
    while True:
        d = _post(base, "loadPageChunk", {"pageId": pid, "limit": 100, "cursor": cursor,
                                          "chunkNumber": chunk, "verticalColumns": False})
        for k, rec in d.get("recordMap", {}).get("block", {}).items():
            blocks[k] = _value(rec)
        cursor, chunk = d.get("cursor") or {}, chunk + 1
        if not cursor.get("stack") or chunk > 50:
            break
    if pid not in blocks:
        raise ValueError("Notion did not return that page - is it shared publicly?")
    space = blocks[pid].get("space_id")
    for _ in range(30):                 # big pages come back partly; fetch the rest by id
        need = sorted({c for b in blocks.values() for c in (b.get("content") or []) if c not in blocks})
        if not need:
            break
        for k in range(0, len(need), 50):
            d = _post(base, "syncRecordValuesMain", {"requests": [
                {"pointer": {"table": "block", "id": i, "spaceId": space}, "version": -1}
                for i in need[k:k + 50]]})
            for i, rec in d.get("recordMap", {}).get("block", {}).items():
                blocks[i] = _value(rec)
    return pid, blocks


# ------------------------------------------------------------------ parsing

def text(prop):
    return "".join(seg[0] for seg in (prop or [])).strip()


def links(prop):
    return [m[1] for seg in (prop or []) for m in (seg[1] if len(seg) > 1 else []) if m[0] == "a"]


def seconds(hms):
    out = 0
    for part in hms.split(":"):
        out = out * 60 + int(part)
    return float(out)


def norm_key(s):
    """'#02', '02', '2' -> '2'; 'c45' -> 'C45'."""
    s = s.strip().lstrip("#").strip()
    return str(int(s)) if s.isdigit() else s.upper()


def _column(heads, *patterns):
    for k, h in enumerate(heads):
        if any(re.search(p, h, re.I) for p in patterns):
            return k
    return None


def parse(blocks, pid):
    """{episode: {key: entry}} from the page, and each episode's priority order.
    An entry: key, start, end, title, topic, hook, caption, direction."""
    sections, order = [], []                          # (section heading, table block)
    def walk(i, section):
        b = blocks.get(i) or {}
        if b.get("type") == "sub_header":
            section = text((b.get("properties") or {}).get("title"))
        if b.get("type") == "table":
            order.append((section, b))
        for c in b.get("content") or []:
            walk(c, section)
    walk(pid, "")
    found = {}                                        # section -> {"clips", "priority", "direction", "yt"}
    for section, table in order:
        cols = (table.get("format") or {}).get("table_block_column_order") or []
        rows = [((blocks.get(r) or {}).get("properties") or {}) for r in table.get("content") or []]
        if len(rows) < 2 or not cols:
            continue
        heads = [text(rows[0].get(c)) for c in cols]
        body = [[r.get(c) for c in cols] for r in rows[1:]]
        t = _column(heads, r"^time", r"^timestamp")
        key = _column(heads, r"^id$") if _column(heads, r"^id$") is not None else _column(heads, r"^#$")
        hook, cap = _column(heads, r"hook"), _column(heads, r"caption")
        how = _column(heads, r"how to cut")
        sec = found.setdefault(section, {"clips": {}, "priority": [], "direction": {}, "yt": Counter()})
        for row in body:
            for cell in row:
                for u in links(cell):
                    m = YT.search(u)
                    if m:
                        sec["yt"][m.group(1)] += 1
        if key is None:
            continue
        if how is not None:
            for row in body:
                if text(row[key]):
                    sec["direction"][norm_key(text(row[key]))] = text(row[how])
        elif t is not None and hook is not None and cap is not None:
            topic = _column(heads, r"^topic")
            for row in body:
                k, m = norm_key(text(row[key])), TIME.search(text(row[t]))
                if not (k and m):
                    continue
                sec["clips"][k] = {"key": k, "start": seconds(m.group(1)), "end": seconds(m.group(2)),
                                   "hook": text(row[hook]), "caption": text(row[cap]),
                                   "topic": text(row[topic]) if topic is not None else ""}
        elif t is not None:
            title = _column(heads, r"moment", r"angle")
            for row in body:
                k = norm_key(text(row[key]))
                if k:
                    sec["priority"].append((k, text(row[title]) if title is not None else ""))
    episodes = {}
    for section, sec in found.items():
        if not sec["clips"] or not sec["yt"]:
            continue
        ep = sec["yt"].most_common(1)[0][0]
        titles = dict(sec["priority"])
        first = [k for k, _ in sec["priority"] if k in sec["clips"]]
        rest = sorted((k for k in sec["clips"] if k not in first),
                      key=lambda k: (int(re.sub(r"\D", "", k) or 0), k))
        entries = []
        for rank, k in enumerate(first + rest):
            e = dict(sec["clips"][k], priority=rank, first_batch=k in first,
                     title=titles.get(k, ""), direction=sec["direction"].get(k, ""))
            entries.append(e)
        episodes[ep] = {"section": section, "entries": entries}
    return episodes


# ------------------------------------------------------------------ storing

def import_list(conn, source_name, url, fetched=None):
    """Read the page and store its lists under the source. Returns
    {episode: (section, clips, first batch)}. Re-importing updates in place."""
    src = conn.execute("SELECT * FROM sources WHERE name = ?", (source_name,)).fetchone()
    if not src:
        raise ValueError(f"no source named {source_name!r}")
    pid, blocks = fetched or fetch(url)
    episodes = parse(blocks, pid)
    if not episodes:
        raise ValueError("no clip list found on that page - it needs a table with a time, a hook "
                         "and a caption column, linked to the YouTube episode")
    now, out = db.now(), {}
    with conn:
        for ep, info in episodes.items():
            for e in info["entries"]:
                conn.execute(
                    "INSERT INTO cutlist (source_id, episode, key, priority, first_batch, start, end, "
                    "title, topic, hook, caption, direction, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(source_id, episode, key) DO UPDATE SET priority=excluded.priority, "
                    "first_batch=excluded.first_batch, start=excluded.start, end=excluded.end, "
                    "title=excluded.title, topic=excluded.topic, hook=excluded.hook, "
                    "caption=excluded.caption, direction=excluded.direction, "
                    "imported_at=excluded.imported_at",
                    (src["id"], ep, e["key"], e["priority"], int(e["first_batch"]), e["start"], e["end"],
                     e["title"], e["topic"], e["hook"], e["caption"], e["direction"], now))
            out[ep] = (info["section"], len(info["entries"]),
                       sum(1 for e in info["entries"] if e["first_batch"]))
    return out


def entries(conn, video):
    """The campaign's list for this video's episode, in the campaign's order."""
    if not video["episode"]:
        return []
    return conn.execute("SELECT * FROM cutlist WHERE source_id = ? AND episode = ? ORDER BY priority",
                        (video["source_id"], video["episode"])).fetchall()


def snap(words, sents, start, end):
    """Sentence indices (i, j) for a campaign's rough timestamps: the sentence
    boundary nearest each one within a few seconds, so a cut never lands in a
    word, and a listed moment is never trimmed short of what it names."""
    starts = [(abs(s["start"] - start), k) for k, s in enumerate(sents) if start - 4 <= s["start"] <= start + 2]
    ends = [(abs(s["end"] - end), k) for k, s in enumerate(sents) if end - 2 <= s["end"] <= end + 4]
    i = min(starts)[1] if starts else max((k for k, s in enumerate(sents) if s["start"] <= start),
                                          default=0)
    j = min(ends)[1] if ends else min((k for k, s in enumerate(sents) if s["end"] >= end),
                                      default=len(sents) - 1)
    if j < i:
        raise ValueError(f"no speech between {start:.0f}s and {end:.0f}s")
    return i, j


def queue(conn, paths, cfg, video, n=None, first_batch=False):
    """Make candidates for the next moments on the campaign's list that this
    video has not had yet - its own first batch, or the next n - and send
    the video back to be rendered. Returns the entries queued."""
    from clipper import moments, pipeline
    rows = entries(conn, video)
    done = {r[0] for r in conn.execute("SELECT cut_id FROM candidates WHERE video_id = ? AND cut_id IS NOT NULL",
                                       (video["id"],))}
    todo = [r for r in rows if r["id"] not in done and (r["first_batch"] or not first_batch)]
    todo = todo if n is None else todo[:n]
    if not todo:
        return []
    words = pipeline.words_of(conn, paths, video)
    sents = moments.sentences(words, cfg["sentence_pause_seconds"])
    with conn:
        for r in todo:
            i, j = snap(words, sents, r["start"], r["end"])
            start, end = moments.cut_points(words, sents[i]["first"], sents[j]["last"])
            why = [f"the campaign's list: #{r['key']} {r['title'] or r['topic']}".rstrip()]
            if r["direction"]:
                why.append(f"campaign note: {r['direction']}")
            conn.execute(
                "INSERT INTO candidates (video_id, start, end, text, score, scorer, features, reasons, "
                "selected, cut_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (video["id"], start, end, " ".join(s["text"] for s in sents[i:j + 1]), 100.0,
                 "campaign-list", db.as_json({}), db.as_json(why), r["id"]))
        conn.execute("UPDATE videos SET stage = 'found', error = NULL, attempts = 0, updated_at = ? "
                     "WHERE id = ? AND stage IN ('found', 'done', 'failed')", (db.now(), video["id"]))
    db.event(conn, "cuts", f"queued {len(todo)} moment(s) from the campaign's list: "
                           + ", ".join("#" + r["key"] for r in todo), video_id=video["id"])
    return todo
