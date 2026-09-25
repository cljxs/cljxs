#!/usr/bin/env python3
"""
gm.py — the GM's morning: the day's facts in, a few proposals out.

    python3 scripts/gm.py run                  # the morning run (the timer does this)
    python3 scripts/gm.py run --dry-run        # print the facts and prompt, call nothing
    python3 scripts/gm.py show                 # the latest morning, in plain words
    python3 scripts/gm.py show --json          # the same, for the Deck
    python3 scripts/gm.py decide 2026-09-26 1 approve --note "yes, today"
    python3 scripts/gm.py decide 2026-09-26 2 decline --note "not while totes-only"

THE GM PROPOSES; THE OWNER DECIDES; CODE SENDS. The GM itself creates no
task and wakes no agent. Each proposal is for the owner or for one agent, and
an agent proposal must name one of that agent's TASKS - the only jobs that
exist for it to be handed. When the owner approves an agent proposal, code
sends that task at once (if today's budget has room and, for a build, the
idea is still waiting); an owner proposal is the owner's to-do. Every
decision is kept with its note, and with what happened when it was sent, for
the GM to read the next morning - that is how it learns what the owner wants.

Approving used to send nothing (the first-month rule, 2026-09-25). The owner
changed it the same day, after the GM proposed jobs no agent could do -
"Scout: edit the listing's tags" - which is also why an agent proposal must
now name a task from the list rather than describe one.

ONE MODEL CALL A DAY, AND ONLY WHEN THERE IS ROOM FOR IT. The GM runs after
Fury and asks budget.py first: if today's AI allowance is spent, unreadable,
or down to less than MIN_LEFT, it writes down why it skipped and stays asleep.
It never runs twice in a day unless told to with --force.

CODE COMPILES THE FACTS, WHEN IT RUNS; THE MODEL ONLY WEIGHS THEM. Every fact
is a numbered line - Fury's briefing built live by Fury's own collector, the
budget, the knowledge store, yesterday's decisions - and every proposal must
cite the facts it rests on. Code then checks the
reply, because a model asked to reason about numbers will eventually claim a
number nobody gave it:

  * a proposal citing no fact, or a fact that does not exist, is dropped
  * a number in a proposal's `why` must appear in the facts it cites
  * departments and people must be real ones
  * a proposal for an agent is in that agent's department and cites at
    least one fact about it - "run Scout" once rested on a fact about Ace's
    betting run and the knowledge count, and passed every other check
  * a proposal for an agent names one of that agent's TASKS, with a Scout
    idea that is really waiting when the task needs one; a proposal for an
    agent that has no tasks is dropped - that work belongs to the owner
  * at most MAX_PROPOSALS proposals and MAX_KNOWLEDGE knowledge suggestions
    (zero for now - see MAX_KNOWLEDGE)

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
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent
DAYS = Path(os.environ.get("GM_DIR", ROOT / "tasks" / "gm"))

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
fury = _load("fury_collect", "fury-collect.py") # the system's state, and the briefing
new_build = _load("emily_new_build", "emily-new-build.py")  # what a build is booked at

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
# Knowledge the GM may propose each morning. Zero since its first real run
# (2026-09-25): all three of its suggestions restated that morning's facts -
# "3 Scout ideas are pending", "the daily budget is not exhausted" - which are
# stale the next day. One morning's snapshot cannot show what is still true
# next week. Raise this when the GM reads more than one day at a time.
MAX_KNOWLEDGE = 0
TITLE_MAX = 90
TEXT_MAX = 400
ASK_MAX = 300        # what the prompt asks for; TEXT_MAX is where code cuts
SUMMARY_MAX = 700

VERDICTS = ("approve", "decline")
NOTE_MAX = 300

# Each agent in a line, for the team the GM is shown. Every agent in
# knowledge.DEPARTMENTS needs one; a test holds them together.
ROLES = {
    "emily":   "turns a Scout idea into a product draft (art, Printify product, "
               "listing text). Never publishes.",
    "scout":   "searches the Etsy market and proposes product ideas for the owner to review.",
    "belfort": "paper-trades stocks on its own schedule.",
    "ace":     "places paper sports bets on its own schedule.",
    "fury":    "writes the morning briefing, in plain code.",
}

# THE ONLY JOBS AN APPROVED PROPOSAL CAN SEND. Each is a command that already
# exists and already works from the Deck - nothing here asks an agent to do a
# thing it has no instructions for. Emily's only task type is a product build;
# a task handed to Scout would wake a model with no idea what to do with it,
# so Scout's job is its own cycle, started the way its timer starts it.
#
# min_left is how much of today's AI allowance must be left before a task is
# sent. A build uses Emily's own booking. Scout's run has its own gate
# (should-run) and holds without waking a model when it has nothing to do.
TASKS = {
    "build_idea": {
        "who": "emily", "needs_idea": True,
        "does": 'build a product draft from one Scout idea waiting for review - '
                'give its number as "idea"',
        "min_left": new_build.COST_ESTIMATE,
    },
    "run_scout": {
        "who": "scout", "needs_idea": False,
        "does": "run Scout's market search now instead of at tomorrow's 08:00 run",
        "min_left": MIN_LEFT,
    },
}

# Long enough for a build's artwork to be drawn before Emily is queued - the
# same path as Scout's Approve button, which the Deck gives sixty seconds.
SEND_TIMEOUT = 110


def model():
    return os.environ.get("GM_MODEL", "").strip() or DEFAULT_MODEL


def utc_now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- facts

def _plain(text):
    return re.sub(r"\*\*|__|`", "", text).strip()


def live_briefing(now=None):
    """Fury's briefing, built from the system as it is right now.

    Not the file Fury wrote at 08:30. The GM read that file once at 11:00 and
    told the owner three Scout ideas were waiting - ideas approved in the
    Command Center in between. Same collector and same wording as Fury's,
    so there is still one account of the system; it is just read when the GM
    runs, and never written.
    """
    now = now or utc_now()
    return fury.build_briefing(fury.collect(now), now.strftime("%Y-%m-%d"))


def briefing_facts(text):
    """A briefing's bullet lines as facts, each with its section."""
    out, section = [], "Briefing"
    for line in (text or "").splitlines():
        if line.startswith("## "):
            section = _plain(line[3:]).strip(" \u26a0\ufe0f") or section
            continue
        s = line.strip()
        if s.startswith("- ") and s[2:].strip().lower() not in ("nothing.",):
            out.append(f"{section}: {_plain(s[2:])}")
    return out or ["The system briefing lists nothing."]


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
        sent = p.get("sent") or {}
        if sent.get("status") == "sent":
            note += f" It was sent to {p['who']}."
        elif sent:
            note += f" It was not sent: {sent.get('detail')}"
        out.append(f"Last proposals ({doc['day']}): the owner {verb} \"{p['title']}\".{note}")
    return out or [f"Last proposals ({doc['day']}): there were none."]


