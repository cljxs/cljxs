#!/usr/bin/env python3
"""
ace-shop.py - the Line Shopper: Pinnacle's true odds against the best price
at nine US books, by plain code, on its own paper $10,000.

    ace-shop.py run            settle, then check the games starting soon and bet any +2% price
    ace-shop.py close          settle; take Pinnacle's closing price for bets about to start
    ace-shop.py check          is the key there and how many credits are left (free)
    ace-shop.py check nba      spend 1 credit: one sport's prices, which books answered, the best edges
    ace-shop.py board          what Ace's page draws (JSON)
    ace-shop.py show           the book in words

THE IDEA (owner, 2026-11-11, from WinnerOdds' "True Odds Calculator"): a
sharp bookmaker's price, with its margin taken out, is the best estimate of
the true chance; a soft bookmaker paying more than that true price is a bet
worth more than it costs. Pinnacle is the sharp book. The nine US books are
the ones a slow price might sit at. Judged by Pinnacle's close, as the page
says: beating the close is the edge; winning or losing a few bets is noise.

THE TRUE ODDS, five ways (the page's methods; its "margin proportional to
odds" gives the same chances as additive, so it is not counted twice):
equal margin, additive, Shin, odds ratio and logarithmic (power). Nobody
knows which one a bookmaker uses, so a bet must be worth EDGE_PCT under
EVERY one of them - the least favourable - and it is graded the same way at
the close. On two-way markets they differ most on long shots, where equal
margin (what ace-sharp.py's novig uses) flatters the underdog.

THE GUARDS (owner, 2026-11-11, option B): the danger is a stale PINNACLE
price, not a stale US one - Pinnacle's prices on The Odds API come from its
public site and "may incur a delay", and against a stale Pinnacle every US
book looks wrong. So:
  * a US price counts however old its stamp - if it is listed it is on
    offer, and a slow price is the very thing being looked for;
  * Pinnacle's must be under PIN_FRESH_MIN minutes old;
  * the crowd check: Pinnacle's no-vig chance must be within CROWD_PTS of
    the middle (median) of the US books' no-vig chances, with at least
    CROWD_MIN books to ask. If Pinnacle disagrees with the books in
    general, Pinnacle is the likelier one to be wrong: no bet. The median,
    so the one slow book being bet cannot pull the middle toward itself.
A worth of more than MAX_EDGE_PCT is taken for a wrong line, not a gift; the
true chance must be 20-80%; one bet a game, DAILY_CAP a day, $100 flat.

THE KEY is the second free Odds API account (500 credits a month), stored
on 2026-11-11 as ODDS_API_KEY_NFL; ODDS_API_KEY_SHOP is read first if it is
ever stored under that name. Never Ace's own ODDS_API_KEY. One sport's
moneylines from Pinnacle and nine US books is ONE credit (up to ten
bookmakers count as one region, per the API's documentation). A run spends
one credit per sport that has an unbet game in the window; a close one per
sport with a bet starting within CLOSE_MIN. DAILY_CALLS caps a day, RESERVE
is kept for closes.

THE GAMES are ESPN's, from the context files ace-fetch.py writes, joined to
The Odds API's by ace-sharp.py's join; bets settle by ESPN's results
(ace-book.py, on this book's own files under agents/ace/state/shop/). Ace's
own book, bankroll and measures never see these bets.

No model is woken. Standard library only.
"""

import argparse
import importlib.util
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
ACE = ROOT / "agents" / "ace"
SHOP = ACE / "state" / "shop"
BETS = SHOP / "bets.jsonl"
BANK = SHOP / "bankroll.json"
USAGE = SHOP / "usage.json"
CLOSES = SHOP / "closes.json"
BOARD = ACE / "data" / "shop-board.json"
CTX = ACE / "data" / "context"
CREDENTIALS = ACE / "state" / "credentials.env"

KEY_NAMES = ("ODDS_API_KEY_SHOP", "ODDS_API_KEY_NFL")   # the second account; never ODDS_API_KEY
SHARP_BOOK = "pinnacle"
US_BOOKS = ("draftkings", "fanduel", "betmgm", "betrivers", "espnbet",
            "hardrockbet", "ballybet", "betparx", "fliff")   # free-plan US books (Caesars, Fanatics are paid-only)
