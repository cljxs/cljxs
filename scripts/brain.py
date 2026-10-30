#!/usr/bin/env python3
"""
brain.py - the brain map: every agent, and what really passes between them.

    brain.py            JSON for the Deck's /brain page
    brain.py --text     the same in plain lines

Owner, 2026-10-30: "I've always wanted my agents to be able to talk to each
other ... a physical way to see it." This draws what already exists, from
the files and databases that own it - nothing here decides or sends anything:

  NODES   each agent, and the GM, Fury, the knowledge store and the owner.
          An agent's last run is its timers' last start, read by
          health-check.py's own reader; a timer that failed turns it red.
  WIRES   the standing connections, each one a line in code that a test
          finds (tests/test_brain.py), so a wire here cannot outlive the
          code it describes: Belfort's trades reach the Coach because
          coach.py loads belfort-trade.py.
  TRAFFIC what moved in the last WINDOW_DAYS, counted: tasks handed to an
          agent (tasks/queue.db), lessons added to the knowledge store and
          by whom, the GM's proposals and the owner's answers, and what is
          waiting on the owner now (attention.py).
  ALONE   agents that talk to no one but Fury - every agent reports its
          runs to him (gm.py health_facts), so that line does not count.
          That is where "talking to each other" would start.

Free: no model, no network. Read-only.
"""

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
WINDOW_DAYS = 7
FRESH_HOURS = 26           # ran within this: lit; a daily agent misses by more only when it did not run

# (id, name, what it does, the units that are its runs, its Deck page or None,
#  where it sits on the map: x, y in a 100 x 100 box)
NODES = [
    ("you", "You", "the owner: approves, declines, decides", [], None, (50, 50)),
    ("fury", "Fury", "the manager: every agent reports to him; your daily debrief, safe fixes, proposals",
     ["fury-cycle", "fury-collect", "gm-brief"], "/fury", (50, 22)),
    ("knowledge", "Knowledge", "what the ecosystem has learned, once you accept it", [], None, (70, 10)),
    ("scout", "Scout", "Etsy niches and ideas", ["scout-cycle", "scout-niches"], None, (12, 36)),
    ("emily", "Emily", "designs into Printify drafts", ["redraw-art"], "/emily", (12, 58)),
    ("shop", "Shop", "store board and daily TikTok kits", ["tiktok-kit"], "/shop", (14, 80)),
    ("stan", "Stan", "software pains from Hacker News", ["stan-fetch", "stan-cycle"], "/stan", (34, 92)),
    ("agentroom", "Agent Room", "an idea into a reviewed build plan", ["agentroom-runner"], "/agentroom", (54, 92)),
    ("paul", "Paul", "local-business websites", ["paul-cycle"], "/paul", (32, 72)),
    ("clip", "Clip", "permitted video into captioned shorts", ["clip-pickup"], None, (74, 92)),
    ("belfort", "Belfort", "paper stock trader",
     ["belfort-cycle", "belfort-fetch", "belfort-exits", "belfort-review"], "/markets", (88, 30)),
    ("bolt", "Bolt", "paper crypto trader", ["bolt-cycle"], "/bolt", (92, 52)),
    ("coach", "Coach", "journals and reviews the traders and Ace", ["coach-daily", "coach-weekly"], "/coach", (70, 44)),
    ("ace", "Ace", "paper sports bets", ["ace-cycle", "ace-fetch", "ace-close"], "/ace", (86, 74)),
    ("aceprops", "Ace Props", "NBA points milestones, plain code", ["ace-props", "ace-props-close"], "/ace-props", (68, 70)),
    ("peter", "Peter", "player props, weather, defense", ["peter-defense"], "/peter", (90, 92)),
]

