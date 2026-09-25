#!/usr/bin/env python3
"""
gm.py — the GM's morning: the day's facts in, a few proposals out.

    python3 scripts/gm.py run                  # the morning run (the timer does this)
    python3 scripts/gm.py run --dry-run        # print the facts and prompt, call nothing
    python3 scripts/gm.py show                 # the latest morning, in plain words
    python3 scripts/gm.py show --json          # the same, for the Deck
    python3 scripts/gm.py decide 2026-09-26 1 approve --note "yes, today"
    python3 scripts/gm.py decide 2026-09-26 2 decline --note "not while totes-only"

PROPOSE-ONLY. The owner decided (2026-09-25) that for the first month the GM
writes proposals and nothing else. It creates no task, changes no agent's
state and wakes no agent. A proposal is approved or declined in the Town Hall
and the decision is kept, with its note, for the GM to read the next morning -
that is how it learns what the owner wants.

ONE MODEL CALL A DAY, AND ONLY WHEN THERE IS ROOM FOR IT. The GM runs after
Fury and asks budget.py first: if today's AI allowance is spent, unreadable,
or down to less than MIN_LEFT, it writes down why it skipped and stays asleep.
It never runs twice in a day unless told to with --force.

CODE COMPILES THE FACTS; THE MODEL ONLY WEIGHS THEM. Every fact is a numbered
line - Fury's briefing, the budget, the knowledge store, yesterday's decisions
- and every proposal must cite the facts it rests on. Code then checks the
reply, because a model asked to reason about numbers will eventually claim a
number nobody gave it:

  * a proposal citing no fact, or a fact that does not exist, is dropped
  * a number in a proposal's `why` must appear in the facts it cites
  * departments and people must be real ones
  * at most MAX_PROPOSALS proposals and MAX_KNOWLEDGE knowledge suggestions

Dropped proposals are kept with the reason, so the Town Hall shows what the
model tried and why it was refused rather than hiding it. Knowledge the GM
suggests goes through knowledge.py like anyone's: proposed, never accepted.

Each morning is one file, tasks/gm/<Eastern date>.json, ignored by git.

Standard library only.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent
DAYS = Path(os.environ.get("GM_DIR", ROOT / "tasks" / "gm"))
FURY_REPORTS = ROOT / "agents" / "fury" / "reports"

sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


budget = _load("budget", "budget.py")          # the daily cap and today's spend
knowledge = _load("knowledge", "knowledge.py") # departments, the store, briefs
ea = budget.preflight.ea                       # the OpenRouter URL and cost_of()

# openai/gpt-5-mini is a slug captured from OpenRouter's own model list
# (tests/fixtures/openrouter-models.json), not typed from memory. At its
# captured rates a morning of a few thousand tokens is about a cent.
DEFAULT_MODEL = "openai/gpt-5-mini"
# gpt-5-mini reasons before it answers, and the reasoning counts against
# max_tokens: a cap that is all thinking returns an empty answer. Low effort
# plus this room keeps the worst case near a cent and a half.
MAX_TOKENS = 6000
REASONING = {"effort": "low"}

# Dollars of today's AI allowance the GM needs before it will wake. A morning
# costs about a cent; this leaves room for a long reply without being the call
# that takes the agents' day to zero.
MIN_LEFT = 0.10

MAX_PROPOSALS = 5
MAX_KNOWLEDGE = 3
TITLE_MAX = 90
TEXT_MAX = 400
SUMMARY_MAX = 700

# Fury writes its briefing at 08:30 Central. Older than this and it is
# yesterday's, which the GM must not present as this morning's.
FURY_FRESH_HOURS = 12

VERDICTS = ("approve", "decline")
NOTE_MAX = 300


def model():
    return os.environ.get("GM_MODEL", "").strip() or DEFAULT_MODEL


def utc_now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- facts

def _plain(text):
    return re.sub(r"\*\*|__|`", "", text).strip()


def fury_facts(reports=FURY_REPORTS, now=None):
    """Fury's newest briefing as fact lines, each with its section.

    Fury already turned every agent's state into sentences, in code. Reading
    those sentences keeps one account of the system rather than a second
    collector that would drift from the first.
    """
    now = now or utc_now()
    try:
        newest = max(Path(reports).glob("*.md"), key=lambda f: f.stat().st_mtime)
    except ValueError:
        return ["Fury's briefing: none has been written."]
    age_h = (now.timestamp() - newest.stat().st_mtime) / 3600
    if age_h > FURY_FRESH_HOURS:
        return [f"Fury's briefing: the newest one ({newest.name}) is {age_h:.0f} hours "
                f"old - there is none for this morning."]
    out, section = [], "Briefing"
    for line in newest.read_text(errors="replace").splitlines():
        if line.startswith("## "):
            section = _plain(line[3:]).strip(" ⚠️") or section
            continue
        s = line.strip()
        if s.startswith("- ") and s[2:].strip().lower() not in ("nothing.",):
            out.append(f"{section}: {_plain(s[2:])}")
    return out or [f"Fury's briefing ({newest.name}) lists nothing."]


def budget_facts(b):
    """The budget as sentences, from budget.py's own assessment."""
    if not b or b.get("state") in (None, "unreadable", "unknown"):
        return ["Budget: today's AI spend could not be read."]
    out = []
    if b.get("cap") is not None:
        out.append(f"Budget: ${b['ai_today']:.2f} of AI spent today of the "
                   f"${b['cap']:.2f} daily cap, ${max(b['ai_left_today'], 0):.2f} left.")
    if b.get("ai_month") is not None:
        out.append(f"Budget: ${b['ai_month']:.2f} of AI spent so far this month.")
    out.append(f"Budget: the hard stop on the OpenRouter key is {b.get('hard_stop')}.")
    return out


