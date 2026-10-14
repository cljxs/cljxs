#!/usr/bin/env python3
"""
agentroom.py — the Agent Room: an idea in, a reviewed build plan out.

    agentroom.py new --title="Dinner planner" --idea="..." [--dry]
    agentroom.py runner                 # the durable loop (agentroom-runner.service)
    agentroom.py step <id>              # one step of one run, by hand
    agentroom.py approve <id>           # the owner's gate: save the plan to its project
    agentroom.py sendback <id> --note="..."   # the planner revises with the owner's note
    agentroom.py stop <id>
    agentroom.py deck                   # everything the page shows, as JSON
    agentroom.py show <id>              # one run: its messages and its plan, as JSON
    agentroom.py check                  # free: key, models, prices, worst cases

WHAT IT DOES (owner, 2026-10-14, after screenshots of a tool called CoreOps).
The owner types an idea. The PLANNER (Kimi) writes a phased plan; the
REVIEWERS (DeepSeek and Codex) critique it; the planner revises it; then it
STOPS and waits for the owner to approve. Building the phases is stage 2 and
is not here yet. Who plays which part is agentroom/roles.json.

WHAT CODE DECIDES, NOT THE MODELS. A run is a state machine in this file and
its database; the models only write text. Code moves a run from state to
state, checks that a plan is a plan (every phase has something code can run
to prove it is done - "the AI said it is done" is not a check), decides when
the reviewers agreed enough to skip a revision, counts the money and stops it.

MONEY. Every call goes through ask(), which refuses BEFORE calling when:
  * the run has made max_calls_per_run calls (a loop is the dearest bug),
  * the call's worst case - its prompt plus max_tokens, at OpenRouter's
    published prices - would take the run past agentroom_run_max, or
  * it would take the day past agentroom_daily_ai, as OpenRouter itself
    counts the day (usage_daily on the Agent Room's own key, UTC day).
The Agent Room has ITS OWN OpenRouter key (AGENTROOM_OPENROUTER_KEY in
agentroom/state/credentials.env), never the agents' key: OpenRouter counts
spend per key, so on a shared key its runs would eat the agents' $1.50 and
budget.py would put Emily and the GM to sleep. A real run with the agents'
key is refused. The key's own daily limit, set on openrouter.ai, is the
hard stop; this file is the soft one.

DRY RUN. A practice run (--dry) calls no model and costs nothing: canned
replies go through the same parsers, checks and states, so the whole room
can be tried before a key exists.

DURABLE. The runner is a systemd service, not a browser tab. Every step can
be redone: a plan or review already saved is never asked for again, so a
restart in the middle of a run resumes it without paying twice.

NOTHING LEAVES THE SERVER. Approving writes the plan into its own folder,
BUILDS/<slug>/ (outside the ecosystem, default /root/builds), and commits it
there. Nothing here deploys, publishes, emails or touches the agents.

Standard library only.
"""

import argparse
import fcntl
import importlib.util
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent
LIMITS = ROOT / "tasks" / "limits.json"


def home():
    return Path(os.environ.get("AGENTROOM_HOME", ROOT / "agentroom"))


def builds():
    # Outside the ecosystem on purpose: nothing a build does can reach the
    # agents' folders, and `git pull` of the ecosystem never meets it.
    return Path(os.environ.get("AGENTROOM_BUILDS", ROOT.parent / "builds"))


def state_dir():
    return home() / "state"


KEY_NAME = "AGENTROOM_OPENROUTER_KEY"
OR_MODELS = "https://openrouter.ai/api/v1/models"

# Money defaults, used only when tasks/limits.json does not say. The owner's
# numbers (2026-10-14) are in limits.json; these match them.
LIMIT_DEFAULTS = {"agentroom_daily_ai": 3.0, "agentroom_run_max": 1.0}

WORKING = ("queued", "planning", "reviewing", "revising")
STATE_LABEL = {
    "queued": "waiting for the runner",
    "planning": "the planner is writing the plan",
    "reviewing": "the reviewers are reading the plan",
    "revising": "the planner is revising the plan",
    "awaiting_approval": "waiting for you to approve the plan",
    "approved": "plan approved - saved to its project folder",
    "stopped": "stopped",
    "failed": "stopped by a problem",
}
MAX_OPEN_RUNS = 3          # working at once - a fourth waits for one to finish
RUNNER_POLL = 3            # seconds between looks at the queue
RUNNER_FRESH = 30          # a heartbeat older than this is a dead runner
BUDGET_CACHE = 60          # seconds a read of OpenRouter's day count is reused by the page
CHARS_PER_TOKEN = 3        # pessimistic: real text runs nearer 4, so worst cases read high
CALL_TIMEOUT = 300         # seconds for one model reply; a planner thinking at length is slow
RETRY_WAITS = (5, 20)      # between tries of a call that failed on the network or a 5xx

TITLE_MAX, IDEA_MIN, IDEA_MAX, NOTE_MAX = 80, 20, 8000, 2000
MAX_PHASES = 15


class Blocked(Exception):
    """The run may not continue: money, the call cap, a missing model. Said
    to the owner in the room, and the run stops as failed."""


class Stopped(Exception):
    """The owner pressed Stop."""


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mods = {}


def mod(name):
    """emily-assets owns the credentials.env parser, the OpenRouter URL and
    what a call cost; preflight owns the key endpoint and the agents' key;
    budget owns what counts as a daily hard stop. One fact, one place."""
    files = {"ea": "emily-assets.py", "preflight": "preflight.py", "budget": "budget.py"}
    if name not in _mods:
        _mods[name] = _load(f"agentroom_{name}", files[name])
    return _mods[name]


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_seconds(stamp):
    try:
        t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def limits(path=None):
    try:
        d = json.loads(Path(path or LIMITS).read_text())
    except Exception:
        d = {}
    out = {}
    for k, dflt in LIMIT_DEFAULTS.items():
        v = d.get(k, dflt)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
        out[k] = float(v) if ok else dflt
    return out


