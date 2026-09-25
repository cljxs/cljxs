#!/usr/bin/env python3
"""
knowledge.py — what the ecosystem has learned, kept where every agent can read it.

    python3 scripts/knowledge.py list                      # everything, newest first
    python3 scripts/knowledge.py list --status proposed    # waiting on the owner
    python3 scripts/knowledge.py add --dept etsy --kind lesson --title "..." --body "..." --evidence "..." --confidence measured --source scout
    python3 scripts/knowledge.py accept 12
    python3 scripts/knowledge.py retire 12 --why "superseded by #19"
    python3 scripts/knowledge.py brief --dept etsy         # what an agent is handed
    python3 scripts/knowledge.py due                       # accepted entries past review
    python3 scripts/knowledge.py renew 12                  # still true: push the review out
    python3 scripts/knowledge.py seed                      # the starting Etsy entries

WHY A STORE AND NOT MEMORY.md. Each agent's MEMORY.md is its own diary; what
Scout learns about the tote market never reaches Emily, and nothing in a diary
is ever checked again. This is the shared half: short entries, each with a
department, a kind, where it came from and how sure anyone is.

ONLY THE OWNER MAKES AN ENTRY TRUE. Anyone - an agent, the GM, a script -
can `add`, and what they add is `proposed`. Agents are briefed from
`accepted` entries only. Accepting is the owner's click in the Town Hall (or
this script's `accept`). That is the propose-only month in one rule: the
system can suggest what it has learned, and cannot teach itself.

WHAT KEEPS IT CLEAN - every one of these is a refusal in code, not a guideline:
  * a title and a short body, nothing essay-length (TITLE_MAX, BODY_MAX)
  * a known department and kind, and a confidence word (not a percentage -
    a model writing "87% confident" is doing arithmetic it did not do)
  * `measured` needs evidence: a file, a command, a number someone can re-check
  * no second open entry with the same title in the same department
  * every accepted entry has a review date; `due` lists the ones past it,
    and an entry nobody renews is an entry nobody is vouching for

Point at the code, do not restate it. An entry that says "the palette check
passes at 50%" goes stale the day the threshold moves. "knockout.py
--check --all-over decides" stays true.

The database is tasks/knowledge.db, beside the task queue and ignored by git
like it. This script owns its schema; the Deck calls this script rather than
opening the file itself, so there is one reader and one set of rules.

Standard library only.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
DB = Path(os.environ.get("KNOWLEDGE_DB", ROOT / "tasks" / "knowledge.db"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402

# Departments and the agents who work in them. The one list of which agent
# belongs where; the GM and the brief both read it from here.
DEPARTMENTS = {
    "all":      [],
    "etsy":     ["emily", "scout"],
    "trading":  ["belfort"],
    "betting":  ["ace"],
    "props":    [],
    "ops":      ["fury"],
}

KINDS = {
    "fact":     "true of the world right now (a price, a limit, a format)",
    "lesson":   "we did X, Y happened - do or avoid it next time",
    "decision": "the owner chose this; do not relitigate it",
    "playbook": "the steps for a thing done more than once",
    "question": "open - nobody knows yet, and it matters",
}

# How sure, in words a person uses. `measured` means a number or output
# someone can re-run; `observed` means seen happen, not measured; `believed`
# means it should be true and nobody has checked.
CONFIDENCE = ("measured", "observed", "believed")

STATUSES = ("proposed", "accepted", "retired")

TITLE_MAX = 90
BODY_MAX = 600
EVIDENCE_MAX = 300

# Days until an accepted entry must be vouched for again. Facts about the
# world move fastest; a decision holds until the owner changes it, and a
# year is only there so nothing is accepted forever by default.
REVIEW_DAYS = {"fact": 30, "lesson": 60, "decision": 365,
               "playbook": 90, "question": 30}

# A brief is pasted into an agent's prompt, and every turn of that cycle
# re-reads it - cost scales with turns, so the brief is capped.
BRIEF_MAX_CHARS = 2500

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  dept        TEXT NOT NULL,
  kind        TEXT NOT NULL,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  evidence    TEXT NOT NULL DEFAULT '',
  confidence  TEXT NOT NULL,
  source      TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'proposed',
  created_at  TEXT NOT NULL,
  decided_at  TEXT,
  review_by   TEXT,
  why_retired TEXT
);
CREATE INDEX IF NOT EXISTS ix_knowledge_status ON knowledge(status, dept);
"""


class Refused(ValueError):
    """An entry the rules will not take, with the reason in plain words."""