def knowledge_facts(con, now=None):
    waiting = knowledge.entries(con, status="proposed")
    out = [f"Knowledge: {len(waiting)} entr{'y' if len(waiting) == 1 else 'ies'} "
           f"proposed and waiting for the owner."]
    for e in knowledge.due(con, now=now):
        out.append(f"Knowledge: #{e['id']} \"{e['title']}\" is past its review date.")
    return out


def previous(day, days=None):
    """The newest morning before `day` that actually ran, or None."""
    days = Path(days or DAYS)
    for f in sorted(days.glob("*.json"), reverse=True):
        if f.stem >= day:
            continue
        doc = read_day(f)
        if doc and doc.get("status") == "ran":
            return doc
    return None


def decision_facts(doc):
    """What the owner did with the last proposals - the GM's only feedback."""
    if not doc:
        return ["Last proposals: none - this is the GM's first morning."]
    out = []
    for p in doc.get("proposals") or []:
        d = p.get("decision")
        if not d:
            out.append(f"Last proposals ({doc['day']}): the owner has not decided on "
                       f"\"{p['title']}\".")
            continue
        verb = "approved" if d["verdict"] == "approve" else "declined"
        note = f" Note: {d['note']}" if d.get("note") else ""
        out.append(f"Last proposals ({doc['day']}): the owner {verb} \"{p['title']}\".{note}")
    return out or [f"Last proposals ({doc['day']}): there were none."]


def compile_facts(b, con, day, now=None, reports=FURY_REPORTS, days=None):
    return (fury_facts(reports, now) + budget_facts(b) + knowledge_facts(con, now)
            + decision_facts(previous(day, days)))


# ------------------------------------------------------------------ the call

def roster():
    return "\n".join(f"  {dept}: {', '.join(people) if people else '(no agent)'}"
                     for dept, people in knowledge.DEPARTMENTS.items())


def people():
    return {a for members in knowledge.DEPARTMENTS.values() for a in members} | {"owner"}