SPORTS = ("nfl", "nba", "cfb")

METHODS = ("equal margin", "additive", "shin", "odds ratio", "log")
EDGE_PCT = 2.0                 # the owner's number, 2026-11-11: worth +2% under every method
MAX_EDGE_PCT = 10.0            # more than this is a stale or wrong line
TRUE_RANGE = (20.0, 80.0)      # true chance of the side bet, in %
PIN_FRESH_MIN = 60             # Pinnacle's price older than this is not used (US prices: no limit)
CROWD_PTS = 3.0                # Pinnacle's no-vig chance within this of the US books' median...
CROWD_MIN = 2                  # ...asked of at least this many US books
DAILY_CAP = 5                  # bets a day, Eastern, by when placed
LOOK_AHEAD_H = 12              # games starting within this...
NOT_WITHIN_MIN = 20            # ...and not sooner than this
CLOSE_MIN = 20                 # a bet's close: Pinnacle within this before the start
CLOSE_GAP_MIN = 15             # one close call per sport in this long
DAILY_CALLS = 18
RESERVE = 15                   # credits only a close may spend
STARTING_BANKROLL = 10000.0
STOP_USD = 1000.0              # settled losses that pause the shopper
STAMP = "%Y-%m-%d %H:%M:%S"
ISO = "%Y-%m-%dT%H:%M:%SZ"

DESCRIBE = ("The Line Shopper: Pinnacle's true odds (its margin taken out five ways) against the best price at "
            "nine US books; bets when a price is worth +2% or more under every way, on its own paper $10,000.")


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SHARP = _load("ace_sharp_for_shop", "ace-sharp.py")     # the API call, team names, the join


def book():
    """ace-book.py on this book's own files: its money, never Ace's."""
    b = _load("ace_book_shop", "ace-book.py")
    b.STATE, b.BETS, b.BANK = SHOP, BETS, BANK
    b.STARTING_BANKROLL = STARTING_BANKROLL
    return b


def utc_now():
    return datetime.now(timezone.utc)


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