def utc_now():
    return datetime.now(timezone.utc)


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=None):
    path = Path(path or DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def row(r):
    return dict(r) if r is not None else None


def get(con, entry_id):
    return row(con.execute("SELECT * FROM knowledge WHERE id = ?", (entry_id,)).fetchone())


def check(dept, kind, title, body, evidence, confidence, source):
    """Every rule an entry must pass, as refusals. Returns the cleaned fields."""
    dept = (dept or "").strip().lower()
    kind = (kind or "").strip().lower()
    confidence = (confidence or "").strip().lower()
    title = " ".join((title or "").split())
    body = (body or "").strip()
    evidence = (evidence or "").strip()
    source = (source or "").strip().lower()

    if dept not in DEPARTMENTS:
        raise Refused(f"unknown department {dept!r} - one of: {', '.join(DEPARTMENTS)}")
    if kind not in KINDS:
        raise Refused(f"unknown kind {kind!r} - one of: {', '.join(KINDS)}")
    if confidence not in CONFIDENCE:
        raise Refused(f"confidence must be a word - one of: {', '.join(CONFIDENCE)}")
    if not source:
        raise Refused("say who it came from (--source: an agent name, gm or owner)")
    if not title:
        raise Refused("an entry needs a title")
    if len(title) > TITLE_MAX:
        raise Refused(f"title is {len(title)} characters; the limit is {TITLE_MAX}")
    if not body:
        raise Refused("an entry needs a body")
    if len(body) > BODY_MAX:
        raise Refused(f"body is {len(body)} characters; the limit is {BODY_MAX} - "
                      f"split it, or point at the file that holds the detail")
    if len(evidence) > EVIDENCE_MAX:
        raise Refused(f"evidence is {len(evidence)} characters; the limit is {EVIDENCE_MAX}")
    if confidence == "measured" and not evidence:
        raise Refused("measured needs --evidence: the file, command or number "
                      "someone can re-check. Without it, say observed or believed.")
    return dept, kind, title, body, evidence, confidence, source


def add(con, dept, kind, title, body, evidence="", confidence="believed",
        source="owner", now=None):
    """Propose an entry. Returns its id. Raises Refused."""
    dept, kind, title, body, evidence, confidence, source = check(
        dept, kind, title, body, evidence, confidence, source)
    clash = con.execute(
        "SELECT id, status FROM knowledge WHERE dept = ? AND lower(title) = lower(?) "
        "AND status != 'retired'", (dept, title)).fetchone()
    if clash:
        raise Refused(f"#{clash['id']} already has that title in {dept} "
                      f"({clash['status']}) - retire it first, or say what is different")
    cur = con.execute(
        "INSERT INTO knowledge (dept, kind, title, body, evidence, confidence, "
        "source, status, created_at) VALUES (?,?,?,?,?,?,?, 'proposed', ?)",
        (dept, kind, title, body, evidence, confidence, source, stamp(now or utc_now())))
    con.commit()
    return cur.lastrowid


def accept(con, entry_id, now=None):
    """The owner vouches for it. Starts its review clock."""
    e = get(con, entry_id)
    if not e:
        raise Refused(f"no entry #{entry_id}")
    if e["status"] != "proposed":
        raise Refused(f"#{entry_id} is {e['status']}, not proposed")
    now = now or utc_now()
    review = now + timedelta(days=REVIEW_DAYS[e["kind"]])
    con.execute("UPDATE knowledge SET status='accepted', decided_at=?, review_by=? "
                "WHERE id=?", (stamp(now), stamp(review), entry_id))
    con.commit()
    return get(con, entry_id)


def renew(con, entry_id, now=None):
    """Still true: restart the review clock on an accepted entry."""
    e = get(con, entry_id)
    if not e:
        raise Refused(f"no entry #{entry_id}")
    if e["status"] != "accepted":
        raise Refused(f"#{entry_id} is {e['status']} - only accepted entries are renewed")
    now = now or utc_now()
    review = now + timedelta(days=REVIEW_DAYS[e["kind"]])
    con.execute("UPDATE knowledge SET review_by=? WHERE id=?", (stamp(review), entry_id))
    con.commit()
    return get(con, entry_id)


def retire(con, entry_id, why, now=None):
    """No longer true, or never was. Kept, with the reason, not deleted -
    a retired lesson is how the next person learns it was tried."""
    why = " ".join((why or "").split())
    if not why:
        raise Refused("say why it is retired (--why)")
    e = get(con, entry_id)
    if not e:
        raise Refused(f"no entry #{entry_id}")
    if e["status"] == "retired":
        raise Refused(f"#{entry_id} is already retired")
    con.execute("UPDATE knowledge SET status='retired', decided_at=?, why_retired=? "
                "WHERE id=?", (stamp(now or utc_now()), why[:EVIDENCE_MAX], entry_id))
    con.commit()
    return get(con, entry_id)


def entries(con, status=None, dept=None):
    q, args = "SELECT * FROM knowledge WHERE 1=1", []
    if status:
        q += " AND status = ?"
        args.append(status)
    if dept:
        q += " AND dept = ?"
        args.append(dept)
    q += " ORDER BY id DESC"
    return [row(r) for r in con.execute(q, args)]


def due(con, now=None):
    """Accepted entries past their review date - nobody is vouching for these."""
    now = stamp(now or utc_now())
    return [row(r) for r in con.execute(
        "SELECT * FROM knowledge WHERE status='accepted' AND review_by < ? "
        "ORDER BY review_by", (now,))]


def brief(con, dept, max_chars=BRIEF_MAX_CHARS, now=None):
    """The accepted entries an agent in `dept` is handed, as prompt text.

    Its own department plus `all`. Decisions first - they are the ones an
    agent must not argue with - then playbooks, lessons, facts, questions.
    An entry past review is still shown, marked, because it was accepted and
    nobody has said otherwise; hiding it would lose it silently.
    """
    if dept not in DEPARTMENTS:
        raise Refused(f"unknown department {dept!r}")
    order = {k: i for i, k in enumerate(("decision", "playbook", "lesson", "fact", "question"))}
    rows = [e for e in entries(con, status="accepted")
            if e["dept"] in (dept, "all")]
    rows.sort(key=lambda e: (order[e["kind"]], e["id"]))
    stale_before = stamp(now or utc_now())

    out, used, dropped = [], 0, 0
    for e in rows:
        tag = f"{e['kind']}, {e['confidence']}"
        if e["review_by"] and e["review_by"] < stale_before:
            tag += ", past review"
        line = f"- #{e['id']} [{tag}] {e['title']}: {e['body']}"
        if e["evidence"]:
            line += f" (evidence: {e['evidence']})"
        if used + len(line) + 1 > max_chars:
            dropped += 1
            continue
        out.append(line)
        used += len(line) + 1
    if dropped:
        out.append(f"- ({dropped} more accepted entries did not fit; "
                   f"python3 scripts/knowledge.py list --dept {dept})")
    return "\n".join(out)


# The starting entries: things this project has already paid to learn about
# the Etsy side. They go in as `proposed` like anything else - the owner
# accepts them in the Town Hall, so even the seed passes through the one gate.
# Each points at the code or file that owns the detail rather than restating it.
SEEDS = [
    dict(dept="etsy", kind="decision", confidence="observed", source="owner",
         title="Totes only until another market is proven",
         body="Scout proposes tote bags and nothing else. Changing the focus is "
              "the owner's call, made with scout-ideas.py focus, not an agent's.",
         evidence="agents/scout/state/focus.txt; scout-ideas.py refuses off-focus intake"),
    dict(dept="etsy", kind="lesson", confidence="measured", source="owner",
         title="All-over art must be flat colour, not a photograph",
         body="A photo-like image fails the all-over check and prints muddy across "
              "a whole tote. Ask for a flat, limited-palette pattern; the check "
              "decides, not the eye.",
         evidence="python3 scripts/knockout.py <image> --check --all-over (real wave design passed)"),
    dict(dept="etsy", kind="lesson", confidence="observed", source="owner",
         title="Tote print sheets fold: front and back are separate faces",
         body="The all-over tote print area is one folded sheet with a strip that "
              "becomes the bottom of the bag. Art is laid per face by "
              "emily-printify.py layout --folded; never place it by hand.",
         evidence="catalogue entry print_sizes + folded, written by emily-printify.py layout"),
    dict(dept="etsy", kind="lesson", confidence="observed", source="owner",
         title="Search phrases that name patterns are not tote demand",
         body="Phrases like 'tote bag pattern' are $4 sewing patterns. The market "
              "scan labels them and the evidence gate refuses them; trust the "
              "label over the search volume.",
         evidence="market-scan.py verdicts(); scout-ideas.py measured()"),
    dict(dept="etsy", kind="fact", confidence="observed", source="owner",
         title="Designer and bag brand names are blocked",
         body="Anything naming a fashion or bag brand is refused before drafting. "
              "The list lives in ip-check.py; add a name there, not in a prompt.",
         evidence="scripts/ip-check.py BLOCKED groups"),
    dict(dept="etsy", kind="playbook", confidence="observed", source="owner",
         title="Before publishing any listing",
         body="Attach the Printify production partner on the listing itself, "
              "answer 'I did' for who made it, and read the description once. "
              "Nothing here can publish; the owner presses Publish.",
         evidence="BACKBONE.md 'Publishing a listing on Etsy'"),
    dict(dept="etsy", kind="fact", confidence="observed", source="owner",
         title="Emily's daily build cap counts the Eastern day",
         body="The cap counts builds started since Eastern midnight, all statuses. "
              "Raise it for one day with emily-cap.py today, never by editing code.",
         evidence="emily-new-build.py daily_cap(); scripts/emily-cap.py"),
    dict(dept="etsy", kind="question", confidence="believed", source="owner",
         title="Can a 'personalized' listing actually be personalized?",
         body="A listing that promises a name or custom text needs a way to take "
              "the buyer's text and make a one-off print. Nothing here does that "
              "yet, so such a listing may promise what cannot be delivered.",
         evidence=""),
    dict(dept="all", kind="decision", confidence="observed", source="owner",
         title="AI spend: at most $1 a day until there is a return",
         body="The agents' AI costs stay under $1 a UTC day (the day turns over "
              "at 8pm Eastern) until the store earns. budget.py reads the real "
              "spend against it; the GM stays asleep once today's is gone.",
         evidence="tasks/limits.json daily_ai_budget; python3 scripts/budget.py"),
    dict(dept="all", kind="decision", confidence="observed", source="owner",
         title="The GM proposes; it does not assign (first month)",
         body="For the first month the GM writes proposals the owner approves or "
              "declines in the Town Hall. It creates no tasks and changes no "
              "agent's work on its own.",
         evidence="decided 2026-09-25"),
]


def seed(con):
    """Propose every seed not already present. Returns (added, skipped)."""
    added = skipped = 0
    for s in SEEDS:
        try:
            add(con, **s)
            added += 1
        except Refused:
            skipped += 1
    return added, skipped


def eastern(ts):
    t = et_time.to_eastern(ts) if ts else None
    return t.strftime("%Y-%m-%d") if t else (ts or "")


def show(e):
    head = f"#{e['id']:<4} {e['status']:<9} {e['dept']:<8} {e['kind']:<9} {e['title']}"
    tail = [f"       {e['body']}"]
    if e["evidence"]:
        tail.append(f"       evidence: {e['evidence']}")
    meta = f"       {e['confidence']}, from {e['source']}, {eastern(e['created_at'])}"
    if e["status"] == "accepted" and e["review_by"]:
        meta += f", review by {eastern(e['review_by'])}"
    if e["status"] == "retired" and e["why_retired"]:
        meta += f", retired: {e['why_retired']}"
    return "\n".join([head] + tail + [meta])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add")
    p.add_argument("--dept", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--evidence", default="")
    p.add_argument("--confidence", default="believed")
    p.add_argument("--source", default="owner")

    for name in ("accept", "renew"):
        sub.add_parser(name).add_argument("id", type=int)
    p = sub.add_parser("retire")
    p.add_argument("id", type=int)
    p.add_argument("--why", required=True)

    p = sub.add_parser("list")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--dept", choices=list(DEPARTMENTS))
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("brief")
    p.add_argument("--dept", required=True, choices=list(DEPARTMENTS))

    sub.add_parser("due")
    sub.add_parser("seed")

    a = ap.parse_args(argv)
    con = connect()
    try:
        if a.cmd == "add":
            new_id = add(con, a.dept, a.kind, a.title, a.body, a.evidence,
                         a.confidence, a.source)
            print(f"proposed #{new_id} - the owner accepts it in the Town Hall, "
                  f"or: python3 scripts/knowledge.py accept {new_id}")
        elif a.cmd == "accept":
            e = accept(con, a.id)
            print(f"accepted #{e['id']}, review by {eastern(e['review_by'])}")
        elif a.cmd == "renew":
            e = renew(con, a.id)
            print(f"renewed #{e['id']}, review by {eastern(e['review_by'])}")
        elif a.cmd == "retire":
            retire(con, a.id, a.why)
            print(f"retired #{a.id}")
        elif a.cmd == "list":
            rows = entries(con, a.status, a.dept)
            if a.json:
                print(json.dumps({"entries": rows, "due": [e["id"] for e in due(con)],
                                  "departments": list(DEPARTMENTS),
                                  "kinds": KINDS, "confidence": list(CONFIDENCE)},
                                 indent=2))
            elif not rows:
                print("no entries" + (f" ({a.status})" if a.status else ""))
            else:
                print("\n\n".join(show(e) for e in rows))
        elif a.cmd == "brief":
            print(brief(con, a.dept) or f"(nothing accepted for {a.dept} yet)")
        elif a.cmd == "due":
            rows = due(con)
            print("\n\n".join(show(e) for e in rows) if rows
                  else "nothing past review")
        elif a.cmd == "seed":
            added, skipped = seed(con)
            print(f"proposed {added} starting entries ({skipped} already there) - "
                  f"accept or retire them in the Town Hall")
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