# (from, to, what passes, the file and the line in it that does it). The test
# reads every one of these in its file: a wire whose code has gone fails it.
WIRES = [
    ("scout", "emily", "an idea you approve becomes her build", "scripts/scout-review.py", "emily-new-build.py"),
    ("emily", "shop", "her products become TikTok kits", "scripts/tiktok-kit.py", "emily"),
    ("belfort", "coach", "his closed trades, journaled", "scripts/coach.py", "belfort-trade.py"),
    ("bolt", "coach", "both books' trades, journaled", "scripts/coach.py", "bolt.py"),
    ("ace", "coach", "his settled bets and closing lines", "scripts/coach.py", "ace-book.py"),
    ("ace", "aceprops", "one bankroll, two books", "scripts/ace-props.py", "ace-book.py"),
    ("stan", "agentroom", "a pain you approve becomes a plan", "scripts/stan.py", "agentroom"),
    ("scout", "fury", "ideas waiting on you", "scripts/gm.py", "fury.scout.load_ideas"),
    ("knowledge", "fury", "accepted lessons", "scripts/gm.py", "knowledge_facts"),
    ("fury", "you", "the daily debrief: what needs you, proposals to approve", "scripts/gm.py", "def decide"),
]
# Every agent reports to Fury (owner, 2026-10-30): its runs and failures,
# through gm.py's health_facts.
WIRES += [(nid, "fury", "reports its runs and failures", "scripts/gm.py", "health_facts")
          for nid, *_ in NODES if nid not in ("you", "fury", "knowledge")]

ATTENTION_NODE = {"shop": "shop", "stan": "stan", "agentroom": "agentroom", "bolt": "bolt"}


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def node_ids():
    return [n[0] for n in NODES]


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def runs(facts=None, now=None):
    """{node: {"last_run_utc", "state", "failed": [unit]}} from each node's
    timers, read the way health-check.py reads them. state: "fresh" (ran in
    FRESH_HOURS), "quiet" (ran, longer ago), "failed" (a run failed),
    "unknown" (no timer, or no systemd to ask)."""
    facts = facts or _load("health_check_brain", "health-check.py").unit_facts
    now = (now or datetime.now(timezone.utc)).timestamp()
    out = {}
    for nid, _name, _what, units, _page, _xy in NODES:
        last, failed, seen = None, [], False
        for u in units:
            result, started, load = facts(f"{u}.service")
            if load not in ("loaded",):
                continue
            seen = True
            if result not in ("success", "?", ""):
                failed.append(u)
            if started and (last is None or started > last):
                last = started
        state = ("failed" if failed else "unknown" if not seen or last is None
                 else "fresh" if now - last <= FRESH_HOURS * 3600 else "quiet")
        out[nid] = {"last_run_utc": _utc(last), "state": state, "failed": failed}
    return out


def task_traffic(db=None, now=None):
    """[(from, to, label, count, detail)] from the task queue: who handed
    which agent work in the window. A build from a Scout idea is drawn from
    Scout, through you, to Emily - that is the path it took."""
    db = Path(db or ROOT / "tasks" / "queue.db")
    if not db.is_file():
        return []
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT created_by, assignee, status, payload FROM tasks WHERE created_at >= ?",
                           (since,)).fetchall()
    finally:
        con.close()
    known, out = set(node_ids()), {}
    for by, to, status, payload in rows:
        by = "you" if by in (None, "", "you", "owner", "deck") else by
        if by not in known or to not in known:
            continue
        key = (by, to)
        c = out.setdefault(key, {"n": 0, "by_status": {}})
        c["n"] += 1
        c["by_status"][status] = c["by_status"].get(status, 0) + 1
    return [(by, to, "tasks handed over", c["n"],
             ", ".join(f"{v} {k}" for k, v in sorted(c["by_status"].items())))
            for (by, to), c in sorted(out.items())]


def knowledge_traffic(db=None, now=None):
    """[(source, "knowledge", label, count, detail)]: lessons added in the
    window, by whoever added them, and how many you have accepted."""
    k = _load("knowledge_brain", "knowledge.py")
    path = Path(db or k.DB)
    if not path.is_file():
        return []
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = k.connect(path)
    try:
        rows = [e for e in k.entries(con) if str(e.get("created_at") or "") >= since]
    finally:
        con.close()
    known, out = set(node_ids()), {}
    for e in rows:
        src = str(e.get("source") or "")
        src = "fury" if src == "gm" else src        # the GM is Fury since 2026-10-30
        src = src if src in known else "you" if src in ("owner", "you", "") else None
        if not src:
            continue
        c = out.setdefault(src, {})
        c[e["status"]] = c.get(e["status"], 0) + 1
    return [(src, "knowledge", "lessons added", sum(c.values()),
             ", ".join(f"{v} {s}" for s, v in sorted(c.items()))) for src, c in sorted(out.items())]