def roles(path=None):
    """roles.json, checked. A setting that is wrong is said here, by name,
    rather than discovered as a KeyError in the middle of a paid run."""
    d = json.loads(Path(path or home() / "roles.json").read_text())

    def part(x, where):
        if not isinstance(x, dict) or not isinstance(x.get("model"), str) or "/" not in x["model"]:
            raise ValueError(f"roles.json: {where} needs a model like 'vendor/model'")
        mt = x.get("max_tokens", 4000)
        if not isinstance(mt, int) or isinstance(mt, bool) or not 500 <= mt <= 64000:
            raise ValueError(f"roles.json: {where} max_tokens must be a whole number 500-64000")
        return {"name": str(x.get("name") or x["model"].split("/")[-1]), "model": x["model"],
                "max_tokens": mt}

    planner = part(d.get("planner"), "planner")
    revs = d.get("reviewers") or []
    if not isinstance(revs, list) or not revs:
        raise ValueError("roles.json: reviewers must be a list of at least one")
    reviewers = [part(r, f"reviewer {i + 1}") for i, r in enumerate(revs)]
    if len({r["name"] for r in reviewers}) != len(reviewers):
        raise ValueError("roles.json: two reviewers have the same name")
    rounds = d.get("review_rounds", 1)
    calls = d.get("max_calls_per_run", 12)
    for k, v, lo, hi in (("review_rounds", rounds, 1, 3), ("max_calls_per_run", calls, 3, 60)):
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"roles.json: {k} must be a whole number {lo}-{hi}")
    return {"planner": planner, "reviewers": reviewers, "review_rounds": rounds,
            "max_calls_per_run": calls}


def room_key():
    return (mod("ea").read_env_file(state_dir() / "credentials.env").get(KEY_NAME) or "").strip()


def key_problem(key=None):
    """Why a real run cannot use the key, or None. Never says the key."""
    key = room_key() if key is None else key
    if not key:
        return (f"no Agent Room key yet - make a second key on openrouter.ai with a daily "
                f"limit, then: python3 scripts/set-credential.py agentroom {KEY_NAME}")
    try:
        agents = mod("preflight").openrouter_key()
    except Exception:
        agents = ""
    if agents and key == agents:
        return ("the Agent Room key is the same key the agents use - its spending would "
                "come out of their $1.50 a day. Make a second key on openrouter.ai.")
    return None


# --------------------------------------------------------------------------
# the model catalogue: does each model exist, and what does it cost
# --------------------------------------------------------------------------

def catalogue(fetch=None, max_age=6 * 3600):
    """{model id: (dollars per prompt token, dollars per output token)} from
    OpenRouter's public list. Cached for a few hours; no key needed, free.

    A price that is missing, unreadable or negative (openrouter/auto
    publishes "-1") is None - and a model without both prices cannot be used
    by a real run, because its worst case cannot be known.
    """
    cache = state_dir() / "models.json"
    data = None
    try:
        if time.time() - cache.stat().st_mtime < max_age:
            data = json.loads(cache.read_text())
    except (OSError, ValueError):
        data = None
    if data is None:
        if fetch is None:
            with urllib.request.urlopen(OR_MODELS, timeout=30) as r:
                data = json.loads(r.read())
        else:
            data = fetch()
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        except OSError:
            pass
    return prices_from(data)


def prices_from(data):
    def price(p, k):
        try:
            v = float((p or {}).get(k))
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None

    out = {}
    for m in (data or {}).get("data") or []:
        mid = m.get("id")
        if isinstance(mid, str):
            pr = m.get("pricing") or {}
            out[mid] = (price(pr, "prompt"), price(pr, "completion"))
    return out


def tokens_in(text):
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def worst_case(prices, model, prompt_text, max_tokens):
    """The most this call can cost, in dollars: every character of the prompt
    as a third of a token, and the whole max_tokens written. None when the
    model's prices are not known - unknown is not free."""
    p_in, p_out = prices.get(model, (None, None))
    if p_in is None or p_out is None:
        return None
    return tokens_in(prompt_text) * p_in + max_tokens * p_out


# --------------------------------------------------------------------------
# the database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  idea TEXT NOT NULL,
  dry INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued',
  round INTEGER NOT NULL DEFAULT 1,
  calls INTEGER NOT NULL DEFAULT 0,
  spent REAL NOT NULL DEFAULT 0,
  spent_known INTEGER NOT NULL DEFAULT 1,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  error TEXT,
  project_dir TEXT,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL,
  approved_utc TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  who TEXT NOT NULL,
  kind TEXT NOT NULL,
  model TEXT,
  text TEXT NOT NULL,
  cost REAL,
  cost_known INTEGER
);
CREATE INDEX IF NOT EXISTS messages_run ON messages(run_id, id);
CREATE TABLE IF NOT EXISTS plans (
  run_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  json TEXT NOT NULL,
  created_utc TEXT NOT NULL,
  PRIMARY KEY (run_id, version)
);
CREATE TABLE IF NOT EXISTS reviews (
  run_id INTEGER NOT NULL,
  round INTEGER NOT NULL,
  reviewer TEXT NOT NULL,
  verdict TEXT,
  json TEXT,
  text TEXT NOT NULL,
  PRIMARY KEY (run_id, round, reviewer)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path=None):
    path = Path(path or state_dir() / "room.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def get_run(conn, rid):
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def set_run(conn, rid, **fields):
    fields["updated_utc"] = now_utc()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), rid))


def say(conn, rid, who, kind, text, model=None, cost=None, cost_known=None):
    conn.execute("INSERT INTO messages (run_id, ts, who, kind, model, text, cost, cost_known) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (rid, now_utc(), who, kind, model, text, cost,
                  None if cost_known is None else int(cost_known)))


def latest_plan(conn, rid):
    row = conn.execute("SELECT version, json FROM plans WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                       (rid,)).fetchone()
    return (row["version"], json.loads(row["json"])) if row else (0, None)


def meta_get(conn, k):
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else None


def meta_set(conn, k, v):
    conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                 (k, v))


# --------------------------------------------------------------------------
# reading what the models write
# --------------------------------------------------------------------------

