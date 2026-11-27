"""The words that go out with a clip: a title, a caption, hashtags.

A model writes a draft from the clip's own transcript; code decides whether
it may be used and builds what each platform receives. The model is never
trusted on anything code can check:

* the title may not contain a number the clip never says ("3 reasons..."
  over a clip that gives one is how a truthful clip becomes a misleading
  post);
* lengths are cut to each platform's limit here, not asked for politely;
* hashtags are reduced to letters, digits and underscores, at most five
  from the model;
* the caption asks the viewer a question - added if the draft has none;
* the source's required tags and credit line (a campaign's rules, a CC
  licence's attribution) are added by code to every post, whatever the
  model wrote.

No model available - no key, today's AI allowance spent, a failed call - is
not an error: the clip gets a plain draft from its first sentence and the
owner can edit it before approving.

Spend: the shared $1/day cap is checked through budget.py (the same check
the GM makes), Clip's own cap through db.spend_guard, and the real cost of
each call is recorded in Clip's ledger.
"""

import importlib.util
import json
import re
import urllib.request

from clipper import config, db, transcribe

TITLE_MAX = 100          # YouTube's limit
CAPTION_MAX = 300        # ours: the hook, not an essay
TIKTOK_MAX = 2200        # TikTok's caption limit
YT_DESC_MAX = 5000
YT_TAGS_MAX = 450        # YouTube allows 500 characters across all tags
MODEL_TAGS = 5
MIN_LEFT = 0.05          # dollars of today's shared allowance a call needs left
ESTIMATE = 0.004         # the most one draft is expected to cost
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
QUESTION = "Agree or disagree?"   # added to a caption that asks nothing


def _script(name, file):
    spec = importlib.util.spec_from_file_location(name, config.ROOT / "scripts" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ rules

def clean_tag(t):
    t = re.sub(r"[^A-Za-z0-9_]", "", str(t or "").lstrip("#"))
    return t if 2 <= len(t) <= 30 else None


def tags_of(text):
    """'#clipping #Creator, stream' -> ['clipping', 'Creator', 'stream']"""
    out = []
    for t in re.split(r"[\s,]+", text or ""):
        c = clean_tag(t)
        if c and c.lower() not in {x.lower() for x in out}:
            out.append(c)
    return out


def cut(text, limit):
    """Shorten at a word boundary, never mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def invented_numbers(title, transcript):
    """Numbers in the title that the clip never says."""
    said = {n.replace(",", "") for n in NUMBER.findall(transcript)}
    return [n for n in NUMBER.findall(title) if n.replace(",", "") not in said]


def ask(caption):
    """A caption that asks the viewer something. Comments are what campaigns
    are paid on (Curious Mike wants 1% likes+comments per view) and a
    question is what gets them - so a draft without one gets one here,
    whether a model forgot or no model was called."""
    caption = " ".join(str(caption or "").split())
    if not caption or "?" in caption:
        return caption
    return cut(caption, CAPTION_MAX - len(QUESTION) - 1) + " " + QUESTION


def check_draft(doc, transcript):
    """(title, caption, [tags]) from a model's JSON, or raise ValueError."""
    title = cut(doc.get("title"), TITLE_MAX)
    caption = ask(cut(doc.get("caption"), CAPTION_MAX))
    if not title or not caption:
        raise ValueError("the draft has no title or no caption")
    bad = invented_numbers(title, transcript)
    if bad:
        raise ValueError(f"the title says {', '.join(bad)}, which the clip never does")
    tags = []
    for t in doc.get("hashtags") or []:
        c = clean_tag(t)
        if c and c.lower() not in {x.lower() for x in tags}:
            tags.append(c)
    return title, caption, tags[:MODEL_TAGS]


def template(transcript):
    """The no-model draft: the clip's own first sentence."""
    first = re.split(r"(?<=[.!?])\s", transcript.strip(), maxsplit=1)[0]
    return cut(first, 90), ask(cut(first, CAPTION_MAX)), []


# ------------------------------------------------------------------ the model

PROMPT = """You write the title and caption for a short vertical video clip.
The clip's transcript is below. Everything you write must be true of THIS
clip: no claims, numbers or names it does not contain, no "you won't
believe", nothing that changes what the speaker meant. Make people want to
watch by pointing at what is genuinely interesting in it. End the caption
with a short question that asks viewers for their own opinion on what is
said (do they agree, who is right, where would they rank it) - comments
are what the clip is judged on.

Reply with JSON only:
{"title": "under 70 characters", "caption": "one or two sentences ending in a question, under 200 characters",
 "hashtags": ["3 to 5 relevant words, no # sign"]}

Source: SOURCE
Transcript: TEXT"""


def call_model(transcript, source, key, slug, timeout=60):
    ea = _script("emily_assets_copy", "emily-assets.py")
    body = {"model": slug, "max_tokens": 1500, "reasoning": {"effort": "low"},
            "response_format": {"type": "json_object"}, "usage": {"include": True},
            "messages": [{"role": "user", "content": PROMPT.replace("SOURCE", source)
                          .replace("TEXT", transcript[:4000])}]}
    req = urllib.request.Request(ea.OR_URL, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cljxs/cljxs", "X-Title": "clip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"OpenRouter: {str(data['error'])[:200]}")
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("the reply holds no JSON object")
    return json.loads(text[s:e + 1]), ea.cost_of(data.get("usage") or {})


def allowance_left():
    """Today's shared AI allowance, from budget.py - None if unreadable."""
    b = _script("budget_copy", "budget.py").read()
    return b.get("ai_left_today") if b.get("state") == "ok" else 0.0


def write(conn, cfg, clip_id, redo=False, call=None, key=None, left=None):
    """Draft the copy for one clip and store it. Returns the stored row.
    An owner's edit is never overwritten unless redo is asked for."""
    have = conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (clip_id,)).fetchone()
    if have and not redo:
        return have
    clip = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    meta = json.loads(clip["meta"])
    # With the source's spellings as they are NOW: the sentence was stored when
    # the clip was cut, and a name fixed since ("Kamehna" -> Kuminga,
    # 2026-10-09) must reach a New draft instead of being redrafted wrong.
    src = conn.execute("SELECT s.spellings FROM sources s JOIN videos v ON v.source_id = s.id "
                       "WHERE v.id = ?", (clip["video_id"],)).fetchone()
    transcript = transcribe.respell_text(meta.get("text") or "",
                                         json.loads(src["spellings"]) if src and src["spellings"] else [])
    listed = conn.execute("SELECT l.* FROM candidates c JOIN cutlist l ON l.id = c.cut_id WHERE c.id = ?",
                          (clip["candidate_id"],)).fetchone()
    if listed and listed["caption"]:
        # The campaign wrote this post itself: its caption, word for word, and
        # its hook as the title. No model and no added question - "Agree or
        # disagree?" under a man describing his recovery is exactly the tone
        # the campaign asks clippers not to take.
        title = cut(listed["hook"] or listed["title"] or listed["caption"], TITLE_MAX)
        caption = cut(listed["caption"], CAPTION_MAX)
        with conn:
            conn.execute("INSERT INTO post_copy (clip_id, title, caption, hashtags, generator, cost_usd, "
                         "updated_at) VALUES (?, ?, ?, '[]', 'campaign', 0, ?) ON CONFLICT(clip_id) DO UPDATE "
                         "SET title=excluded.title, caption=excluded.caption, hashtags=excluded.hashtags, "
                         "generator=excluded.generator, cost_usd=0, updated_at=excluded.updated_at",
                         (clip_id, title, caption, db.now()))
        return conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (clip_id,)).fetchone()
    title, caption, tags, gen, cost = (*template(transcript), "template", 0.0)
    why = None
    try:
        if not cfg.get("copy_model"):
            raise RuntimeError("drafting by model is switched off (copy_model is null)")
        if left is None:
            left = allowance_left()
        if left is None or left < MIN_LEFT:
            raise RuntimeError("today's shared AI allowance is spent")
        db.spend_guard(conn, cfg, ESTIMATE)
        key = key if key is not None else _script("preflight_copy", "preflight.py").openrouter_key()
        if not key:
            raise RuntimeError("no OpenRouter key")
        doc, cost = (call or call_model)(transcript, meta.get("source") or "", key, cfg["copy_model"])
        db.record_cost(conn, "openrouter", "copy", float(cost or 0), note=f"clip {clip_id}")
        title, caption, tags = check_draft(doc, transcript)
        gen = "llm:" + cfg["copy_model"]
    except Exception as exc:          # the draft falls back; posting never depends on a model
        why = str(exc)[:200]
        title, caption, tags = template(transcript)
        gen = "template"
    with conn:
        conn.execute("INSERT INTO post_copy (clip_id, title, caption, hashtags, generator, cost_usd, "
                     "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(clip_id) DO UPDATE SET "
                     "title=excluded.title, caption=excluded.caption, hashtags=excluded.hashtags, "
                     "generator=excluded.generator, cost_usd=excluded.cost_usd, "
                     "updated_at=excluded.updated_at",
                     (clip_id, title, caption, json.dumps(tags), gen, float(cost or 0), db.now()))
    if why:
        db.event(conn, "copy", f"plain draft for clip {clip_id}: {why}", level="warn", clip_id=clip_id)
    return conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (clip_id,)).fetchone()


