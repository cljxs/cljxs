#!/usr/bin/env python3
"""
stan-fetch.py — Stan's fetcher: Hacker News posts that say a software pain
out loud. Plain code, no AI, costs nothing.

    stan-fetch.py              # new posts since the last fetch, into the backlog
    stan-fetch.py --days 30    # look further back (a first run looks 14 days)

Searches Hacker News (Algolia's public HN search API - no account, no key) for
each phrase in agents/stan/watchlist.json, as an exact phrase, in stories and
comments. A post is kept only when the phrase really is in its text: the
search also matches near-misses, and a near-miss is noise Stan would pay to
read.

WRITES, all inside agents/stan/:
    data/backlog.json        the posts not yet judged - this script owns it
    data/fetch-status.json   per phrase: ok, how many found, how many new, any error
    state/seen-posts.json    every post id ever fetched ("hn:<id>"), so a post is
                             fetched once; ids only, pruned after 120 days

RETENTION. The backlog keeps a short excerpt round the phrase, not the whole
post, and drops anything older than keep_backlog_days. What a surfaced
problem cites (a link and the quote) is kept with the problem by stan.py.

Posts are untrusted text from strangers. Nothing here follows a link in one,
runs anything from one, or posts anywhere: it reads the search API and
writes files.

Standard library only.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://news.ycombinator.com/item?id={}"
SOURCE = "hn"

PER_PAGE = 100
MAX_PAGES = 3              # 300 posts a phrase a fetch - far above the ~40 a month seen
OVERLAP = timedelta(days=1)  # re-read the last day: a post can reach the index late
SEEN_KEEP_DAYS = 120
EXCERPT_BEFORE, EXCERPT_AFTER = 500, 900
PAUSE = 1.0                # seconds between requests - one fetch a day, so be polite


def agent_dir():
    return ROOT / "agents" / "stan"


def now_utc():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    """Whole or not at all: a reader never meets half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)


def watchlist(path=None):
    """The phrases and Stan's settings, checked. A broken watchlist is said
    by name here, not discovered as a KeyError halfway through a fetch."""
    d = json.loads(Path(path or agent_dir() / "watchlist.json").read_text())
    phrases = []
    for p in d.get("phrases") or []:
        text = str((p or {}).get("phrase") or "").strip()
        w = (p or {}).get("weight")
        if not text or not isinstance(w, (int, float)) or isinstance(w, bool) or not 0 < w <= 1:
            raise ValueError(f"watchlist.json: each phrase needs text and a weight above 0, at most 1 ({p!r})")
        phrases.append({"phrase": text, "weight": float(w)})
    if not 1 <= len(phrases) <= 20:
        raise ValueError("watchlist.json: 1 to 20 phrases")
    d["phrases"] = phrases
    return d