def waiting_ideas():
    """Scout's ideas still waiting on the owner, read now, by Scout's own rule."""
    d, _ = fury.scout.load_ideas()
    return [i for i in d["ideas"] if fury.scout.is_pending(i)]


def idea_number(v):
    """An idea number as the model may write it - 7, "7", "#7" - or None."""
    m = re.fullmatch(r"#?\s*(\d{1,6})", str(v if v is not None else "").strip())
    return int(m.group(1)) if m else None


def idea_facts(ideas):
    """One fact per waiting idea, with the number a build_idea task needs."""
    return [f"Scout idea #{i.get('id')} is waiting for review: {i.get('title')} "
            f"({i.get('product') or 'product not given'})." for i in ideas]


def compile_facts(b, con, day, now=None, briefing=None, days=None, ideas=()):
    """Every fact the GM is given, read at the moment it runs."""
    text = (briefing or live_briefing)(now)
    return (briefing_facts(text) + idea_facts(ideas) + budget_facts(b)
            + knowledge_facts(con, now) + decision_facts(previous(day, days)))


# ------------------------------------------------------------------ the call

# Facts that belong to a department without naming one of its agents. The
# store section is Fury's "## The store" (see briefing_facts), and waiting
# ideas are written by idea_facts - both are the Etsy side.
DEPT_MARKERS = {"etsy": ("The store:", "Scout idea #")}


def department_of(agent):
    return next((d for d, members in knowledge.DEPARTMENTS.items() if agent in members), None)


def fact_departments(fact):
    """The departments a fact is about: any whose agent it names, whole word,
    plus the markers above. A fact about the budget or the knowledge store is
    about no department in particular."""
    out = {d for d, members in knowledge.DEPARTMENTS.items()
           if any(re.search(rf"\b{re.escape(a)}\b", fact, re.I) for a in members)}
    out |= {d for d, marks in DEPT_MARKERS.items() if fact.startswith(marks)}
    return out