def messages(facts, rules, day, cap):
    cap_words = f"${cap:.2f} a day" if cap is not None else "a daily cap"
    system = f"""You are the GM of a small business run by one owner and a few AI agents.
Each morning you read the facts below and propose what should happen today.

This month you are PROPOSE-ONLY. You write proposals; the owner approves or
declines each one. You assign nothing and create no tasks.

Rules - code checks the first three and drops any proposal that breaks them:
1. Every proposal cites the facts it rests on, by id (F1, F2, ...).
2. Do no arithmetic. Any number in a proposal's "why" must appear in a fact it cites.
3. "dept" and "who" must come from the team list. "who" may also be "owner".
4. At most {MAX_PROPOSALS} proposals, most valuable first. Fewer strong proposals
   beat many weak ones; none is a fine answer on a quiet day.
5. Prefer actions that cost nothing. All AI spend is capped at {cap_words}.
6. Accepted knowledge marked "decision" is the owner's call. Do not propose
   reversing one unless a fact shows it is causing harm - then cite that fact.
7. Learn from the owner's decisions on the last proposals. Do not re-propose
   something declined unless a fact has changed.
8. "knowledge": at most {MAX_KNOWLEDGE} things learned from these facts that will
   still be true next week. confidence is measured, observed or believed;
   measured needs evidence someone can re-check.

Reply with one JSON object and nothing else:
{{"summary": "two or three sentences on the state of things",
  "proposals": [{{"title": "...", "dept": "...", "who": "...",
                 "action": "what to do, concretely", "why": "...",
                 "facts": ["F1"]}}],
  "knowledge": [{{"dept": "...", "kind": "fact|lesson|decision|playbook|question",
                 "title": "...", "body": "...", "evidence": "...",
                 "confidence": "observed"}}]}}"""
    numbered = "\n".join(f"F{i} {f}" for i, f in enumerate(facts, 1))
    user = (f"Today is {day} (Eastern).\n\nFACTS\n{numbered}\n\n"
            f"ACCEPTED KNOWLEDGE\n{rules or '(none accepted yet)'}\n\nTEAM\n{roster()}\n")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def call_model(msgs, key, slug, timeout=180):
    """One chat call through OpenRouter. Returns (text, usage)."""
    body = {"model": slug, "messages": msgs, "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "reasoning": REASONING,
            "usage": {"include": True}}
    req = urllib.request.Request(
        ea.OR_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/cljxs/cljxs", "X-Title": "gm"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"OpenRouter: {str(data['error'])[:200]}")
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    usage = dict(data.get("usage") or {})
    # Why the reply ended. "length" beside an empty answer means the cap went
    # on reasoning - the one failure that looks like the model saying nothing.
    usage["finish_reason"] = choice.get("finish_reason")
    return text, usage


# ----------------------------------------------------------------- the reply

def parse_reply(text):
    """The JSON object in a reply, tolerating a code fence around it."""
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the reply holds no JSON object")
    doc = json.loads(t[start:end + 1])
    if not isinstance(doc, dict):
        raise ValueError("the reply is not a JSON object")
    return doc


_ID_RE = re.compile(r"\b[Ff]\d+\b|#\d+")
_NUM_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def numbers(text):
    """Every number in `text` as a value, ignoring fact and entry ids - "F3"
    and "#9" are references, not claims. $1 and $1.00 are the same number."""
    stripped = _ID_RE.sub(" ", text or "")
    return {float(n.replace(",", "")) for n in _NUM_RE.findall(stripped)}


def _text(v, limit):
    return " ".join(str(v or "").split())[:limit]


def check_proposal(p, facts):
    """(proposal, None) if it stands, (None, reason) if it is dropped."""
    if not isinstance(p, dict):
        return None, "not an object"
    title = _text(p.get("title"), TITLE_MAX)
    if not title:
        return None, "no title"
    dept = str(p.get("dept") or "").strip().lower()
    if dept not in knowledge.DEPARTMENTS:
        return None, f"unknown department {dept!r}"
    who = str(p.get("who") or "").strip().lower()
    if who not in people():
        return None, f"unknown person {who!r}"
    action = _text(p.get("action"), TEXT_MAX)
    why = _text(p.get("why"), TEXT_MAX)
    if not action or not why:
        return None, "missing the action or the why"

    cited = p.get("facts") if isinstance(p.get("facts"), list) else []
    ids = []
    for c in cited:
        m = re.fullmatch(r"[Ff](\d+)", str(c).strip())
        if not m or not 1 <= int(m.group(1)) <= len(facts):
            return None, f"cites {c!r}, which is not a fact it was given"
        ids.append(int(m.group(1)))
    if not ids:
        return None, "cites no facts"

    grounded = set()
    for i in ids:
        grounded |= numbers(facts[i - 1])
    unfounded = sorted(numbers(why) - grounded)
    if unfounded:
        shown = ", ".join(f"{n:g}" for n in unfounded)
        return None, f"its why uses {shown}, which is in none of the facts it cites"

    return {"title": title, "dept": dept, "who": who, "action": action, "why": why,
            "facts": [f"F{i}" for i in sorted(set(ids))]}, None


def check_reply(doc, facts):
    """Split a parsed reply into what stands and what is dropped."""
    summary = _text(doc.get("summary"), SUMMARY_MAX)
    kept, dropped = [], []
    raw = doc.get("proposals") if isinstance(doc.get("proposals"), list) else []
    for p in raw:
        title = _text(p.get("title") if isinstance(p, dict) else "", TITLE_MAX) or "(untitled)"
        if len(kept) >= MAX_PROPOSALS:
            dropped.append({"title": title, "reason": f"over the limit of {MAX_PROPOSALS}"})
            continue
        ok, why = check_proposal(p, facts)
        if ok:
            ok["n"] = len(kept) + 1
            ok["decision"] = None
            kept.append(ok)
        else:
            dropped.append({"title": title, "reason": why})
    ks = doc.get("knowledge") if isinstance(doc.get("knowledge"), list) else []
    return summary, kept, dropped, ks


def file_knowledge(con, suggestions):
    """Hand the GM's suggestions to knowledge.py, which proposes or refuses."""
    out = []
    for i, k in enumerate(suggestions):
        if not isinstance(k, dict):
            continue
        title = _text(k.get("title"), 200) or "(untitled)"
        if i >= MAX_KNOWLEDGE:
            out.append({"title": title, "result": f"not filed: over the limit of {MAX_KNOWLEDGE}"})
            continue
        try:
            new_id = knowledge.add(con, k.get("dept"), k.get("kind"), k.get("title"),
                                   k.get("body"), k.get("evidence") or "",
                                   k.get("confidence") or "believed", "gm")
            out.append({"title": title, "result": f"proposed #{new_id}"})
        except knowledge.Refused as exc:
            out.append({"title": title, "result": f"refused: {exc}"})
    return out


# ------------------------------------------------------------------ the days

def day_path(day, days=None):
    return Path(days or DAYS) / f"{day}.json"


def read_day(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def write_day(doc, days=None):
    path = day_path(doc["day"], days)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)
    return path


def latest(days=None):
    files = sorted(Path(days or DAYS).glob("*.json"))
    return read_day(files[-1]) if files else None


def run(force=False, dry_run=False, now=None, days=None, con=None,
        read_budget=None, call=None, key=None, reports=FURY_REPORTS):
    """The morning. Returns (exit code, the day's record). Injectable parts
    are what the tests replace: the budget read, the model call and the key."""
    now = now or utc_now()
    day = et_time.day(et_time.to_eastern(now))
    existing = read_day(day_path(day, days))
    if existing and existing.get("status") == "ran" and not force and not dry_run:
        return 0, existing

    doc = {"day": day, "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "model": model(), "status": None, "reason": None, "cost_usd": None,
           "facts": [], "summary": "", "proposals": [], "dropped": [], "knowledge": []}

    def finish(status, reason=None, code=0):
        doc["status"], doc["reason"] = status, reason
        if not dry_run:
            write_day(doc, days)
        return code, doc

    try:
        b = (read_budget or budget.read)()
    except Exception as exc:
        return finish("skipped", f"the budget could not be read ({exc}) - "
                                 f"the GM does not spend against an unknown budget")
    if b.get("state") != "ok":
        return finish("skipped", f"the budget says {b.get('state')!r}")
    if b.get("ai_left_today") is None or b["ai_left_today"] < MIN_LEFT:
        return finish("skipped", f"less than ${MIN_LEFT:.2f} of today's AI allowance is left")

    own_con = con is None
    con = con or knowledge.connect()
    try:
        facts = compile_facts(b, con, day, now, reports, days)
        rules = knowledge.brief(con, knowledge.EVERY)
        doc["facts"] = facts
        msgs = messages(facts, rules, day, b.get("cap"))
        if dry_run:
            print(msgs[0]["content"] + "\n\n" + msgs[1]["content"])
            return 0, doc

        key = key if key is not None else budget.preflight.openrouter_key()
        if not key:
            return finish("failed", "no OPENROUTER_API_KEY found", 1)
        try:
            text, usage = (call or call_model)(msgs, key, model())
        except Exception as exc:
            return finish("failed", f"the model call failed: {str(exc)[:200]}", 1)
        doc["cost_usd"] = ea.cost_of(usage)
        doc["usage"] = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens",
                                                   "finish_reason")}
        try:
            reply = parse_reply(text)
        except ValueError as exc:
            doc["reply_head"] = (text or "")[:500]
            return finish("failed", f"the reply could not be read: {exc}", 1)

        doc["summary"], doc["proposals"], doc["dropped"], ks = check_reply(reply, facts)
        doc["knowledge"] = file_knowledge(con, ks)
        return finish("ran")
    finally:
        if own_con:
            con.close()