def extract_json(text):
    """The JSON object in a model's reply, or None.

    Models wrap JSON in ```json fences, or put a sentence before it, or both.
    Tried in order: the whole reply, a fenced block, then the span from the
    first { to the last }.
    """
    if not isinstance(text, str):
        return None
    cands = [text.strip()]
    cands += re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        cands.append(text[a:b + 1])
    for c in cands:
        try:
            v = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict):
            return v
    return None


def _strs(v, limit=40):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()][:limit]


def check_plan(obj):
    """(plan, problems). The plan is normalised; problems are sentences a
    model can act on, and an empty list means code accepts the plan.

    THE RULE THAT MATTERS: every phase says how code will know it is done
    (`done_when`). A phase without one can only ever be "done" because a
    model said so, and a model will say so about work it did not do.
    """
    if not isinstance(obj, dict):
        return None, ["the reply was not a JSON object"]
    problems = []
    phases_in = obj.get("phases")
    if not isinstance(phases_in, list) or not phases_in:
        return None, ["there is no 'phases' list"]
    if len(phases_in) > MAX_PHASES:
        problems.append(f"{len(phases_in)} phases is too many - merge them into at most {MAX_PHASES}")
    phases, seen = [], set()
    for i, p in enumerate(phases_in[:MAX_PHASES]):
        if not isinstance(p, dict):
            problems.append(f"phase {i + 1} is not an object")
            continue
        pid = str(p.get("id") or i + 1).strip()[:8]
        title = str(p.get("title") or "").strip()[:120]
        if pid in seen:
            problems.append(f"two phases have the id '{pid}'")
        seen.add(pid)
        if not title:
            problems.append(f"phase {pid} has no title")
        done_when = _strs(p.get("done_when"))
        if not done_when:
            problems.append(f"phase {pid} has no done_when - say what code can run to prove it works")
        phases.append({"id": pid, "title": title, "goal": str(p.get("goal") or "").strip()[:600],
                       "tasks": _strs(p.get("tasks")), "done_when": done_when})
    plan = {"summary": str(obj.get("summary") or "").strip()[:2000],
            "stack": str(obj.get("stack") or "").strip()[:600],
            "phases": phases,
            "risks": _strs(obj.get("risks"), 20),
            "questions": _strs(obj.get("questions"), 20),
            "changes": _strs(obj.get("changes"), 30)}
    if not plan["summary"]:
        problems.append("there is no 'summary'")
    return plan, problems


SEVERITIES = ("high", "medium", "low")


def read_review(obj):
    """A reviewer's reply, normalised, or None if it is not one."""
    if not isinstance(obj, dict):
        return None
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("approve", "changes"):
        return None
    concerns = []
    for c in obj.get("concerns") or []:
        if not isinstance(c, dict) or not str(c.get("text") or "").strip():
            continue
        sev = str(c.get("severity") or "").strip().lower()
        concerns.append({"severity": sev if sev in SEVERITIES else "medium",
                         "phase": str(c.get("phase") or "all").strip()[:16],
                         "text": str(c.get("text")).strip()[:800],
                         "suggestion": str(c.get("suggestion") or "").strip()[:800]})
    return {"verdict": verdict, "summary": str(obj.get("summary") or "").strip()[:1200],
            "concerns": concerns[:30]}


def needs_revision(reviews):
    """Whether the planner must revise. No, only when every reviewer was read
    and approved with nothing above a low concern - an unreadable review is
    not an approval."""
    for r in reviews:
        if r is None or r["verdict"] != "approve":
            return True
        if any(c["severity"] != "low" for c in r["concerns"]):
            return True
    return False


def plan_markdown(plan, title, idea):
    out = [f"# {title}", "", "## The idea", "", idea.strip(), "", "## Summary", "", plan["summary"]]
    if plan.get("stack"):
        out += ["", f"**Stack:** {plan['stack']}"]
    out += ["", "## Phases"]
    for p in plan["phases"]:
        out += ["", f"### Phase {p['id']} - {p['title']}"]
        if p["goal"]:
            out += ["", p["goal"]]
        if p["tasks"]:
            out += [""] + [f"- {t}" for t in p["tasks"]]
        out += ["", "Done when:"] + [f"- [ ] {d}" for d in p["done_when"]]
    for head, key in (("Risks", "risks"), ("Open questions", "questions")):
        if plan.get(key):
            out += ["", f"## {head}", ""] + [f"- {x}" for x in plan[key]]
    return "\n".join(out) + "\n"