def tasks_for(agent):
    return [t for t, spec in TASKS.items() if spec["who"] == agent]


def roster():
    """The team as the GM is shown it: who each agent is and exactly what
    each can be sent. Built from ROLES, DEPARTMENTS and TASKS, so the list
    the model reads and the list code checks against cannot differ."""
    out = []
    for dept, members in knowledge.DEPARTMENTS.items():
        for agent in members:
            out.append(f"  {agent} ({dept}): {ROLES.get(agent, '')}")
            jobs = tasks_for(agent)
            for t in jobs:
                out.append(f"      can be sent: {t} - {TASKS[t]['does']}")
            if not jobs:
                out.append(f"      nothing can be sent to {agent} - give that work to the owner")
    out.append("  owner (any department): does everything else, including publishing "
               "and editing listings. A proposal for the owner has no task.")
    out.append("  departments: " + ", ".join(knowledge.DEPARTMENTS))
    return "\n".join(out)


def people():
    return {a for members in knowledge.DEPARTMENTS.values() for a in members} | {"owner"}


def messages(facts, rules, day, cap):
    cap_words = f"${cap:.2f} a day" if cap is not None else "a daily cap"
    if MAX_KNOWLEDGE:
        knowledge_rule = (f"12. \"knowledge\": at most {MAX_KNOWLEDGE} things learned from these "
                          f"facts that will\n    still be true next week. confidence is measured, "
                          f"observed or believed;\n    measured needs evidence someone can re-check.\n")
        knowledge_shape = (',\n  "knowledge": [{"dept": "...", "kind": "lesson|playbook|question", '
                           '"title": "...", "body": "...", "evidence": "...", "confidence": "observed"}]')
    else:
        knowledge_rule, knowledge_shape = "", ""
    system = f"""You are the GM of a small business run by one owner and a few AI agents.
Each morning you read the facts below and propose what should happen today.

You propose; the owner approves or declines each proposal. An approved
proposal for an agent is sent to that agent as the task you named. An
approved proposal for the owner is the owner's to-do.

Rules - code checks the first four and drops any proposal that breaks them:
1. Every proposal cites the facts it rests on, by id (F1, F2, ...). A
   proposal for an agent is in that agent's department and cites at least
   one fact about that department.
2. Do no arithmetic. Any number in a proposal's "why" must appear in a fact it cites.
3. "dept" and "who" must come from the team list. "who" may also be "owner".
4. A proposal for an agent names, in "task", one job that agent "can be sent".
   build_idea also needs "idea": the number of a Scout idea the facts say is
   waiting. If a job is not on an agent's list it is the owner's: set "who"
   to "owner" and leave "task" out.
5. At most {MAX_PROPOSALS} proposals, most valuable first. Fewer strong proposals
   beat many weak ones; none is a fine answer on a quiet day.
6. A proposal is ONE concrete step that moves a sale closer or fixes
   something that is broken. Not a new process, report, scoring scheme,
   checklist or review of other proposals.
7. Say nothing about the AI budget. It is capped at {cap_words} and code enforces it.
8. Accepted knowledge marked "decision" is the owner's call. Do not propose
   reversing one unless a fact shows it is causing harm - then cite that fact.
9. Apply a lesson or playbook only to what it names. A rule about all-over
   prints applies to all-over products, not to every tote. If no fact says what
   kind of product something is, do not assume.
10. Learn from the owner's decisions on the last proposals. Do not re-propose
    something declined unless a fact has changed.
11. Keep "action" and "why" under {ASK_MAX} characters each.
{knowledge_rule}
Reply with one JSON object and nothing else:
{{"summary": "two or three sentences on the state of things",
  "proposals": [{{"title": "...", "dept": "...", "who": "...",
                 "task": "build_idea | run_scout | leave out for the owner",
                 "idea": 7,
                 "action": "what to do, concretely", "why": "...",
                 "facts": ["F1"]}}]{knowledge_shape}}}"""
    numbered = "\n".join(f"F{i} {f}" for i, f in enumerate(facts, 1))
    user = (f"Today is {day} (Eastern).\n\nFACTS\n{numbered}\n\n"
            f"ACCEPTED KNOWLEDGE\n{rules or '(none accepted yet)'}\n\n"
            f"TEAM - who each one is, and what each can be sent\n{roster()}\n")
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
    """Whitespace squeezed, and cut at `limit` with a visible mark - a cut
    that ends mid-word with nothing to show for it reads as the whole text."""
    t = " ".join(str(v or "").split())
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "\u2026"