def log(msg):
    print(f"[ace-shop {utc_now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ the true odds

def _solve(f, lo, hi, steps=200):
    """The root of an increasing-then-decreasing sum f between lo and hi, by halving."""
    for _ in range(steps):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def true_chances(decimals):
    """{method: [chance of each outcome]} with the margin taken out of
    decimal prices five ways (WinnerOdds' True Odds Calculator; Shin after
    Clarke et al., odds ratio after Cheung, logarithmic after Buchdahl).
    A method that cannot give every outcome a chance above 0 is left out."""
    pi = [1 / float(d) for d in decimals]
    s, n = sum(pi), len(pi)
    out = {"equal margin": [p / s for p in pi],
           "additive": [p - (s - 1) / n for p in pi]}
    if s > 1:
        def shin(z):
            return [(math.sqrt(z * z + 4 * (1 - z) * p * p / s) - z) / (2 * (1 - z)) for p in pi]
        out["shin"] = shin(_solve(lambda z: sum(shin(z)) - 1, 0.0, 0.5))
        c = _solve(lambda c: sum(p / (c + p - c * p) for p in pi) - 1, 1.0, 10.0)
        out["odds ratio"] = [p / (c + p - c * p) for p in pi]
        k = _solve(lambda k: sum(p ** k for p in pi) - 1, 1.0, 10.0)
        out["log"] = [p ** k for p in pi]
    else:
        # No margin (or a negative one): every method is the plain inverse, normalised.
        for m in ("shin", "odds ratio", "log"):
            out[m] = list(out["equal margin"])
    return {m: v for m, v in out.items() if all(0 < p < 1 for p in v)}


def worth(price_dec, chances, side):
    """{method: what the price is worth, in %} - decimal x true chance - 1."""
    return {m: round((float(price_dec) * v[side] - 1) * 100, 2) for m, v in chances.items()}


# ------------------------------------------------------------------ prices

def parse_iso(s):
    for fmt in (ISO, "%Y-%m-%dT%H:%MZ", STAMP):
        try:
            return datetime.strptime(str(s or ""), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def h2h(event, key):
    """(home decimal, away decimal, last update) for one bookmaker, or None."""
    for b in event.get("bookmakers") or []:
        if b.get("key") != key:
            continue
        for m in b.get("markets") or []:
            if m.get("key") != "h2h":
                continue
            by = {o.get("name"): o.get("price") for o in m.get("outcomes") or []}
            h, a = by.get(event.get("home_team")), by.get(event.get("away_team"))
            if h and a:
                return float(h), float(a), parse_iso(m.get("last_update") or b.get("last_update"))
    return None


def fresh(when, now):
    return when is not None and now - when <= timedelta(minutes=PIN_FRESH_MIN)


def age_min(when, now):
    return None if when is None else round((now - when).total_seconds() / 60)


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


def crowd(event, pin):
    """(Pinnacle's no-vig home chance, the US books' median, how many books)
    - all by equal margin, so the two are measured the same way."""
    us = [h2h(event, k) for k in US_BOOKS]
    homes = [SHARP.novig(p[0], p[1])[0] for p in us if p]
    return SHARP.novig(pin[0], pin[1])[0], median(homes), len(homes)


def judge_game(ctx, event, now):
    """One game: Pinnacle's true chances five ways, the crowd check, the
    best US price on each side, its worth under every method, the verdict."""
    out = {"event_id": str(ctx.get("event_id")), "sport": ctx.get("sport"), "match": ctx.get("short"),
           "starts_utc": ctx.get("start_utc"), "odds_id": event.get("id"), "sides": {}, "bet": None, "why": ""}
    pin = h2h(event, SHARP_BOOK)
    if not pin:
        out["why"] = "no Pinnacle price"
        return out
    if not fresh(pin[2], now):
        out["why"] = f"Pinnacle's price is {age_min(pin[2], now)} minutes old - over {PIN_FRESH_MIN}"
        return out
    mine, middle, n = crowd(event, pin)
    out["crowd"] = {"pinnacle_home_pct": mine, "us_median_home_pct": middle, "books": n}
    if n < CROWD_MIN:
        out["why"] = f"only {n} US book(s) priced it - too few to check Pinnacle against"
        return out
    if abs(mine - middle) > CROWD_PTS:
        out["why"] = (f"Pinnacle has the home side {mine:g}%, the US books' middle {middle:g}% - "
                      f"more than {CROWD_PTS:g} pts apart, so Pinnacle is probably stale: no bet")
        return out
    chances = true_chances(pin[:2])
    out["pinnacle"] = {"home": SHARP.american(pin[0]), "away": SHARP.american(pin[1])}
    for i, side in enumerate(("home", "away")):
        offers = []
        for key in US_BOOKS:
            p = h2h(event, key)
            if p:
                offers.append((p[i], key))
        if not offers:
            out["sides"][side] = {"why": "no US price"}
            continue
        best, at = max(offers)
        w = worth(best, chances, i)
        low = min(w.values())
        true_pct = round(min(v[i] for v in chances.values()) * 100, 1)
        row = {"price": SHARP.american(best), "decimal": best, "book": at, "books": len(offers),
               "worth": w, "worst_pct": low, "true_pct": true_pct,
               "true_by_method": {m: round(v[i] * 100, 2) for m, v in chances.items()}}
        mid = sum(v[i] for v in chances.values()) / len(chances) * 100
        if not TRUE_RANGE[0] <= mid <= TRUE_RANGE[1]:
            row["why"] = f"true chance {mid:.0f}% is outside {TRUE_RANGE[0]:g}-{TRUE_RANGE[1]:g}%"
        elif low > MAX_EDGE_PCT:
            row["why"] = f"worth {low:+.1f}% - more than {MAX_EDGE_PCT:g}% is a stale or wrong line"
        elif low < EDGE_PCT:
            row["why"] = f"best {row['price']:+d} at {at}: worth {low:+.2f}% at worst, the bar is +{EDGE_PCT:g}%"
        else:
            row["clears"] = True
            row["why"] = (f"{row['price']:+d} at {at} vs Pinnacle {out['pinnacle'][side]:+d}: worth {low:+.2f}% "
                          f"under every method (best {max(w.values()):+.2f}%)")
        out["sides"][side] = row
    ok = [(r["worst_pct"], s) for s, r in out["sides"].items() if r.get("clears")]
    if ok:
        out["bet"] = max(ok)[1]
        out["why"] = out["sides"][out["bet"]]["why"]
    else:
        out["why"] = "; ".join(f"{s}: {r.get('why')}" for s, r in out["sides"].items())
    return out


# ------------------------------------------------------------------ the key and the budget

def api_key():
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name].strip() or None
    if not CREDENTIALS.exists():
        return None
    try:
        env = dict(SHARP._env_reader()(CREDENTIALS))
    except Exception:
        return None
    for name in KEY_NAMES:
        if (env.get(name) or "").strip():
            return env[name].strip()
    return None


def usage(now):
    u = _read(USAGE, {}) or {}
    day = et_time.day(et_time.to_eastern(now))
    if u.get("day") != day:
        u = {"day": day, "calls": 0, "remaining": u.get("remaining"), "last_close": u.get("last_close") or {}}
    u.setdefault("last_close", {})
    return u


def may_spend(u, close=False):
    if u["calls"] >= DAILY_CALLS:
        return False
    rem = u.get("remaining")
    return rem is None or rem > (0 if close else RESERVE)


def fetch(sport, key, books):
    """(events, credits left) for one sport's moneylines at `books` - one credit."""
    return SHARP.call(f"/v4/sports/{SHARP.KEYS[sport]}/odds",
                      {"regions": "us", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso",
                       "bookmakers": ",".join(books)}, key=key)


def spend(u, sport, why, key, books, fetcher=None):
    """One call, counted. Returns the events, or None if it failed."""
    fetcher = fetcher or fetch
    try:
        events, left = fetcher(sport, key, books)
    except SHARP.ApiError as exc:
        log(f"{sport} ({why}): {exc}")
        return None
    u["calls"] += 1
    if left is not None:
        u["remaining"] = left
    _write(USAGE, u)
    return events if isinstance(events, list) else []


# ------------------------------------------------------------------ the games

def contexts():
    out = []
    for f in sorted(CTX.glob("*.json")):
        c = _read(f, None)
        if isinstance(c, dict) and c.get("sport") in SPORTS and c.get("event_id"):
            out.append(c)
    return out


def due(ctxs, now, already):
    """The games a run looks at: scheduled, starting NOT_WITHIN_MIN to
    LOOK_AHEAD_H from now, not bet already."""
    out = []
    for c in ctxs:
        start = parse_iso(c.get("start_utc"))
        if not start or c.get("status") not in (None, "STATUS_SCHEDULED") or str(c["event_id"]) in already:
            continue
        mins = (start - now).total_seconds() / 60
        if NOT_WITHIN_MIN <= mins <= LOOK_AHEAD_H * 60:
            out.append(c)
    return out


# ------------------------------------------------------------------ the book

def stop_reason(settled):
    lost = -sum(float(x.get("profit") or 0) for x in settled)
    if lost >= STOP_USD:
        return f"the Line Shopper has lost ${lost:,.2f} settled - its ${STOP_USD:,.0f} stop"
    return None


def placed_on(bets, day):
    return [x for x in bets if et_time.day(et_time.to_eastern(x.get("utc"))) == day]


def bet_fields(g, ctx, now):
    side = g["bet"]
    r = g["sides"][side]
    team = (ctx.get(side) or {}).get("abbr") or (ctx.get(side) or {}).get("name")
    return {"book": "shop", "sport": ctx["sport"], "event_id": str(ctx["event_id"]), "odds_id": g["odds_id"],
            "match": ctx.get("short"), "starts_utc": ctx.get("start_utc"), "selection": f"{team} ML", "side": side,
            "price": r["price"], "at_book": r["book"], "books_priced": r["books"],
            "pinnacle_price": g["pinnacle"][side], "novig_pct": r["true_pct"],
            "true_by_method": r["true_by_method"], "worth_by_method": r["worth"], "ev_pct": r["worst_pct"],
            "price_source": f"{r['book']} via The Odds API at {now.strftime(STAMP)} UTC", "why": r["why"]}


def pick(games, ctx_by_id, open_bets, settled, day, now):
    """The bets to place now: best worth first, one a game, up to DAILY_CAP a day."""
    why = stop_reason(settled)
    if why:
        return [], why
    room = DAILY_CAP - len(placed_on(open_bets + settled, day))
    if room <= 0:
        return [], f"the day's {DAILY_CAP} bets are placed"
    ok = sorted((g for g in games if g.get("bet")), key=lambda g: -g["sides"][g["bet"]]["worst_pct"])
    return [bet_fields(g, ctx_by_id[g["event_id"]], now) for g in ok[:room]], None


# ------------------------------------------------------------------ runs

def run(now=None, key=None, fetcher=None, dry=False):
    now = now or utc_now()
    day = et_time.day(et_time.to_eastern(now))
    b = book()
    lines = [f"settled: {x['selection']} {x['result']} {x['profit']:+,.2f}" for x in b.settle(BETS, None, BANK, now)]
    f = b.fold(b.events(BETS), STARTING_BANKROLL)
    open_bets, settled = f["open"], f["settled"]
    already = {str(x.get("event_id")) for x in open_bets + settled}
    todo = due(contexts(), now, already)
    key = key or api_key()
    u = usage(now)
    games = []
    room = DAILY_CAP - len(placed_on(open_bets + settled, day))
    if not todo:
        lines.append("no unbet game starts in the next 12 hours")
    elif stop_reason(settled):
        lines.append(stop_reason(settled))
    elif room <= 0:
        lines.append(f"the day's {DAILY_CAP} bets are placed - no credits spent")
    elif not key:
        lines.append(f"no second Odds API key ({KEY_NAMES[-1]}) - nothing priced")
    else:
        for sport in sorted({c["sport"] for c in todo}):
            if not may_spend(u):
                lines.append(f"budget: {u['calls']} call(s) today, {u.get('remaining')} credits left - {sport} skipped")
                continue
            events = spend(u, sport, "decision", key, (SHARP_BOOK,) + US_BOOKS, fetcher)
            if events is None:
                continue
            joined = 0
            for c in (c for c in todo if c["sport"] == sport):
                e = SHARP.join(c, events)
                if e:
                    joined += 1
                    games.append(judge_game(c, e, now))
            lines.append(f"{sport}: {len(events)} priced, {joined} of {sum(1 for c in todo if c['sport'] == sport)} "
                         f"games joined; {u.get('remaining')} credits left")
        ctx_by_id = {str(c["event_id"]): c for c in todo}
        bets, why = pick(games, ctx_by_id, open_bets, settled, day, now)
        for fields in bets:
            if dry:
                lines.append(f"would bet {fields['selection']} ({fields['match']}) {fields['price']:+d}: {fields['why']}")
            else:
                e = b.place(fields, BETS, BANK, now)
                lines.append(f"BET {e['selection']} ({e['match']}) {e['price']:+d} ${e['stake']:,.2f}: {e['why']}")
        if not bets:
            lines.append(f"no bet: {why or 'nothing worth +' + format(EDGE_PCT, 'g') + '% under every method'}")
    old = _read(BOARD, {}) or {}
    keep = [g for g in old.get("games", []) if old.get("day") == day and g["event_id"] not in {x["event_id"] for x in games}]
    _write(BOARD, {"asof_utc": now.strftime(STAMP), "day": day,
                   "games": sorted(keep + games, key=lambda g: str(g.get("starts_utc")))})
    return lines


def record_closes(open_bets, now, key=None, fetcher=None):
    """Pinnacle's price within CLOSE_MIN of each open bet's start: one call a
    sport, at most every CLOSE_GAP_MIN. The last one kept is the close."""
    soon = {}
    for x in open_bets:
        start = parse_iso(x.get("starts_utc"))
        if start and now < start <= now + timedelta(minutes=CLOSE_MIN):
            soon.setdefault(x["sport"], []).append(x)
    closes = _read(CLOSES, {}) or {}
    if not soon:
        return closes
    key = key or api_key()
    if not key:
        return closes
    u = usage(now)
    for sport, bets in sorted(soon.items()):
        last = parse_iso(u["last_close"].get(sport))
        if last and now - last < timedelta(minutes=CLOSE_GAP_MIN):
            continue
        if not may_spend(u, close=True):
            log(f"no credits for {sport}'s close")
            continue
        events = spend(u, sport, "close", key, (SHARP_BOOK,), fetcher)
        if events is None:
            continue
        u["last_close"][sport] = now.strftime(ISO)
        _write(USAGE, u)
        by_id = {e.get("id"): e for e in events}
        for x in bets:
            e = by_id.get(x.get("odds_id"))
            pin = h2h(e, SHARP_BOOK) if e else None
            if pin:
                closes[x["id"]] = {"at": now.strftime(STAMP), "home": SHARP.american(pin[0]),
                                   "away": SHARP.american(pin[1]), "home_dec": pin[0], "away_dec": pin[1]}
    _write(CLOSES, closes)
    return closes


def clv_of(bet, closes):
    """The price taken, against Pinnacle's close with its margin taken out:
    {method: worth in %} and the least of them - the same rule as the bet."""
    c = closes.get(bet.get("id"))
    if not c:
        return None, None
    i = 0 if bet.get("side") == "home" else 1
    d = book().decimal_odds(bet.get("price"))
    if d is None:
        return None, None
    w = worth(d, true_chances((c["home_dec"], c["away_dec"])), i)
    return round(min(w.values()), 2), w


def close(now=None, key=None, fetcher=None):
    now = now or utc_now()
    b = book()
    done = b.settle(BETS, None, BANK, now)
    open_bets = b.fold(b.events(BETS), STARTING_BANKROLL)["open"]
    record_closes(open_bets, now, key, fetcher)
    return [f"settled: {x['selection']} {x['result']} {x['profit']:+,.2f}" for x in done] + \
           [f"{len(open_bets)} Line Shopper bet(s) open"]


# ------------------------------------------------------------------ the owner's check

def book_ages(events, now):
    """One line a book: how many games it priced, and how old its price
    stamps were at this moment (the youngest, the middle, the oldest) - to
    see what The Odds API's stamps really look like."""
    out = ["  book          games   price age in minutes (youngest / middle / oldest)"]
    for k in (SHARP_BOOK,) + US_BOOKS:
        ages = [age_min(p[2], now) for p in (h2h(e, k) for e in events) if p and p[2]]
        if not ages:
            out.append(f"  {k:<13}{0:>5}   - no prices on this plan")
            continue
        out.append(f"  {k:<13}{len(ages):>5}   {min(ages)} / {round(median(ages))} / {max(ages)}")
    return out


def check(sport=None, key=None, fetcher=None, now=None, say=print):
    now = now or utc_now()
    key = key or api_key()
    if not key:
        say(f"no second Odds API key. Put it in with: python3 scripts/set-credential.py ace {KEY_NAMES[-1]}")
        return 1
    try:
        _sports, left = SHARP.call("/v4/sports/", {}, key=key)
    except SHARP.ApiError as exc:
        say(f"the key did not work: {exc}")
        return 1
    say(f"key works: {left} credit(s) left this month (the sports list is free)")
    if not sport:
        say(f"add a sport ({', '.join(SPORTS)}) to spend 1 credit and see the books and the edges")
        return 0
    u = usage(now)
    events = spend(u, sport, "check", key, (SHARP_BOOK,) + US_BOOKS, fetcher)
    if events is None:
        return 1
    say(f"{sport}: {len(events)} game(s) priced, {u.get('remaining')} credits left")
    for line in book_ages(events, now):
        say(line)
    ctxs = {}
    for c in contexts():
        if c["sport"] == sport:
            e = SHARP.join(c, events)
            if e:
                ctxs[c["event_id"]] = judge_game(c, e, now)
    say(f"  {len(ctxs)} joined to ESPN's games")
    for g in ctxs.values():
        c = g.get("crowd")
        if c and c["us_median_home_pct"] is not None and abs(c["pinnacle_home_pct"] - c["us_median_home_pct"]) > CROWD_PTS:
            say(f"    crowd check stops {g['match']}: Pinnacle {c['pinnacle_home_pct']:g}% home, "
                f"US books' middle {c['us_median_home_pct']:g}%")
    rows = sorted(((r["worst_pct"], g["match"], s, r) for g in ctxs.values()
                   for s, r in g["sides"].items() if "worst_pct" in r), key=lambda t: -t[0])
    for w, match, side, r in rows[:8]:
        say(f"    {match:<14}{side:<5}{r['price']:>+6d} at {r['book']:<12} worth {w:+.2f}% at worst"
            f"{'  <- clears' if r.get('clears') else ''}")
    return 0


# ------------------------------------------------------------------ the page

def board_json():
    """The Line Shopper as one more bettor on Ace's page: the same shape as
    ace-baselines.py's strategies (record, open, settled)."""
    b = book()
    f = b.fold(b.events(BETS), STARTING_BANKROLL)
    closes = _read(CLOSES, {}) or {}

    def row(x):
        clv, _w = clv_of(x, closes)
        return {"utc": x.get("utc"), "sport": x.get("sport"), "match": x.get("match"),
                "starts_utc": x.get("starts_utc"), "selection": f"{x.get('selection')} at {x.get('at_book')}",
                "side": x.get("side"), "price": x.get("price"), "novig_pct": x.get("novig_pct"),
                "stake": x.get("stake"), "why": x.get("why"), "result": x.get("result"),
                "profit": x.get("profit"), "clv_pct": clv}
    settled = [row(x) for x in f["settled"]]
    real = [x for x in settled if x["result"] in ("won", "lost")]
    graded = [x for x in real if x["clv_pct"] is not None]
    u = _read(USAGE, {}) or {}
    return {"describe": DESCRIBE,
            "record": {"bets": len(f["open"]) + len(f["settled"]), "settled": len(settled),
                       "won": sum(1 for x in real if x["result"] == "won"), "profit": f["profit"],
                       "avg_clv_pct": round(sum(x["clv_pct"] for x in graded) / len(graded), 2) if graded else None,
                       "beat_close_pct": round(sum(1 for x in graded if x["clv_pct"] > 0) / len(graded) * 100, 1)
                       if graded else None,
                       "bankroll": f["bankroll"], "credits_left": u.get("remaining"),
                       "stopped": stop_reason(f["settled"])},
            "open": sorted((row(x) for x in f["open"]), key=lambda x: str(x["starts_utc"])),
            "settled": sorted(settled, key=lambda x: str(x["starts_utc"]), reverse=True)[:40],
            "board": _read(BOARD, {})}


def show_lines():
    d = board_json()
    r = d["record"]
    out = [f"Line Shopper: {r['bets']} bet(s), {r['settled']} settled, {r['won']} won, ${r['profit']:+,.2f}; "
           f"bankroll ${r['bankroll']:,.2f}; credits left {r['credits_left']}"]
    if r["avg_clv_pct"] is not None:
        out.append(f"vs Pinnacle's close (worst method): {r['avg_clv_pct']:+.2f}% a bet, beat it {r['beat_close_pct']}%")
    if r["stopped"]:
        out.append("STOPPED: " + r["stopped"])
    for x in d["open"]:
        out.append(f"  open  {x['selection']} ({x['match']}) {x['price']:+d}: {x['why']}")
    for x in d["settled"][:10]:
        out.append(f"  {x['result']:<5} {x['selection']} ({x['match']}) {x['price']:+d} {float(x['profit'] or 0):+,.2f}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Line Shopper: Pinnacle's true odds vs nine US books")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry", action="store_true")
    sub.add_parser("close")
    c = sub.add_parser("check")
    c.add_argument("sport", nargs="?", choices=SPORTS)
    sub.add_parser("board")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        print("\n".join(run(dry=a.dry)))
    elif a.cmd == "close":
        print("\n".join(close()))
    elif a.cmd == "check":
        return check(a.sport)
    elif a.cmd == "board":
        print(json.dumps(board_json()))
    else:
        print("\n".join(show_lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
