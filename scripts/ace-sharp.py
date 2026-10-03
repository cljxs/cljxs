#!/usr/bin/env python3
"""
ace-sharp.py - Pinnacle's price beside DraftKings', from The Odds API.

    python3 scripts/ace-sharp.py check          is the key there and working? (free: no credit used)
    python3 scripts/ace-sharp.py check nfl      spend 1 credit: fetch NFL, show how the games join
    python3 scripts/ace-sharp.py budget         credits used today, and what is left this month

WHY. Everything Ace was measured against was one book: DraftKings, through
ESPN. "Beat the closing line" meant beat DraftKings' close, which you can do
while still being on the wrong side of the market. Pinnacle is the sharp
book - high limits, low margin, it moves first - so its no-vig price is the
better estimate of the truth, and a DraftKings price that has not followed
it yet is the one realistic edge a slow bettor has (the review's B3).

THE BUDGET. The free plan is 500 credits a month. One call is one sport,
h2h (moneyline) only, Pinnacle and DraftKings together: 1 credit. So:

  * a DECISION snapshot per sport per day, at the first fetch from
    DECISION_HOUR_ET (2:30pm ET, before the 3pm betting wake);
  * a CLOSE snapshot per sport from the 5-minute close, when a game starts
    within CLOSE_MINUTES and that sport has none in the last CLOSE_GAP_MINUTES;
  * never more than DAILY_CAP a day, and never below RESERVE remaining.

THE KEY lives in agents/ace/state/credentials.env as ODDS_API_KEY, put there
with scripts/set-credential.py. It goes in the request URL (the API's design)
and is never printed: errors are reported by status code only.

THE JOIN to ESPN's games is by start time (within 3 hours) and team names,
normalised. DraftKings' price is fetched alongside on purpose: where both
sides carry it, the two DraftKings prices should agree, and `check` says
when they do not - a wrong join would show up there first.

Standard library only.
"""

import importlib.util
import json
import os
import re
import ssl
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
STATE = ROOT / "agents" / "ace" / "state"
CREDENTIALS = STATE / "credentials.env"
USAGE = STATE / "sharp-usage.json"

HOST = "https://api.the-odds-api.com"
KEYS = {"nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf",
        "nba": "basketball_nba", "mlb": "baseball_mlb"}
BOOKS = "pinnacle,draftkings"
SHARP = "pinnacle"
TIMEOUT = 20
DECISION_HOUR_ET = 14.5
CLOSE_MINUTES = 20
CLOSE_GAP_MINUTES = 25
DAILY_CAP = 14
RESERVE = 10
JOIN_HOURS = 3.0