def edit(conn, clip_id, title=None, caption=None, hashtags=None):
    """The owner's words. Checked for length and tag shape only - the
    invented-number rule is for a model, not for the person approving."""
    have = conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (clip_id,)).fetchone()
    if not have:
        raise ValueError(f"clip {clip_id} has no draft yet")
    t = cut(title, TITLE_MAX) if title is not None else have["title"]
    c = cut(caption, CAPTION_MAX) if caption is not None else have["caption"]
    h = json.dumps(tags_of(hashtags)[:10]) if hashtags is not None else have["hashtags"]
    if not t or not c:
        raise ValueError("a post needs a title and a caption")
    with conn:
        conn.execute("UPDATE post_copy SET title=?, caption=?, hashtags=?, generator='owner', "
                     "updated_at=? WHERE clip_id=?", (t, c, h, db.now(), clip_id))
    return conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (clip_id,)).fetchone()


# ------------------------------------------------------------------ per platform

def compose(copy_row, source):
    """What each platform receives. The source's required tags come first and
    its credit/attribution line is always present - added here, by code."""
    required = tags_of(source["post_tags"])
    tags = required + [t for t in json.loads(copy_row["hashtags"])
                       if t.lower() not in {r.lower() for r in required}]
    credit = [x for x in (source["credit"], source["attribution"]) if x]
    hashline = " ".join("#" + t for t in tags)
    yt_tags, total = [], 0
    for t in tags:
        if total + len(t) + 1 > YT_TAGS_MAX:
            break
        yt_tags.append(t)
        total += len(t) + 1
    youtube = {
        "title": copy_row["title"][:TITLE_MAX],
        "description": "\n\n".join(x for x in [copy_row["caption"], "\n".join(credit),
                                               (hashline + " #Shorts").strip()] if x)[:YT_DESC_MAX],
        "tags": yt_tags,
    }
    tiktok = " ".join(x for x in [copy_row["caption"], " ".join(credit), hashline] if x)[:TIKTOK_MAX]
    return {"youtube": youtube, "tiktok": tiktok, "tags": tags}