def review_lines(name, rev, raw):
    if rev is None:
        return f"{name} (could not be read as a review):\n{raw[:3000]}"
    lines = [f"{name}: {rev['verdict']}. {rev['summary']}"]
    for c in rev["concerns"]:
        lines.append(f"- [{c['severity']}] phase {c['phase']}: {c['text']}"
                     + (f" Suggestion: {c['suggestion']}" if c["suggestion"] else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# what the models are told
# --------------------------------------------------------------------------

PLAN_SHAPE = """{
  "summary": "what will be built, for whom, in a short paragraph",
  "stack": "languages, frameworks, database",
  "phases": [
    {"id": "1", "title": "Foundation", "goal": "...",
     "tasks": ["...", "..."],
     "done_when": ["a check code can run, e.g. 'pytest passes', 'GET /health returns 200'"]}
  ],
  "risks": ["..."],
  "questions": ["things only the owner can answer"]
}"""

PLANNER_SYSTEM = f"""You are the Planner in an Agent Room: several AIs that turn one person's
software idea into working software. A coding agent will build your plan one
phase at a time in a fresh git repository, and code - not an AI - decides when
a phase is done, by running that phase's done_when checks.

Write a phased build plan:
- 3 to 12 phases, in build order. Phase 1 sets up the project, its test runner
  and a test that passes.
- Each phase small enough to build and test in one sitting.
- Every phase has done_when: checks a program can run (a test command, an HTTP
  request and its expected answer, a file that must exist). Never "looks good".
- Nothing that leaves the computer without the owner: no deploying, payments,
  sending email or posting. Say where the owner must decide, in questions.

Reply with ONLY one JSON object, no other text, in this shape:
{PLAN_SHAPE}"""

REVIEWER_SYSTEM = """You are a Reviewer in an Agent Room. A planner wrote a phased build plan for
a coding agent; code will judge each phase only by running its done_when checks.

Find what is wrong with the plan: missing pieces, phases too big to build in
one sitting, done_when checks that a program cannot actually run, wrong order,
security and privacy problems, anything that would cost the owner money or
reach outside the computer. Do not rewrite the plan.

Reply with ONLY one JSON object, no other text:
{"verdict": "approve" or "changes",
 "summary": "one or two sentences",
 "concerns": [{"severity": "high" | "medium" | "low", "phase": "<phase id or all>",
               "text": "what is wrong", "suggestion": "what to do instead"}]}"""


def planner_prompt(title, idea):
    return f"Project: {title}\n\nThe owner's idea:\n{idea}"


def revise_prompt(title, idea, plan, notes, from_owner):
    who = ("The owner read your plan and sent it back with this note"
           if from_owner else "The reviewers' notes on your plan")
    return (f"Project: {title}\n\nThe owner's idea:\n{idea}\n\nYour current plan:\n"
            f"{json.dumps(plan, indent=1)}\n\n{who}:\n{notes}\n\n"
            "Revise the plan. Reply with the WHOLE revised plan as one JSON object in the same "
            "shape, plus \"changes\": a list saying what you changed and why, and which notes "
            "you did not take and why.")


def review_prompt(title, idea, plan):
    return (f"Project: {title}\n\nThe owner's idea:\n{idea}\n\nThe plan:\n"
            f"{json.dumps(plan, indent=1)}")


# --------------------------------------------------------------------------
# talking to a model
# --------------------------------------------------------------------------

def reply_text(data):
    """(text, finish_reason, usage) from an OpenRouter chat completion.

    BELIEVED, not verified: no real Agent Room reply has been captured yet
    (no key in the sandbox where this was written). capture() saves the
    first real reply per model to agentroom/state/captured/ so it can become
    a fixture. The shape read here is the one test_shop.py uses for
    OpenRouter's replies elsewhere in this repo.
    """
    choice = ((data or {}).get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return (content or "").strip(), choice.get("finish_reason"), (data or {}).get("usage") or {}


def capture(model, data):
    """Keep the first real reply from each model, to make a fixture of. A
    reply holds no secret; the text is cut short to keep the file small."""
    d = state_dir() / "captured"
    f = d / (re.sub(r"[^a-z0-9.-]+", "_", model.lower()) + ".json")
    if f.exists():
        return
    try:
        keep = json.loads(json.dumps(data))
        for c in keep.get("choices") or []:
            m = c.get("message") or {}
            if isinstance(m.get("content"), str):
                m["content"] = m["content"][:1500]
            for k in ("reasoning", "reasoning_details"):
                m.pop(k, None)
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(keep, indent=1))
    except Exception:
        pass


def openrouter_call(key, model, system, user, max_tokens, post=None, wait=time.sleep):
    """One chat completion. Retries a network failure, a 429 or a 5xx twice;
    never retries a 4xx such as 402 (no credit) - asking again cannot help
    and each try is priced."""
    ea = mod("ea")
    body = json.dumps({"model": model, "max_tokens": max_tokens, "usage": {"include": True},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    post = post or (lambda req: ea.post(req, timeout=CALL_TIMEOUT))
    last = None
    for attempt in range(len(RETRY_WAITS) + 1):
        req = urllib.request.Request(ea.OR_URL, data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/cljxs/cljxs", "X-Title": "agentroom"})
        try:
            return post(req)
        except RuntimeError as e:
            last = e
            m = re.match(r"HTTP (\d{3})", str(e))
            code = int(m.group(1)) if m else 0
            if code and code != 429 and code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = RuntimeError(f"network: {e}")
        if attempt < len(RETRY_WAITS):
            wait(RETRY_WAITS[attempt])
    raise last


class Room:
    """What a step needs from the outside world, so a test can replace it:
    the key, the prices, OpenRouter's day count, and the call itself."""

    def __init__(self, key=None, prices=None, day_spent=None, call=None):
        self._key, self._prices, self._day, self._call = key, prices, day_spent, call

    def key(self):
        return room_key() if self._key is None else self._key

    def prices(self):
        if self._prices is None:
            self._prices = catalogue()
        return self._prices

    def day_spent(self):
        """Today's spend on the Agent Room's key, as OpenRouter counts it."""
        if self._day is not None:
            return self._day()
        d = mod("preflight").key_status(self.key())
        v = d.get("usage_daily")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise RuntimeError("OpenRouter did not report today's spend on this key")
        return float(v)

    def call(self, model, system, user, max_tokens):
        if self._call is not None:
            return self._call(model, system, user, max_tokens)
        data = openrouter_call(self.key(), model, system, user, max_tokens)
        capture(model, data)
        return data


def money_check(run, part, worst, day_spent, caps):
    """(ok, why) for one call. Pure, so the rules are tested on their own."""
    if worst is None:
        return False, (f"{part['model']} has no published price on OpenRouter - "
                       "its worst case cannot be known, so it is not used")
    if run["spent"] + worst > caps["agentroom_run_max"] + 1e-9:
        return False, (f"this run has spent ${run['spent']:.2f}; the next reply could cost up "
                       f"to ${worst:.2f}, past the ${caps['agentroom_run_max']:.2f} a run may spend")
    if day_spent is None:
        return False, "OpenRouter would not say what was spent today - not spending blind"
    if day_spent + worst > caps["agentroom_daily_ai"] + 1e-9:
        return False, (f"${day_spent:.2f} of today's ${caps['agentroom_daily_ai']:.2f} is spent; "
                       f"the next reply could cost up to ${worst:.2f}. It starts again at 8pm "
                       "Eastern (midnight UTC).")
    return True, None


def ask(conn, room, rid, part, who, system, user):
    """One model call for a run, with every check before it and the cost
    recorded after. Returns the reply text."""
    run = get_run(conn, rid)
    if run["stop_requested"]:
        raise Stopped()
    cfg = roles()
    if run["calls"] >= cfg["max_calls_per_run"]:
        raise Blocked(f"this run has made {run['calls']} model calls, the most one run may make "
                      "(max_calls_per_run in agentroom/roles.json) - stopped so a loop cannot run up a bill")

    if run["dry"]:
        set_run(conn, rid, calls=run["calls"] + 1)
        text = dry_reply(system, user, part)
        say(conn, rid, who, "agent", text, model=f"{part['model']} (practice)", cost=0.0, cost_known=True)
        return text

    problem = key_problem(room.key())
    if problem:
        raise Blocked(problem)
    prices = room.prices()
    if part["model"] not in prices:
        raise Blocked(f"{part['model']} is not a model OpenRouter has - change it in agentroom/roles.json")
    worst = worst_case(prices, part["model"], system + user, part["max_tokens"])
    try:
        day = room.day_spent()
    except Exception:
        day = None
    ok, why = money_check(run, part, worst, day, limits())
    if not ok:
        raise Blocked(why)

    # Counted BEFORE the call: a call that dies halfway may still be billed,
    # and the cap is on attempts, not on successes.
    set_run(conn, rid, calls=run["calls"] + 1)
    try:
        data = room.call(part["model"], system, user, part["max_tokens"])
    except Exception as e:
        raise Blocked(f"{who} could not be reached: {str(e)[:300]}")
    text, finish, usage = reply_text(data)
    cost = mod("ea").cost_of(usage)
    known = cost is not None
    # Unknown is counted as the worst case, never as zero: a zero would let
    # a run with no cost reports spend without limit.
    spent = cost if known else worst
    run = get_run(conn, rid)
    set_run(conn, rid, spent=round(run["spent"] + spent, 6),
            spent_known=int(run["spent_known"] and known))
    if not text:
        say(conn, rid, who, "error", f"(an empty reply - finish reason: {finish or 'not given'})",
            model=part["model"], cost=spent, cost_known=known)
        if finish == "length":
            raise Blocked(f"{who} ran out of room before answering - raise its max_tokens "
                          "in agentroom/roles.json")
        raise Blocked(f"{who} sent an empty reply")
    say(conn, rid, who, "agent", text, model=part["model"], cost=spent, cost_known=known)
    if get_run(conn, rid)["stop_requested"]:
        raise Stopped()
    return text


# --------------------------------------------------------------------------
# practice replies (dry run): no model, no money, the same parsers
# --------------------------------------------------------------------------

def dry_reply(system, user, part):
    if system == REVIEWER_SYSTEM:
        # One reviewer asks for changes and one approves, so a practice run
        # goes through every state a real one can.
        if sum(map(ord, part["name"])) % 2:
            body = {"verdict": "approve", "summary": "(practice) The phases are small and each has a test.",
                    "concerns": [{"severity": "low", "phase": "all", "text": "(practice) Name the database early.",
                                  "suggestion": "Say SQLite in phase 1."}]}
        else:
            body = {"verdict": "changes", "summary": "(practice) Sign-in comes too late.",
                    "concerns": [{"severity": "medium", "phase": "3", "text": "(practice) Accounts are needed by phase 2.",
                                  "suggestion": "Move sign-in into phase 2."}]}
        return "Here is my review.\n```json\n" + json.dumps(body, indent=1) + "\n```"
    title = re.search(r"Project: (.*)", user)
    title = title.group(1).strip() if title else "the project"
    revised = "Your current plan:" in user
    phases = [
        {"id": "1", "title": "Foundation", "goal": f"An empty {title} app with a test runner.",
         "tasks": ["Create the project", "Add a test runner", "Add a health check"],
         "done_when": ["the test command passes", "GET /health returns 200"]},
        {"id": "2", "title": "Accounts" if revised else "Core screens", "goal": "The first thing a person can do.",
         "tasks": ["Model the data", "Build the first screen"],
         "done_when": ["the test command passes", "a test creates and reads one record"]},
        {"id": "3", "title": "Core screens" if revised else "Accounts", "goal": "Everything else in the idea.",
         "tasks": ["Build the remaining screens"],
         "done_when": ["the test command passes"]},
    ]
    body = {"summary": f"(practice) A plan for {title}, made without any AI - nothing was spent.",
            "stack": "Python, SQLite", "phases": phases,
            "risks": ["(practice) none - this is a dry run"],
            "questions": ["(practice) Who will use it first?"]}
    if revised:
        body["changes"] = ["(practice) Moved accounts earlier, as a reviewer asked."]
    return "```json\n" + json.dumps(body, indent=1) + "\n```"


# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------

def get_plan(conn, room, rid, who, system, user):
    """A plan code accepts, asking once more with the problems named if the
    first reply is not one. Two bad replies end the run."""
    part = roles()["planner"]
    text = ask(conn, room, rid, part, who, system, user)
    plan, problems = check_plan(extract_json(text))
    if problems:
        say(conn, rid, "Code", "system", "The plan was sent back to the planner: " + "; ".join(problems))
        user = (f"{user}\n\nYour last reply was not accepted:\n- " + "\n- ".join(problems)
                + "\nReply again with only the JSON object.")
        text = ask(conn, room, rid, part, who, system, user)
        plan, problems = check_plan(extract_json(text))
        if problems:
            raise Blocked("the planner's plan failed code's checks twice: " + "; ".join(problems))
    return plan


def save_plan(conn, rid, plan, then_state, **fields):
    v, _ = latest_plan(conn, rid)
    n = len(plan["phases"])
    tests = sum(len(p["done_when"]) for p in plan["phases"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO plans (run_id, version, json, created_utc) VALUES (?, ?, ?, ?)",
                     (rid, v + 1, json.dumps(plan), now_utc()))
        say(conn, rid, "Code", "system",
            f"Code checked plan v{v + 1}: {n} phases, each with a check code can run "
            f"({tests} in all).")
        set_run(conn, rid, state=then_state, **fields)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def label(part, role):
    return f"{part['name']} · {role}"


def step(conn, rid, room=None):
    """Do the work of the run's current state and move it on. Returns the new
    state. Safe to call again after a crash at any point."""
    room = room or Room()
    run = get_run(conn, rid)
    if run is None:
        raise ValueError(f"no run {rid}")
    if run["state"] not in WORKING:
        return run["state"]
    if run["stop_requested"]:
        set_run(conn, rid, state="stopped")
        say(conn, rid, "Code", "system", "Stopped, as you asked.")
        return "stopped"
    cfg = roles()
    try:
        if run["state"] in ("queued", "planning"):
            if run["state"] == "queued":
                set_run(conn, rid, state="planning")
            if latest_plan(conn, rid)[1] is None:
                plan = get_plan(conn, room, rid, label(cfg["planner"], "Planner"),
                                PLANNER_SYSTEM, planner_prompt(run["title"], run["idea"]))
                save_plan(conn, rid, plan, "reviewing")
            else:
                set_run(conn, rid, state="reviewing")

        elif run["state"] == "reviewing":
            v, plan = latest_plan(conn, rid)
            for part in cfg["reviewers"]:
                done = conn.execute("SELECT 1 FROM reviews WHERE run_id = ? AND round = ? AND reviewer = ?",
                                    (rid, run["round"], part["name"])).fetchone()
                if done:
                    continue        # a restart never pays for the same review twice
                text = ask(conn, room, rid, part, label(part, "Reviewer"), REVIEWER_SYSTEM,
                           review_prompt(run["title"], run["idea"], plan))
                rev = read_review(extract_json(text))
                conn.execute("INSERT INTO reviews (run_id, round, reviewer, verdict, json, text) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (rid, run["round"], part["name"], rev and rev["verdict"],
                              rev and json.dumps(rev), text))
            revs = [r for r in conn.execute("SELECT * FROM reviews WHERE run_id = ? AND round = ?",
                                            (rid, run["round"]))]
            parsed = [json.loads(r["json"]) if r["json"] else None for r in revs]
            if needs_revision(parsed):
                say(conn, rid, "Code", "system",
                    f"Round {run['round']}: the reviewers asked for changes - back to the planner.")
                set_run(conn, rid, state="revising")
            else:
                say(conn, rid, "Code", "system",
                    "Every reviewer approved the plan with nothing above a low concern - no revision needed.")
                set_run(conn, rid, state="awaiting_approval")

        elif run["state"] == "revising":
            v, plan = latest_plan(conn, rid)
            owner_note = run["note"]
            if owner_note:
                notes = owner_note
            else:
                revs = conn.execute("SELECT * FROM reviews WHERE run_id = ? AND round = ? ORDER BY reviewer",
                                    (rid, run["round"])).fetchall()
                notes = "\n\n".join(review_lines(r["reviewer"], json.loads(r["json"]) if r["json"] else None,
                                                 r["text"]) for r in revs)
            new = get_plan(conn, room, rid, label(cfg["planner"], "Planner"), PLANNER_SYSTEM,
                           revise_prompt(run["title"], run["idea"], plan, notes, bool(owner_note)))
            if owner_note or run["round"] >= cfg["review_rounds"]:
                save_plan(conn, rid, new, "awaiting_approval", note=None)
            else:
                save_plan(conn, rid, new, "reviewing", round=run["round"] + 1)

        run = get_run(conn, rid)
        if run["state"] == "awaiting_approval":
            say(conn, rid, "Code", "system",
                f"The plan is ready for you. This run spent {money(run)}. Approve it, or send it back with a note.")
        return run["state"]

    except Stopped:
        set_run(conn, rid, state="stopped")
        say(conn, rid, "Code", "system", "Stopped, as you asked.")
        return "stopped"
    except Blocked as e:
        set_run(conn, rid, state="failed", error=str(e))
        say(conn, rid, "Code", "error", f"Stopped: {e}")
        return "failed"


def money(run):
    if run["dry"]:
        return "nothing (practice run)"
    s = f"${run['spent']:.2f}"
    return s if run["spent_known"] else s + " at most (OpenRouter did not report every cost)"


# --------------------------------------------------------------------------
# the owner's actions
# --------------------------------------------------------------------------

def slugify(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return s or "project"


def new_run(conn, title, idea, dry):
    title = re.sub(r"\s+", " ", title or "").strip()
    idea = (idea or "").strip()
    if not 1 <= len(title) <= TITLE_MAX:
        raise ValueError(f"the name must be 1-{TITLE_MAX} characters")
    if not IDEA_MIN <= len(idea) <= IDEA_MAX:
        raise ValueError(f"the idea must be {IDEA_MIN}-{IDEA_MAX} characters")
    open_n = conn.execute(f"SELECT COUNT(*) FROM runs WHERE state IN ({','.join('?' * len(WORKING))})",
                          WORKING).fetchone()[0]
    if open_n >= MAX_OPEN_RUNS:
        raise ValueError(f"{open_n} runs are already going - wait for one to finish")
    if not dry:
        problem = key_problem()
        if problem:
            raise ValueError(problem + " - or tick 'Practice run' to try it free")
    roles()                      # a broken roles.json is said now, not by the runner
    base = ("practice-" if dry else "") + slugify(title)
    slug, n = base, 2
    while (conn.execute("SELECT 1 FROM runs WHERE slug = ?", (slug,)).fetchone()
           or (builds() / slug).exists()):
        slug, n = f"{base}-{n}", n + 1
    t = now_utc()
    cur = conn.execute("INSERT INTO runs (slug, title, idea, dry, state, created_utc, updated_utc) "
                       "VALUES (?, ?, ?, ?, 'queued', ?, ?)", (slug, title, idea, int(dry), t, t))
    rid = cur.lastrowid
    say(conn, rid, "You", "owner", idea)
    say(conn, rid, "Code", "system",
        ("Practice run: no AI is called and nothing is spent." if dry else
         f"Real run: this run may spend up to ${limits()['agentroom_run_max']:.2f}.")
        + " The runner will pick it up in a few seconds.")
    return rid


def approve(conn, rid, git="git"):
    """The owner's gate. The plan is written into its own project folder and
    committed there; building it is stage 2."""
    run = get_run(conn, rid)
    if not run:
        raise ValueError(f"no run {rid}")
    if run["state"] != "awaiting_approval":
        raise ValueError(f"run {rid} is not waiting for approval (it is {run['state']})")
    v, plan = latest_plan(conn, rid)
    d = builds() / run["slug"]
    if d.exists() and any(d.iterdir()):
        raise ValueError(f"{d} already has files in it - not writing over them")
    d.mkdir(parents=True, exist_ok=True)
    (d / "PLAN.md").write_text(plan_markdown(plan, run["title"], run["idea"]))
    (d / "plan.json").write_text(json.dumps(plan, indent=1) + "\n")
    commit = None
    if shutil.which(git):
        def g(*args):
            return subprocess.run([git, "-C", str(d), "-c", "user.name=Agent Room",
                                   "-c", "user.email=agentroom@localhost", *args],
                                  capture_output=True, text=True, timeout=60)
        if g("init", "-q").returncode == 0 and g("add", "-A").returncode == 0:
            if g("commit", "-q", "-m", f"Plan v{v}, approved by the owner").returncode == 0:
                commit = g("rev-parse", "--short", "HEAD").stdout.strip() or None
    set_run(conn, rid, state="approved", approved_utc=now_utc(), project_dir=str(d))
    say(conn, rid, "Code", "system",
        f"You approved plan v{v}. Saved to {d}" + (f" (commit {commit})" if commit else "")
        + ". Building the phases is the next stage of the Agent Room and is not switched on yet.")
    return str(d)


def send_back(conn, rid, note):
    note = (note or "").strip()
    run = get_run(conn, rid)
    if not run:
        raise ValueError(f"no run {rid}")
    if run["state"] != "awaiting_approval":
        raise ValueError(f"run {rid} is not waiting for approval (it is {run['state']})")
    if not 3 <= len(note) <= NOTE_MAX:
        raise ValueError(f"the note must be 3-{NOTE_MAX} characters")
    say(conn, rid, "You", "owner", note)
    set_run(conn, rid, state="revising", note=note, error=None)


def stop(conn, rid):
    run = get_run(conn, rid)
    if not run:
        raise ValueError(f"no run {rid}")
    if run["state"] in WORKING:
        set_run(conn, rid, stop_requested=1)
        say(conn, rid, "Code", "system", "Stop pressed - the run stops before its next AI call.")
        return "stopping"
    if run["state"] == "awaiting_approval":
        set_run(conn, rid, state="stopped")
        say(conn, rid, "Code", "system", "Stopped, as you asked. The plan was not saved.")
        return "stopped"
    raise ValueError(f"run {rid} is already {run['state']}")


# --------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------

def next_run(conn):
    row = conn.execute(f"SELECT id FROM runs WHERE state IN ({','.join('?' * len(WORKING))}) "
                       "ORDER BY id LIMIT 1", WORKING).fetchone()
    return row["id"] if row else None


def runner(once=False, room=None):
    """One run at a time, oldest first, until the service is stopped. A lock
    file keeps a second runner (a hand-typed `runner` while the service is
    up) from working the same run twice."""
    state_dir().mkdir(parents=True, exist_ok=True)
    lock = open(state_dir() / "runner.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another Agent Room runner is already running", file=sys.stderr)
        return 1
    conn = connect()
    print("agentroom runner started", flush=True)
    while True:
        meta_set(conn, "runner_beat", now_utc())
        rid = next_run(conn)
        if rid is None:
            if once:
                return 0
            time.sleep(RUNNER_POLL)
            continue
        try:
            state = step(conn, rid, room)
            print(f"run {rid}: {state}", flush=True)
        except Exception as e:                       # a bug, not a run's problem
            set_run(conn, rid, state="failed", error=f"runner error: {str(e)[:300]}")
            say(conn, rid, "Code", "error", f"Stopped by a bug in the runner: {str(e)[:300]}")
            print(f"run {rid}: runner error {e!r}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# what the page shows
# --------------------------------------------------------------------------

def budget_view(conn, refresh=False):
    """Today's Agent Room spend as OpenRouter counts it, read at most once a
    minute (the page asks every few seconds). None fields mean unknown."""
    caps = limits()
    out = {"cap": caps["agentroom_daily_ai"], "run_cap": caps["agentroom_run_max"],
           "today": None, "left": None, "hard_stop": None, "read_utc": None, "error": None}
    key = room_key()
    if key_problem(key):
        return out
    cached = meta_get(conn, "budget")
    if cached and not refresh:
        c = json.loads(cached)
        if (age_seconds(c.get("read_utc")) or 1e9) < BUDGET_CACHE:
            return c
    try:
        d = mod("preflight").key_status(key)
        today = d.get("usage_daily")
        if isinstance(today, (int, float)) and not isinstance(today, bool):
            out["today"] = round(float(today), 4)
            out["left"] = round(caps["agentroom_daily_ai"] - float(today), 4)
        out["hard_stop"] = mod("budget").hard_stop(d.get("limit"), d.get("limit_reset"),
                                                   caps["agentroom_daily_ai"])
    except Exception as e:
        out["error"] = str(e)[:160]
    out["read_utc"] = now_utc()
    meta_set(conn, "budget", json.dumps(out))
    return out


def run_view(conn, run):
    v, plan = latest_plan(conn, run["id"])
    return {"id": run["id"], "slug": run["slug"], "title": run["title"], "dry": bool(run["dry"]),
            "state": run["state"], "state_label": STATE_LABEL.get(run["state"], run["state"]),
            "working": run["state"] in WORKING, "stopping": bool(run["stop_requested"]),
            "round": run["round"], "calls": run["calls"],
            "spent": round(run["spent"], 4), "spent_known": bool(run["spent_known"]),
            "plan_version": v, "phases": len(plan["phases"]) if plan else 0,
            "error": run["error"], "project_dir": run["project_dir"],
            "created_utc": run["created_utc"], "updated_utc": run["updated_utc"]}


def deck(conn):
    runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 50")]
    beat = meta_get(conn, "runner_beat")
    age = age_seconds(beat)
    key = room_key()
    try:
        cfg, cfg_error = roles(), None
    except Exception as e:
        cfg, cfg_error = None, str(e)
    activity = [dict(r) for r in conn.execute(
        "SELECT m.run_id, m.ts, m.who, m.kind, r.title, CASE WHEN m.kind = 'agent' "
        "THEN 'replied (' || length(m.text) || ' characters)' ELSE substr(m.text, 1, 160) END AS text "
        "FROM messages m JOIN runs r ON r.id = m.run_id "
        "WHERE m.kind != 'owner' ORDER BY m.id DESC LIMIT 12")]
    states = [r["state"] for r in runs]
    return {
        "runner": {"alive": age is not None and age < RUNNER_FRESH, "beat_utc": beat},
        "key": {"present": bool(key), "problem": key_problem(key)},
        "budget": budget_view(conn),
        "roles": cfg, "roles_error": cfg_error,
        "counts": {"working": sum(s in WORKING for s in states),
                   "waiting": states.count("awaiting_approval"),
                   "approved": states.count("approved"),
                   "total": len(runs)},
        "runs": [run_view(conn, r) for r in runs],
        "activity": activity,
        "builds_dir": str(builds()),
    }


def show(conn, rid):
    run = get_run(conn, rid)
    if not run:
        raise ValueError(f"no run {rid}")
    v, plan = latest_plan(conn, rid)
    msgs = [dict(m) for m in conn.execute("SELECT * FROM messages WHERE run_id = ? ORDER BY id", (rid,))]
    reviews = [dict(r) for r in conn.execute(
        "SELECT run_id, round, reviewer, verdict, json FROM reviews WHERE run_id = ? ORDER BY round, reviewer",
        (rid,))]
    for r in reviews:
        r["review"] = json.loads(r.pop("json")) if r["json"] else None
    return {"run": run_view(conn, run), "idea": run["idea"], "plan": plan, "plan_version": v,
            "messages": msgs, "reviews": reviews}


def check_text():
    """Free: no model is woken. What a real run would need, and what a
    planning run can cost at most with an idea of typical length."""
    out = []
    try:
        cfg = roles()
    except Exception as e:
        return [f"roles.json  BROKEN: {e}"], 1
    problem = key_problem()
    out.append(f"key         {'ok - the Agent Room has its own key' if not problem else problem}")
    try:
        prices = catalogue()
    except Exception as e:
        out.append(f"models      could not read OpenRouter's model list: {e}")
        return out, 1
    bad = 0
    idea_chars = 1500
    plan_chars = 9000
    total = 0.0
    parts = [("planner", cfg["planner"], 2)] + [("reviewer", r, cfg["review_rounds"]) for r in cfg["reviewers"]]
    for role, part, times in parts:
        p_in, p_out = prices.get(part["model"], (None, None))
        if part["model"] not in prices:
            out.append(f"{role:<11} {part['name']}: {part['model']} - NOT ON OPENROUTER")
            bad += 1
            continue
        prompt = "x" * (len(PLANNER_SYSTEM) + idea_chars + (plan_chars if role == "reviewer" else 0))
        w = worst_case(prices, part["model"], prompt, part["max_tokens"])
        if w is None:
            out.append(f"{role:<11} {part['name']}: {part['model']} - no published price")
            bad += 1
            continue
        out.append(f"{role:<11} {part['name']}: {part['model']}  ${p_in * 1e6:.2f} in / "
                   f"${p_out * 1e6:.2f} out per million tokens; one reply at most ${w:.3f}")
        # the planner writes, then revises with the plan in its prompt
        total += w * times + (tokens_in("x" * plan_chars) * p_in if role == "planner" else 0)
    caps = limits()
    out.append(f"worst case one planning run: ${total:.2f} (a run may spend ${caps['agentroom_run_max']:.2f}, "
               f"a day ${caps['agentroom_daily_ai']:.2f})")
    if not problem:
        try:
            d = mod("preflight").key_status(room_key())
            out.append(f"today       ${float(d.get('usage_daily') or 0):.2f} spent on this key")
            out.append(f"hard stop   {mod('budget').hard_stop(d.get('limit'), d.get('limit_reset'), caps['agentroom_daily_ai'])}")
        except Exception as e:
            out.append(f"today       unreadable: {str(e)[:120]}")
    return out, 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--title", required=True)
    n.add_argument("--idea", required=True)
    n.add_argument("--dry", action="store_true")
    sub.add_parser("runner").add_argument("--once", action="store_true")
    for c in ("step", "approve", "stop", "show"):
        sub.add_parser(c).add_argument("id", type=int)
    sb = sub.add_parser("sendback")
    sb.add_argument("id", type=int)
    sb.add_argument("--note", required=True)
    sub.add_parser("deck")
    sub.add_parser("check")
    a = ap.parse_args(argv)

    if a.cmd == "runner":
        return runner(once=a.once)
    if a.cmd == "check":
        lines, rc = check_text()
        print("\n".join(lines))
        return rc
    conn = connect()
    try:
        if a.cmd == "new":
            rid = new_run(conn, a.title, a.idea, a.dry)
            print(json.dumps({"ok": True, "id": rid}))
        elif a.cmd == "step":
            print(step(conn, a.id))
        elif a.cmd == "approve":
            print(json.dumps({"ok": True, "project_dir": approve(conn, a.id)}))
        elif a.cmd == "sendback":
            send_back(conn, a.id, a.note)
            print(json.dumps({"ok": True}))
        elif a.cmd == "stop":
            print(json.dumps({"ok": True, "said": stop(conn, a.id)}))
        elif a.cmd == "deck":
            print(json.dumps(deck(conn)))
        elif a.cmd == "show":
            print(json.dumps(show(conn, a.id)))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