def check_task(p, who, waiting):
    """(task fields, None) or (None, reason). `waiting` is the set of Scout
    idea numbers waiting for review right now."""
    task = str(p.get("task") or "").strip().lower() or None
    if who == "owner":
        if task:
            return None, "the owner is not sent tasks - an owner proposal has no task"
        return {}, None
    allowed = tasks_for(who)
    if not allowed:
        return None, f"nothing can be sent to {who} - that work is the owner's"
    if task not in allowed:
        return None, (f"{who} can only be sent {', '.join(allowed)}, not {task!r}"
                      if task else f"names no task for {who} (one of: {', '.join(allowed)})")
    out = {"task": task}
    if TASKS[task]["needs_idea"]:
        idea = idea_number(p.get("idea"))
        if idea is None or idea not in waiting:
            return None, (f"idea {p.get('idea')!r} is not a Scout idea waiting for review")
        out["idea"] = idea
    return out, None


def check_proposal(p, facts, waiting=frozenset()):
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
    task, reason = check_task(p, who, waiting)
    if reason:
        return None, reason

    cited = p.get("facts") if isinstance(p.get("facts"), list) else []
    ids = []
    for c in cited:
        m = re.fullmatch(r"[Ff](\d+)", str(c).strip())
        if not m or not 1 <= int(m.group(1)) <= len(facts):
            return None, f"cites {c!r}, which is not a fact it was given"
        ids.append(int(m.group(1)))
    if not ids:
        return None, "cites no facts"

    if who != "owner":
        home = department_of(who)
        if dept != home:
            return None, f"{who} works in {home}, not {dept}"
        if not any(home in fact_departments(facts[i - 1]) for i in ids):
            about = "; ".join(f"F{i} is about "
                              f"{', '.join(sorted(fact_departments(facts[i - 1]))) or 'no department'}"
                              for i in sorted(set(ids)))
            return None, f"cites nothing about {home} ({about})"

    grounded = set()
    for i in ids:
        grounded |= numbers(facts[i - 1])
    unfounded = sorted(numbers(why) - grounded)
    if unfounded:
        shown = ", ".join(f"{n:g}" for n in unfounded)
        return None, f"its why uses {shown}, which is in none of the facts it cites"

    return dict({"title": title, "dept": dept, "who": who, "action": action, "why": why,
                 "facts": [f"F{i}" for i in sorted(set(ids))]}, **task), None


def check_reply(doc, facts, waiting=frozenset()):
    """Split a parsed reply into what stands and what is dropped."""
    summary = _text(doc.get("summary"), SUMMARY_MAX)
    kept, dropped = [], []
    raw = doc.get("proposals") if isinstance(doc.get("proposals"), list) else []
    for p in raw:
        title = _text(p.get("title") if isinstance(p, dict) else "", TITLE_MAX) or "(untitled)"
        if len(kept) >= MAX_PROPOSALS:
            dropped.append({"title": title, "reason": f"over the limit of {MAX_PROPOSALS}"})
            continue
        ok, why = check_proposal(p, facts, waiting)
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
            why = (f"over the limit of {MAX_KNOWLEDGE}" if MAX_KNOWLEDGE
                   else "the GM does not propose knowledge yet")
            out.append({"title": title, "result": f"not filed: {why}"})
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
        read_budget=None, call=None, key=None, briefing=None, ideas=None):
    """The morning. Returns (exit code, the day's record). Injectable parts
    are what the tests replace: the budget read, the briefing, Scout's
    waiting ideas, the model call and the key."""
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
        waiting = (ideas or waiting_ideas)()
        facts = compile_facts(b, con, day, now, briefing, days, waiting)
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

        numbers_waiting = {idea_number(i.get("id")) for i in waiting}
        doc["summary"], doc["proposals"], doc["dropped"], ks = check_reply(
            reply, facts, numbers_waiting)
        doc["knowledge"] = file_knowledge(con, ks)
        return finish("ran")
    finally:
        if own_con:
            con.close()


