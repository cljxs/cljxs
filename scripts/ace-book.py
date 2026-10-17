#!/usr/bin/env python3
"""
ace-book.py - Ace's money, kept by code. Bets go in, results come from
ESPN's final scores, and the bankroll is worked out from those two alone.

    python3 scripts/ace-book.py show        bankroll, open bets, settled bets
    python3 scripts/ace-book.py settle      grade what has finished, now
    python3 scripts/ace-book.py rebuild     rewrite bankroll.json from the bets

WHY. Until 2026-10-01 Ace graded his own bets: he found each game, decided
whether it was won, worked out the profit and edited bankroll.json by hand.
Everything downstream - the 4-bet cap, the stop, every return figure - read
that file, and a verifier that checks the file balances cannot tell a
consistent mistake from the truth. An outside review put it first of
fourteen, and it is the same fix Belfort had: arithmetic belongs in code.

THE RECORD is state/bets.jsonl, append-only: one line when a bet is placed
(ace-judge.py bet writes it), one when it settles (this file writes it).
Nothing is ever edited in place. bankroll.json is rebuilt from it - starting
bankroll, less every stake, plus every return - so it cannot drift, and a
hand edit shows up as a difference from the rebuild (ace-verify.py checks).

RESULTS come from the scoreboards ace-fetch.py already downloads, kept in
state/results.json. A bet is:

    won / lost   STATUS_FINAL, by the score
    push         STATUS_FINAL and level (an NFL tie): the stake comes back
    void         postponed or cancelled - or no final score 72 hours after
                 the start (a suspended game that never resumed). The stake
                 comes back.

The words are the ones the Deck's ace.js and ace-verify.py already read.

Standard library only.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
STATE = ROOT / "agents" / "ace" / "state"
BETS = STATE / "bets.jsonl"
BANK = STATE / "bankroll.json"
RESULTS = STATE / "results.json"

STARTING_BANKROLL = 10000.0
# Flat stakes (2026-10-01, from the review): 1% of the STARTING bankroll on
# every bet, $100. Quarter-Kelly sized each bet from Ace's own estimate, so
# a confident mistake bet bigger, and returns could not be compared bet to
# bet. Kelly is still worked out and stored on each bet, as a shadow figure.
FLAT_STAKE_PCT = 1.0
MAX_OPEN_BETS = 4
# A fault detector, not a verdict on skill: at flat 1% stakes a bettor with
# no edge at all almost never loses 30% in the first few hundred bets, so
# reaching it means something mechanical is wrong. (It was -15%, which a
# zero-edge bettor at 3% stakes hits about half the time - noise, not news.)
# Whether he has an edge is ace-judge.py's evidence stop.
FAULT_STOP_PCT = 30.0
VOID_AFTER_HOURS = 72
FINAL = "STATUS_FINAL"
VOIDED = ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED")
KEEP_RESULTS_DAYS = 40
STAMP = "%Y-%m-%d %H:%M:%S"


def utc_now():
    return datetime.now(timezone.utc)


def parse_utc(s):
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ", STAMP):
        try:
            return datetime.strptime(str(s or ""), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _write(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n")
    tmp.replace(path)


def decimal_odds(american):
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return 1 + a / 100
    if a <= -100:
        return 1 + 100 / -a
    return None


# ------------------------------------------------------------------ the record

def events(path=None):
    out = []
    try:
        text = Path(path or BETS).read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def append(event, path=None):
    path = Path(path or BETS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")


def bet_id(sport, event_id, selection):
    raw = f"{sport}|{event_id}|{selection}"
    return "b" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def starting_bankroll():
    b = _read(BANK, {})
    try:
        return float(b.get("starting_bankroll") or STARTING_BANKROLL)
    except (TypeError, ValueError):
        return STARTING_BANKROLL


def flat_stake():
    return round(starting_bankroll() * FLAT_STAKE_PCT / 100, 2)


def fold(evs, start=None):
    """The book from its events: open bets, settled bets, and the bankroll -
    cash not tied up in an open bet. The identity ace-verify.py checks:
    start - every stake + every return."""
    start = STARTING_BANKROLL if start is None else float(start)
    bets, settles = {}, {}
    for e in evs:
        if e.get("kind") == "bet" and e.get("id") not in bets:
            bets[e["id"]] = e
        elif e.get("kind") == "settle" and e.get("id") in bets and e["id"] not in settles:
            settles[e["id"]] = e
    open_bets, settled = [], []
    cash = start
    for i, b in bets.items():
        stake = float(b.get("stake") or 0)
        cash -= stake
        s = settles.get(i)
        if s is None:
            open_bets.append(b)
            continue
        profit = float(s.get("profit") or 0)
        cash += stake + profit
        settled.append(dict(b, result=s.get("result"), profit=round(profit, 2),
                            settled_utc=s.get("utc"), detail=s.get("detail")))
    return {"open": open_bets, "settled": settled, "bankroll": round(cash, 2),
            "profit": round(sum(s["profit"] for s in settled), 2)}


def _open_row(b):
    return {k: b.get(k) for k in ("id", "sport", "event_id", "match", "selection", "side",
                                  "starts_utc", "price", "price_source", "stake", "my_pct", "p_used",
                                  "ev_pct", "novig_pct", "sharp_pct", "event", "why", "utc",
                                  "kelly_shadow_stake", "imported")
            if b.get(k) is not None}


def rebuild(path=None, bank=None):
    """Rewrite bankroll.json from the bets, keeping what is not money: the
    cycle count, when it started, the mode. Returns the new document."""
    bank = Path(bank or BANK)
    doc = _read(bank, {}) or {}
    start = float(doc.get("starting_bankroll") or STARTING_BANKROLL)
    book = fold(events(path), start)
    doc.setdefault("mode", "paper")
    doc.setdefault("currency", "USD")
    doc["starting_bankroll"] = start
    doc["bankroll"] = book["bankroll"]
    doc["open_bets"] = [_open_row(b) for b in book["open"]]
    doc["settled_bets"] = [dict(_open_row(s), result=s["result"], profit=s["profit"],
                                settled_utc=s.get("settled_utc"), detail=s.get("detail"))
                           for s in book["settled"]]
    doc["kept_by"] = "ace-book.py from state/bets.jsonl - never edit by hand"
    _write(bank, doc)
    return doc


def drift(path=None, bank=None):
    """None when bankroll.json says what the bets say; otherwise what differs
    - a hand edit, which nothing else would notice."""
    doc = _read(bank or BANK, None)
    if doc is None:
        return None
    start = float(doc.get("starting_bankroll") or STARTING_BANKROLL)
    book = fold(events(path), start)
    probs = []
    try:
        if abs(float(doc.get("bankroll") or 0) - book["bankroll"]) > 0.01:
            probs.append(f"bankroll ${float(doc.get('bankroll') or 0):,.2f} but the bets give "
                         f"${book['bankroll']:,.2f}")
    except (TypeError, ValueError):
        probs.append("bankroll is not a number")
    if len(doc.get("open_bets") or []) != len(book["open"]):
        probs.append(f"{len(doc.get('open_bets') or [])} open bets listed, the bets give {len(book['open'])}")
    if len(doc.get("settled_bets") or []) != len(book["settled"]):
        probs.append(f"{len(doc.get('settled_bets') or [])} settled listed, the bets give {len(book['settled'])}")
    return "; ".join(probs) or None


def migrate(path=None, bank=None, now=None):
    """Once, when bets.jsonl does not exist yet: the bets Ace wrote into
    bankroll.json by hand become events, marked imported, so the history is
    kept. A settled one keeps the profit he recorded - there is no score on
    file to check it against. Returns how many were imported, or None if
    there was nothing to do."""
    path, bank = Path(path or BETS), Path(bank or BANK)
    if path.exists():
        return None
    doc = _read(bank, {}) or {}
    stamp = (now or utc_now()).strftime(STAMP)
    n = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    for b in (doc.get("settled_bets") or []) + (doc.get("open_bets") or []):
        if not isinstance(b, dict):
            continue
        sel, match = b.get("selection") or b.get("pick"), b.get("match") or b.get("fixture")
        stake = float(b.get("stake") or 0)
        i = b.get("id") or bet_id(b.get("sport"), b.get("event_id") or match, sel)
        append({"kind": "bet", "id": i, "utc": b.get("utc") or b.get("placed_utc") or stamp,
                "imported": True, "sport": b.get("sport"), "event_id": b.get("event_id"),
                "match": match, "selection": sel, "side": b.get("side"),
                "starts_utc": b.get("starts_utc") or b.get("start_utc"),
                "price": b.get("price") or b.get("odds"), "stake": stake,
                "my_pct": b.get("my_pct")}, path)
        n += 1
        if b in (doc.get("settled_bets") or []):
            result = str(b.get("result") or "").lower()
            if b.get("profit") is not None:
                profit = float(b["profit"])
            elif b.get("payout") is not None:
                profit = float(b["payout"]) - stake
            elif result in ("loss", "lost", "l"):
                profit = -stake
            else:
                profit = 0.0
            word = ("won" if profit > 0 else "lost" if profit < 0
                    else "void" if result in ("void", "cancelled", "canceled") else "push")
            append({"kind": "settle", "id": i, "utc": stamp, "result": word,
                    "profit": round(profit, 2), "detail": "imported from the hand-kept bankroll"}, path)
    return n


# ------------------------------------------------------------------ results

def results_of(sport, board_events):
    """{"<sport>|<event id>": result} for every game on a scoreboard that is
    final, postponed or cancelled. Read off the same scoreboard the fetcher
    already downloads; a scheduled game shows a score of "0", which is not
    a result."""
    out = {}
    for e in board_events or []:
        try:
            comp = e["competitions"][0]
            status = comp["status"]["type"]["name"]
        except (KeyError, IndexError, TypeError):
            continue
        if status != FINAL and status not in VOIDED:
            continue
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        h, a = sides.get("home") or {}, sides.get("away") or {}
        row = {"sport": sport, "event_id": str(e.get("id")), "match": e.get("shortName"),
               "start_utc": e.get("date"), "status": status,
               "home": (h.get("team") or {}).get("abbreviation"),
               "away": (a.get("team") or {}).get("abbreviation"),
               "home_score": None, "away_score": None, "winner": None}
        if status == FINAL:
            try:
                hs, as_ = int(h.get("score")), int(a.get("score"))
            except (TypeError, ValueError):
                continue                       # final with no readable score: wait
            row.update(home_score=hs, away_score=as_,
                       winner="home" if hs > as_ else "away" if as_ > hs else "tie")
        out[f"{sport}|{row['event_id']}"] = row
    return out


def record_results(sport, board_events, path=None, now=None):
    path = Path(path or RESULTS)
    have = _read(path, {})
    have.update(results_of(sport, board_events))
    cutoff = (now or utc_now()) - timedelta(days=KEEP_RESULTS_DAYS)
    have = {k: v for k, v in have.items() if (parse_utc(v.get("start_utc")) or cutoff) >= cutoff}
    _write(path, have)
    return have


def find_result(results, bet):
    """The result for a bet: by sport and ESPN event id, or - for a bet Ace
    wrote by hand before this file existed - by the fixture name."""
    r = results.get(f"{bet.get('sport')}|{bet.get('event_id')}")
    if r:
        return r
    match = str(bet.get("match") or "").upper()
    hits = [v for v in results.values() if str(v.get("match") or "").upper() == match and match]
    start = parse_utc(bet.get("starts_utc"))
    if start:
        hits = [v for v in hits if abs(((parse_utc(v.get("start_utc")) or start) - start).total_seconds()) < 36 * 3600]
    return hits[0] if len(hits) == 1 else None


def side_of(bet, result):
    if bet.get("side") in ("home", "away"):
        return bet["side"]
    team = str(bet.get("selection") or "").rsplit(" ML", 1)[0].strip().upper()
    if team and team == str(result.get("home") or "").upper():
        return "home"
    if team and team == str(result.get("away") or "").upper():
        return "away"
    return None


def outcome(bet, result, now=None):
    """(result word, profit, detail) for one open bet, or None while it must
    wait."""
    stake = float(bet.get("stake") or 0)
    start = parse_utc(bet.get("starts_utc"))
    if result is None:
        if start and (now or utc_now()) - start > timedelta(hours=VOID_AFTER_HOURS):
            return "void", 0.0, f"no final score {VOID_AFTER_HOURS}h after the start"
        return None
    if result["status"] in VOIDED:
        return "void", 0.0, result["status"].replace("STATUS_", "").lower()
    side = side_of(bet, result)
    if side is None:
        return None
    score = f"{result['away']} {result['away_score']} @ {result['home']} {result['home_score']}"
    if result["winner"] == "tie":
        return "push", 0.0, score
    if result["winner"] == side:
        d = decimal_odds(bet.get("price"))
        if d is None:
            return None
        return "won", round(stake * (d - 1), 2), score
    return "lost", -stake, score


def settle(path=None, results_path=None, bank=None, now=None):
    """Grade every open bet that can be graded. Returns the settle events
    written, and rebuilds bankroll.json when there were any."""
    now = now or utc_now()
    results = _read(results_path or RESULTS, {})
    done = []
    for b in fold(events(path))["open"]:
        got = outcome(b, find_result(results, b), now)
        if not got:
            continue
        word, profit, detail = got
        e = {"kind": "settle", "id": b["id"], "utc": now.strftime(STAMP), "result": word,
             "profit": profit, "detail": detail}
        append(e, path)
        done.append(dict(e, selection=b.get("selection"), match=b.get("match")))
    if done:
        rebuild(path, bank)
    return done


# ------------------------------------------------------------------ the rules

def fault_stop(path=None):
    """The reason betting is paused, or None: settled results have lost
    FAULT_STOP_PCT of the starting bankroll."""
    start = starting_bankroll()
    lost = -fold(events(path), start)["profit"]
    if lost >= start * FAULT_STOP_PCT / 100:
        return (f"settled bets have lost ${lost:,.2f}, {lost / start * 100:.1f}% of the "
                f"${start:,.0f} start - the {FAULT_STOP_PCT:g}% fault stop. Something mechanical "
                f"may be wrong; the owner looks before any more bets")
    return None


def refusal(sport, event_id, selection, match=None, path=None):
    """Why a bet may not be placed now, or None."""
    book = fold(events(path), starting_bankroll())
    if len(book["open"]) >= MAX_OPEN_BETS:
        return (f"{len(book['open'])} bets are already open and the cap is {MAX_OPEN_BETS}. "
                f"They settle by themselves when their games finish")
    for b in book["open"] + book["settled"]:
        same = (str(b.get("event_id")) == str(event_id) and b.get("sport") == sport) if event_id \
            else (match and b.get("match") == match)
        if same:
            return f"there is already a bet on this game ({b.get('selection')}, {b.get('result') or 'open'})"
    return fault_stop(path)


def place(fields, path=None, bank=None, now=None):
    """Write one bet. `fields` comes from ace-judge.py, which has checked the
    bar; the stake is set here."""
    stamp = (now or utc_now()).strftime(STAMP)
    e = dict(fields, kind="bet", utc=stamp, stake=flat_stake(),
             id=bet_id(fields.get("sport"), fields.get("event_id"), fields.get("selection")))
    append(e, path)
    rebuild(path, bank)
    return e


# ------------------------------------------------------------------ commands

def lines(path=None):
    start = starting_bankroll()
    book = fold(events(path), start)
    s = book["settled"]
    real = [x for x in s if x["result"] in ("won", "lost")]
    won = sum(1 for x in real if x["result"] == "won")
    out = [f"bankroll ${book['bankroll']:,.2f} cash, ${sum(float(b.get('stake') or 0) for b in book['open']):,.2f} "
           f"in {len(book['open'])} open bet(s); settled profit ${book['profit']:+,.2f} "
           f"({len(s)} settled: {won} won, {len(real) - won} lost, {len(s) - len(real)} push/void)",
           f"stake ${flat_stake():,.2f} a bet, flat; at most {MAX_OPEN_BETS} open"]
    stop = fault_stop(path)
    if stop:
        out.append("STOPPED: " + stop)
    for b in book["open"]:
        flag = "" if b.get("event_id") else "  [no ESPN id: settles by fixture name, or by hand]"
        out.append(f"  open  {b.get('selection')} ({b.get('match')}) {b.get('price')} "
                   f"${float(b.get('stake') or 0):,.2f}{flag}")
    for x in s[-10:]:
        out.append(f"  {x['result']:<5} {x.get('selection')} ({x.get('match')}) {x.get('price')} "
                   f"{x['profit']:+,.2f}  {x.get('detail') or ''}")
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Ace's money, kept by code")
    ap.add_argument("cmd", nargs="?", default="show", choices=["show", "settle", "rebuild"])
    cmd = ap.parse_args(argv).cmd
    # After parsing: --help must never touch the book.
    n = migrate()
    if n:
        print(f"imported {n} hand-written bet(s) from bankroll.json into state/bets.jsonl")
        rebuild()
    if cmd == "settle":
        done = settle()
        for d in done:
            print(f"settled {d['selection']} ({d['match']}): {d['result']} {d['profit']:+,.2f} - {d['detail']}")
        print(f"{len(done)} settled")
    elif cmd == "rebuild":
        doc = rebuild()
        print(f"bankroll.json rebuilt: ${doc['bankroll']:,.2f}, {len(doc['open_bets'])} open")
    else:
        print("\n".join(lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