def gm_traffic(days_dir=None, now=None):
    """[("fury", who, label, count, detail)]: Fury's proposals in the window,
    to you or to an agent, and what you said."""
    d = Path(days_dir or ROOT / "tasks" / "gm")
    if not d.is_dir():
        return []
    first = ((now or datetime.now(timezone.utc)) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    known, out = set(node_ids()), {}
    for f in sorted(d.glob("*.json")):
        if f.stem < first:
            continue
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for p in doc.get("proposals") or []:
            who = str(p.get("who") or "").lower()
            who = who if who in known else "you"
            dec = p.get("decision") or {}
            state = "waiting" if not dec else "approved" if dec.get("verdict") == "approve" else "declined"
            c = out.setdefault(who, {})
            c[state] = c.get(state, 0) + 1
    return [("fury", who, "proposals", sum(c.values()), ", ".join(f"{v} {s}" for s, v in sorted(c.items())))
            for who, c in sorted(out.items()) if who != "fury"]


def fix_traffic(days_dir=None, now=None):
    """[("fury", node, label, count, detail)]: the re-runs Fury made himself
    in the window (gm.py's ledger), drawn to the agent the unit belongs to."""
    path = Path(days_dir or ROOT / "tasks" / "gm") / "fixes.jsonl"
    try:
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []
    first = ((now or datetime.now(timezone.utc)) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    owner = {u: nid for nid, _n, _w, units, _p, _xy in NODES for u in units}
    out = {}
    for r in rows:
        nid = owner.get(r.get("unit"))
        if nid and str(r.get("day") or "") >= first:
            c = out.setdefault(nid, {})
            c[r.get("status")] = c.get(r.get("status"), 0) + 1
    return [("fury", nid, "safe fixes (re-runs)", sum(c.values()), ", ".join(f"{v} {k}" for k, v in sorted(c.items())))
            for nid, c in sorted(out.items())]


def waiting(att=None):
    """[(node, "you", label, count, detail)]: what is waiting on you now."""
    try:
        att = att if att is not None else _load("attention_brain", "attention.py").attention()
    except Exception:
        return []
    out = []
    for tab, nid in ATTENTION_NODE.items():
        a = att.get(tab) or {}
        if a.get("n"):
            why = a.get("why") or []
            out.append((nid, "you", "waiting on you" if a.get("tone") != "alert" else "needs a look",
                        int(a["n"]), "; ".join(str(w) for w in why)[:200]))
    return out


def brain(now=None, facts=None, db=None, kdb=None, gm_dir=None, att=None):
    now = now or datetime.now(timezone.utc)
    lit = runs(facts, now)
    nodes = [{"id": nid, "name": name, "what": what, "page": page, "x": x, "y": y, "units": units, **lit[nid]}
             for nid, name, what, units, page, (x, y) in NODES]
    edges = [{"from": a, "to": b, "kind": "report" if (b == "fury" and needle == "health_facts") else "wire",
              "label": label, "count": None, "detail": f"{f}: {needle}"}
             for a, b, label, f, needle in WIRES]
    for kind, rows in (("tasks", task_traffic(db, now)), ("knowledge", knowledge_traffic(kdb, now)),
                       ("proposals", gm_traffic(gm_dir, now)), ("fixes", fix_traffic(gm_dir, now)),
                       ("waiting", waiting(att))):
        edges += [{"from": a, "to": b, "kind": kind, "label": label, "count": n, "detail": detail}
                  for a, b, label, n, detail in rows]
    # Alone: talks to no one but its report to Fury - every agent reports.
    talk = [e for e in edges if e["kind"] != "report"]
    touched = {e["from"] for e in talk} | {e["to"] for e in talk}
    alone = [n["id"] for n in nodes if n["id"] not in touched]
    return {"asof_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "window_days": WINDOW_DAYS,
            "nodes": nodes, "edges": edges, "alone": alone,
            "fresh_hours": FRESH_HOURS}


def text(b):
    names = {n["id"]: n["name"] for n in b["nodes"]}
    out = [f"Brain map, {b['asof_utc']} (traffic: last {b['window_days']} days)"]
    for n in b["nodes"]:
        out.append(f"  {n['name']:<11} {n['state']:<8} last run {n['last_run_utc'] or '-'}")
    for e in b["edges"]:
        count = "" if e["count"] is None else f" x{e['count']} ({e['detail']})"
        out.append(f"  {names[e['from']]} -> {names[e['to']]}: {e['label']}{count}")
    if b["alone"]:
        out.append("  Working alone: " + ", ".join(names[a] for a in b["alone"]))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--text", action="store_true")
    a = ap.parse_args(argv)
    b = brain()
    print(text(b) if a.text else json.dumps(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