def command_for(p, day):
    """The one command that sends proposal `p`. Argument lists only - no
    shell - so nothing the model wrote can become part of a command line
    except a checked idea number."""
    if p["task"] == "build_idea":
        return [sys.executable, str(SCRIPTS / "scout-review.py"), "approve", str(p["idea"]),
                "--reason", f"approved from the GM's proposal {day} #{p['n']}"]
    if p["task"] == "run_scout":
        return ["systemctl", "start", "--no-block", "scout-cycle.service"]
    raise knowledge.Refused(f"no way to send task {p['task']!r}")


def run_command(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer within {SEND_TIMEOUT}s"
    except OSError as exc:
        return 127, "", str(exc)


def _tail(text, n=3):
    return " ".join(l.strip() for l in (text or "").strip().splitlines()[-n:] if l.strip())[:300]


def send(p, day, read_budget=None, runner=None, ideas=None, now=None):
    """Send an approved agent proposal. Returns what happened, as a record
    kept on the proposal: {"status": "sent" | "not sent", "detail", "at"}.

    Checked again at the moment of sending, not trusted from the morning:
    the budget may be spent by now, and the idea may have been approved or
    rejected in Scout's house since the GM read it.
    """
    at = (now or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec = TASKS[p["task"]]

    def result(status, detail):
        return {"status": status, "detail": detail, "at": at}

    try:
        b = (read_budget or budget.read)()
    except Exception as exc:
        return result("not sent", f"the budget could not be read ({exc})")
    left = b.get("ai_left_today")
    if b.get("state") != "ok" or left is None or left < spec["min_left"]:
        shown = "unknown" if left is None else f"${max(left, 0):.2f}"
        return result("not sent", f"today's AI allowance has {shown} left and this needs "
                                  f"${spec['min_left']:.2f} - approve it again after the "
                                  f"daily reset (7pm Central)")
    if spec["needs_idea"]:
        still = {idea_number(i.get("id")) for i in (ideas or waiting_ideas)()}
        if p.get("idea") not in still:
            return result("not sent", f"Scout idea #{p.get('idea')} is no longer waiting - "
                                      f"it was approved or rejected since this morning")
    code, out, err = (runner or run_command)(command_for(p, day))
    if code == 0:
        return result("sent", _tail(out) or "started")
    return result("not sent", _tail(err) or _tail(out) or f"exit {code}")


def decide(day, n, verdict, note="", days=None, now=None, **send_kw):
    """The owner's answer to one proposal. Approving an agent proposal sends
    its task; approving it again after a send that did not go through tries
    again. What was sent cannot be declined afterwards - it is already on its
    way, and the queue is where it can be stopped."""
    if verdict not in VERDICTS:
        raise knowledge.Refused(f"the verdict is approve or decline, not {verdict!r}")
    doc = read_day(day_path(day, days))
    if not doc:
        raise knowledge.Refused(f"no GM morning on {day}")
    match = [p for p in doc.get("proposals") or [] if p.get("n") == n]
    if not match:
        raise knowledge.Refused(f"{day} has no proposal {n}")
    p = match[0]
    already = (p.get("sent") or {}).get("status") == "sent"
    if verdict == "decline" and already:
        raise knowledge.Refused(f"#{n} was already sent to {p['who']} - it cannot be "
                                f"called back from here")
    p["decision"] = {"verdict": verdict, "note": _text(note, NOTE_MAX),
                     "at": (now or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if verdict == "approve" and p.get("task") and not already:
        p["sent"] = send(p, day, now=now, **send_kw)
    write_day(doc, days)
    return p


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
        job = f", task {p['task']}" + (f" idea #{p['idea']}" if p.get("idea") else "") if p.get("task") else ""
        out.append(f"\n  {p['n']}. {p['title']}  [{p['dept']} / {p['who']}{job}, {state}]")
        out.append(f"     do:  {p['action']}")
        out.append(f"     why: {p['why']} ({', '.join(p['facts'])})")
        if p.get("sent"):
            out.append(f"     {p['sent']['status']}: {p['sent']['detail']}")
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
    said = f"{a.verdict}d {a.day} #{p['n']}: {p['title']}"
    sent = p.get("sent")
    if sent and sent["status"] == "sent" and a.verdict == "approve":
        said += f" - sent to {p['who']}: {sent['detail']}"
    elif sent and a.verdict == "approve":
        said += f" - NOT sent to {p['who']}: {sent['detail']}"
    elif a.verdict == "approve" and not p.get("task"):
        said += " - this one is yours to do"
    print(said)
    return 0


if __name__ == "__main__":
    sys.exit(main())