def decide(day, n, verdict, note="", days=None, now=None):
    """The owner's answer to one proposal. Changing one's mind is allowed;
    the latest answer is the one kept."""
    if verdict not in VERDICTS:
        raise knowledge.Refused(f"the verdict is approve or decline, not {verdict!r}")
    doc = read_day(day_path(day, days))
    if not doc:
        raise knowledge.Refused(f"no GM morning on {day}")
    match = [p for p in doc.get("proposals") or [] if p.get("n") == n]
    if not match:
        raise knowledge.Refused(f"{day} has no proposal {n}")
    match[0]["decision"] = {"verdict": verdict, "note": _text(note, NOTE_MAX),
                            "at": (now or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")}
    write_day(doc, days)
    return match[0]


def lines(doc):
    if not doc:
        return ["The GM has not run yet."]
    out = [f"GM, {doc['day']} - {doc['status']}"
           + (f" (${doc['cost_usd']:.4f}, {doc['model']})" if doc.get("cost_usd") is not None else "")]
    if doc.get("reason"):
        out.append(f"  {doc['reason']}")
    if doc.get("summary"):
        out.append(f"  {doc['summary']}")
    for p in doc.get("proposals") or []:
        d = p.get("decision")
        state = "waiting" if not d else ("approved" if d["verdict"] == "approve" else "declined")
        out.append(f"\n  {p['n']}. {p['title']}  [{p['dept']} / {p['who']}, {state}]")
        out.append(f"     do:  {p['action']}")
        out.append(f"     why: {p['why']} ({', '.join(p['facts'])})")
    for d in doc.get("dropped") or []:
        out.append(f"\n  dropped: {d['title']} - {d['reason']}")
    for k in doc.get("knowledge") or []:
        out.append(f"  knowledge: {k['title']} - {k['result']}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--force", action="store_true", help="run again today")
    p.add_argument("--dry-run", action="store_true", help="print the prompt, call nothing")
    p = sub.add_parser("show")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("decide")
    p.add_argument("day")
    p.add_argument("n", type=int)
    p.add_argument("verdict", choices=VERDICTS)
    p.add_argument("--note", default="")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        code, doc = run(force=a.force, dry_run=a.dry_run)
        if not a.dry_run:
            print("\n".join(lines(doc)))
        return code
    if a.cmd == "show":
        doc = latest()
        print(json.dumps(doc or {"status": "none"}, indent=2) if a.json else "\n".join(lines(doc)))
        return 0
    try:
        p = decide(a.day, a.n, a.verdict, a.note)
    except knowledge.Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"{a.verdict}d {a.day} #{p['n']}: {p['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