def clean(raw):
    """HN's HTML as plain text: paragraphs as line breaks, tags gone,
    entities decoded, curly apostrophes made straight so phrases match."""
    t = re.sub(r"<p>|<br\s*/?>", "\n", raw or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = t.replace("’", "'").replace("‘", "'")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def excerpt(text, phrase):
    """The part of the post round the phrase - the evidence, not the post."""
    i = text.lower().find(phrase.lower())
    if i < 0:
        return None
    a, b = max(0, i - EXCERPT_BEFORE), min(len(text), i + len(phrase) + EXCERPT_AFTER)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def to_post(hit, phrase, weight, fetched):
    """A search hit as a backlog post, or None when the phrase is not really
    in it (the search also returns near-misses)."""
    tags = hit.get("_tags") or []
    kind = "story" if "story" in tags else "comment" if "comment" in tags else None
    oid = str(hit.get("objectID") or "")
    if not kind or not oid.isdigit():
        return None
    title = clean(hit.get("title") or hit.get("story_title") or "")
    body = clean(hit.get("comment_text") or hit.get("story_text") or "")
    text = (title + "\n" + body).strip() if kind == "story" else body
    ex = excerpt(text, phrase)
    if ex is None:
        return None
    created = hit.get("created_at_i")
    created = stamp(datetime.fromtimestamp(int(created), timezone.utc)) if str(created).isdigit() else None
    return {"id": f"{SOURCE}:{oid}", "kind": kind, "author": str(hit.get("author") or ""),
            "created_utc": created, "fetched_utc": fetched,
            "thread": title, "link": ITEM.format(oid),
            "points": hit.get("points") if isinstance(hit.get("points"), int) else None,
            "phrases": [phrase], "weight": weight, "text": ex}


def search(phrase, since, get=None, pause=time.sleep):
    """Every hit for one exact phrase since `since`, newest first."""
    get = get or (lambda url: json.loads(urllib.request.urlopen(url, timeout=30).read()))
    hits = []
    for page in range(MAX_PAGES):
        q = urllib.parse.urlencode({"query": f'"{phrase}"', "tags": "(story,comment)",
                                    "numericFilters": f"created_at_i>{int(since.timestamp())}",
                                    "hitsPerPage": PER_PAGE, "page": page})
        d = get(f"{SEARCH}?{q}")
        hits += d.get("hits") or []
        if page + 1 >= int(d.get("nbPages") or 0):
            break
        pause(PAUSE)
    return hits


def fetch(days=None, get=None, pause=time.sleep, now=None):
    """One fetch. Returns the status written to data/fetch-status.json."""
    now = now or now_utc()
    a = agent_dir()
    wl = watchlist()
    status_path = a / "data" / "fetch-status.json"
    last = parse_stamp(read_json(status_path, {}).get("last_ok_utc"))
    if days:
        since = now - timedelta(days=days)
    elif last:
        since = last - OVERLAP
    else:
        since = now - timedelta(days=int(wl.get("fetch_days_first_run", 14)))

    seen = read_json(a / "state" / "seen-posts.json", {})
    backlog = read_json(a / "data" / "backlog.json", {}).get("posts") or []
    by_id = {p["id"]: p for p in backlog}
    fetched = stamp(now)
    rows, new_ids = [], []
    for i, p in enumerate(wl["phrases"]):
        if i:
            pause(PAUSE)
        row = {"phrase": p["phrase"], "ok": False, "found": 0, "new": 0, "error": None}
        try:
            hits = search(p["phrase"], since, get=get, pause=pause)
        except Exception as e:
            row["error"] = str(e)[:200]
            rows.append(row)
            continue
        row["ok"] = True
        for h in hits:
            post = to_post(h, p["phrase"], p["weight"], fetched)
            if post is None:
                continue
            row["found"] += 1
            if post["id"] in by_id:            # in this fetch already, by another phrase
                old = by_id[post["id"]]
                if p["phrase"] not in old["phrases"]:
                    old["phrases"].append(p["phrase"])
                    old["weight"] = max(old["weight"], p["weight"])
                continue
            if post["id"] in seen:
                continue
            seen[post["id"]] = fetched
            by_id[post["id"]] = post
            new_ids.append(post["id"])
            row["new"] += 1
        rows.append(row)

    keep_after = now - timedelta(days=int(wl.get("keep_backlog_days", 21)))
    posts = [p for p in by_id.values() if (parse_stamp(p.get("fetched_utc")) or now) >= keep_after]
    posts.sort(key=lambda p: p.get("created_utc") or "", reverse=True)
    cut = now - timedelta(days=SEEN_KEEP_DAYS)
    seen = {k: v for k, v in seen.items() if (parse_stamp(v) or now) >= cut}

    ok = any(r["ok"] for r in rows)
    write_json(a / "data" / "backlog.json", {"source": "Hacker News (hn.algolia.com)", "updated_utc": fetched,
                                              "posts": posts})
    write_json(a / "state" / "seen-posts.json", seen)
    status = {"utc": fetched, "since_utc": stamp(since), "ok": ok,
              "last_ok_utc": fetched if ok else (stamp(last) if last else None),
              "new": len(new_ids), "backlog": len(posts), "phrases": rows,
              "reddit": "off - not used until its terms for this use are confirmed (owner, 2026-10-14)"}
    write_json(status_path, status)
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, help="look back this many days instead of since the last fetch")
    a = ap.parse_args(argv)
    try:
        st = fetch(days=a.days)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    for r in st["phrases"]:
        said = f"error: {r['error']}" if r["error"] else f"{r['found']} found, {r['new']} new"
        print(f"  {r['phrase']:<22} {said}")
    print(f"{st['new']} new post(s); {st['backlog']} in the backlog; since {st['since_utc']}")
    # Every phrase failing is a failed fetch - the timer's unit goes red,
    # health-check.py says so. Some failing is reported above and is not.
    return 0 if st["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