def log(msg):
    print(f"[ace-sharp {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def _env_reader():
    spec = importlib.util.spec_from_file_location("emily_assets", SCRIPTS / "emily-assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.read_env_file


def api_key():
    if os.environ.get("ODDS_API_KEY"):
        return os.environ["ODDS_API_KEY"].strip()
    if not CREDENTIALS.exists():
        return None
    try:
        return (dict(_env_reader()(CREDENTIALS)).get("ODDS_API_KEY") or "").strip() or None
    except Exception:
        return None


class ApiError(Exception):
    pass


def call(path, params, key=None):
    """(parsed JSON, remaining credits or None). The key is added here and
    nowhere else, and an error names the status, never the URL."""
    key = key or api_key()
    if not key:
        raise ApiError("no ODDS_API_KEY - put it in with: python3 scripts/set-credential.py ace ODDS_API_KEY")
    q = "&".join(f"{k}={v}" for k, v in dict(params, apiKey=key).items())
    req = urllib.request.Request(f"{HOST}{path}?{q}", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
            left = r.headers.get("x-requests-remaining")
            return json.loads(r.read()), (int(float(left)) if left not in (None, "") else None)
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read()).get("error_code") or ""
        except Exception:
            why = ""
        raise ApiError(f"HTTP {e.code} {why}".strip()) from None
    except urllib.error.URLError as e:
        raise ApiError(f"network: {e.reason}") from None


# ------------------------------------------------------------------ prices

def novig(home_dec, away_dec):
    """No-vig chances in percent from two decimal prices, or (None, None)."""
    try:
        ph, pa = 1 / float(home_dec), 1 / float(away_dec)
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None
    t = ph + pa
    return round(ph / t * 100, 1), round(pa / t * 100, 1)


def american(dec):
    try:
        d = float(dec)
    except (TypeError, ValueError):
        return None
    if d <= 1:
        return None
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def book_prices(event, book):
    """(home decimal, away decimal) for one bookmaker's h2h market, or None."""
    for b in event.get("bookmakers") or []:
        if b.get("key") != book:
            continue
        for m in b.get("markets") or []:
            if m.get("key") != "h2h":
                continue
            by = {o.get("name"): o.get("price") for o in m.get("outcomes") or []}
            h, a = by.get(event.get("home_team")), by.get(event.get("away_team"))
            if h and a:
                return h, a, b.get("last_update")
    return None


def norm(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def same_team(espn_name, odds_name):
    a, b = norm(espn_name), norm(odds_name)
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta) >= 2 and len(tb) >= 2 and (ta <= tb or tb <= ta)


def parse_iso(s):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return datetime.strptime(str(s or ""), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def join(context, odds_events):
    """The Odds API event for one ESPN context, or None."""
    start = parse_iso(context.get("start_utc"))
    home = (context.get("home") or {}).get("name")
    away = (context.get("away") or {}).get("name")
    hits = []
    for e in odds_events or []:
        t = parse_iso(e.get("commence_time"))
        if start and t and abs((t - start).total_seconds()) > JOIN_HOURS * 3600:
            continue
        if same_team(home, e.get("home_team")) and same_team(away, e.get("away_team")):
            hits.append(e)
    return hits[0] if len(hits) == 1 else None


def attach(context, event, now=None):
    """Write Pinnacle's no-vig chances (and DraftKings', for the check) into
    the context as "sharp". True if it did."""
    p = book_prices(event, SHARP)
    if not p:
        return False
    h, a = novig(p[0], p[1])
    sharp = {"book": SHARP, "novig_home_pct": h, "novig_away_pct": a,
             "home_price": american(p[0]), "away_price": american(p[1]),
             "last_update": p[2], "at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    dk = book_prices(event, "draftkings")
    if dk:
        sharp["draftkings_home_price"], sharp["draftkings_away_price"] = american(dk[0]), american(dk[1])
    context["sharp"] = sharp
    return True


# ------------------------------------------------------------------ the budget

def _usage(now):
    try:
        u = json.loads(USAGE.read_text())
    except Exception:
        u = {}
    day = et_time.day(et_time.to_eastern(now))
    if u.get("day") != day:
        u = {"day": day, "calls": 0, "remaining": u.get("remaining"), "last": u.get("last") or {},
             "decision_done": []}
    return u


def _save(u):
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    USAGE.write_text(json.dumps(u, indent=1) + "\n")


def may_spend(u):
    if u["calls"] >= DAILY_CAP:
        return False
    rem = u.get("remaining")
    return rem is None or rem > RESERVE


def wanted(contexts, close, now, u):
    """Which sports to fetch now, and why."""
    et = et_time.to_eastern(now)
    hour = et.hour + et.minute / 60
    out = {}
    for c in contexts:
        sport = c.get("sport")
        if sport not in KEYS:
            continue
        start = parse_iso(c.get("start_utc"))
        if start is None or start <= now:
            continue
        mins = (start - now).total_seconds() / 60
        if close:
            last = parse_iso((u.get("last") or {}).get(sport))
            if mins <= CLOSE_MINUTES and (last is None or now - last > timedelta(minutes=CLOSE_GAP_MINUTES)):
                out[sport] = "close"
        elif hour >= DECISION_HOUR_ET and sport not in u["decision_done"] and mins <= 12 * 60:
            out[sport] = "decision"
    return out


def refresh(contexts, close=False, now=None, fetch=None):
    """Fetch the sports due now, within budget, and attach Pinnacle's price
    to every context that joins. Returns the ids of the contexts changed."""
    now = now or datetime.now(timezone.utc)
    fetch = fetch or (lambda sport: call(f"/v4/sports/{KEYS[sport]}/odds",
                                         {"regions": "eu", "markets": "h2h", "oddsFormat": "decimal",
                                          "bookmakers": BOOKS}))
    u = _usage(now)
    changed = set()
    for sport, why in sorted(wanted(contexts, close, now, u).items()):
        if not may_spend(u):
            log(f"budget: {u['calls']} call(s) today, {u.get('remaining')} left - {sport} skipped")
            break
        try:
            events, left = fetch(sport)
        except ApiError as exc:
            log(f"{sport}: {exc}")
            continue
        u["calls"] += 1
        u["remaining"] = left if left is not None else u.get("remaining")
        u.setdefault("last", {})[sport] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if why == "decision":
            u["decision_done"].append(sport)
        joined = 0
        for c in contexts:
            if c.get("sport") == sport:
                e = join(c, events)
                if e and attach(c, e, now):
                    changed.add(id(c))
                    joined += 1
        log(f"{sport} ({why}): {len(events)} priced, {joined} joined to ESPN games; {u.get('remaining')} credits left")
    _save(u)
    return changed


# ------------------------------------------------------------------ commands

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Pinnacle's price from The Odds API")
    ap.add_argument("cmd", nargs="?", default="check", choices=["check", "budget"])
    ap.add_argument("sport", nargs="?", choices=sorted(KEYS), help="check only: spend 1 credit on this sport")
    a = ap.parse_args(argv)
    cmd, argv = a.cmd, [a.cmd] + ([a.sport] if a.sport else [])
    if cmd == "budget":
        u = _usage(datetime.now(timezone.utc))
        print(f"{u['calls']} call(s) today (cap {DAILY_CAP}), {u.get('remaining')} credits left this month")
        return 0
    if not api_key():
        print("no ODDS_API_KEY yet. Put it in with:\n  python3 scripts/set-credential.py ace ODDS_API_KEY")
        return 1
    try:
        sports, left = call("/v4/sports/", {})
    except ApiError as exc:
        print(f"the key did not work: {exc}")
        return 1
    active = {s.get("key") for s in sports if s.get("active")}
    print(f"key works. {left} credits left this month.")
    for ours, theirs in KEYS.items():
        print(f"  {ours:<4} {theirs:<24} {'in season' if theirs in active else 'not in season'}")
    if len(argv) < 2:
        print("\n`ace-sharp.py check nfl` spends 1 credit and shows how the games join.")
        return 0
    sport = argv[1]
    events, left = call(f"/v4/sports/{KEYS[sport]}/odds",
                        {"regions": "eu", "markets": "h2h", "oddsFormat": "decimal", "bookmakers": BOOKS})
    ctx_dir = ROOT / "agents" / "ace" / "data" / "context"
    contexts = []
    for f in sorted(ctx_dir.glob(f"{sport}-*.json")):
        try:
            contexts.append(json.loads(f.read_text()))
        except Exception:
            continue
    joined = 0
    for c in contexts:
        e = join(c, events)
        if not e or not attach(c, e):
            print(f"  NOT JOINED  {c.get('short')}  ({(c.get('away') or {}).get('name')} @ {(c.get('home') or {}).get('name')})")
            continue
        joined += 1
        s, o = c["sharp"], c.get("odds") or {}
        dk = (s.get("draftkings_home_price"), s.get("draftkings_away_price"))
        espn = (o.get("moneyline_home"), o.get("moneyline_away"))
        agree = "" if dk == (None, None) else ("  DK agrees" if dk == espn else f"  DK {dk} vs ESPN {espn}")
        print(f"  {c.get('short'):<14} Pinnacle {s['novig_home_pct']}/{s['novig_away_pct']}  "
              f"DraftKings {o.get('novig_home_pct')}/{o.get('novig_away_pct')}{agree}")
    print(f"{len(events)} priced by The Odds API, {joined} of {len(contexts)} ESPN games joined. {left} credits left.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
