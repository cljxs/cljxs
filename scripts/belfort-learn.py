#!/usr/bin/env python3
"""
belfort-learn.py - what Belfort's own results say, worked out in code.

    belfort-learn.py record [--json]           # his record, and what the names he passed on did next
    belfort-learn.py review [--month YYYY-MM] [--write]   # the monthly review for the owner
    belfort-learn.py replay [--trail 7] [--shadow] [--json]   # his trades under the sell rules now and before

Owner, 2026-10-07: "Does it learn from its wins and losses?" It did not.
Every wake is a new conversation, MEMORY.md holds a week of his own notes,
and nothing showed him his results. Three things here, all arithmetic, so
none of it is a model's recollection of how it has been doing:

1. His record: closed trades by the catalyst TYPE he bought on, the score he
   gave, the cluster and the regime. belfort-cycle.sh puts a few lines of it
   in every wake message.
2. His passes: every call in state/calls.jsonl, measured 5 and 10 trading
   days later against SPY. Passes outnumber buys about ten to one, so this is
   where evidence about his 7+ bar builds up fastest. Outcomes are kept in
   state/pass-outcomes.json because quotes.json only holds 90 closes.
3. The monthly review (belfort-review.timer, the 1st of each month): the
   record, the books against each other and the market, and suggestions -
   only for a group of MIN_SAMPLE or more, and only ever suggestions. Nothing
   here changes a rule. The owner decides; a run of five losses is luck, and
   rules that chase it are worse than rules that ignore it.
4. The replay (owner, 2026-10-22: "we are selling way lower than the
   highs"): every position he has held, walked day by day through real daily
   prices under the sell rules as they are and under the alternatives the
   owner is weighing. It decides nothing; it shows what each rule would have
   done on the same buys.

Standard library only.
"""

import argparse
import importlib.util
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402


def _trade():
    """belfort-trade.py, loaded by path - the hyphen keeps it from a normal
    import. Its book, calls, clusters and closed trades are read, never
    restated."""
    spec = importlib.util.spec_from_file_location("belfort_trade_learn",
                                                  Path(__file__).resolve().parent / "belfort-trade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _trade()
AGENT = T.ROOT / "agents" / "belfort"
OUTCOMES = AGENT / "state" / "pass-outcomes.json"
REVIEWS = AGENT / "reviews"
HORIZONS = (5, 10)          # trading days after a call
MIN_SAMPLE = 20             # the fewest in a group before a review suggests anything from it
EDGE_PCT = 1.0              # average lead over SPY, in 10 days, that counts as a pattern
BEAT_SHARE = 55.0           # ...and the share of them that must have beaten it
NO_AI_GAP_PCT = 5.0         # the no-AI book this far ahead, same days, is worth saying


def score_band(score):
    if score is None:
        return "unscored"
    s = int(score)
    return "9-10" if s >= 9 else str(s) if s >= 6 else "4-5" if s >= 4 else "0-3"


BAND_ORDER = ["0-3", "4-5", "6", "7", "8", "9-10", "unscored"]


def summary(rows, field):
    vals = [r[field] for r in rows if r.get(field) is not None]
    if not vals:
        return {"n": 0}
    won = sum(1 for v in vals if v > 0)
    return {"n": len(vals), "won": won, "win_rate": round(won / len(vals) * 100, 1),
            "avg_pct": round(sum(vals) / len(vals), 2)}


def grouped(rows, key, field):
    out = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return {k: summary(v, field) for k, v in out.items()}


def _index_on(hist, day):
    """The last bar on or before `day`, or None."""
    i = None
    for n, (d, _) in enumerate(hist):
        if d <= day:
            i = n
        else:
            break
    return i


def pass_outcomes(doc=None, calls=None):
    """{"SYM|day": outcome} for every name he passed on: what it did over
    HORIZONS trading days from his call, and SPY over the same days. The first
    call of a day stands for that day - he calls the same name at the open and
    the close, and counting both would count one move twice. Held names are
    not passes. Kept in OUTCOMES once measured; quotes.json forgets after 90
    trading days and the review looks back further."""
    doc = doc if doc is not None else T.quotes_doc()
    calls = T.calls_log() if calls is None else calls
    try:
        cache = json.loads(OUTCOMES.read_text())
    except Exception:
        cache = {}
    quotes = doc.get("quotes") or {}
    spy = [(d, float(c)) for d, c in ((doc.get("benchmarks") or {}).get("SPY") or {}).get("history") or []]
    seen, changed = set(), False
    for c in calls:
        sym, day = str(c.get("symbol") or "").upper(), str(c.get("utc") or "")[:10]
        key = f"{sym}|{day}"
        if c.get("held") or not day or key in seen:
            continue
        seen.add(key)
        row = dict(cache.get(key) or {"symbol": sym, "day": day, "score": c.get("score"),
                                 "cluster": T.cluster_of(sym), "wake": c.get("wake")})
        if all(row.get(f"vs_spy_{k}") is not None for k in HORIZONS):
            continue
        hist = [(d, float(v)) for d, v in (quotes.get(sym) or {}).get("history") or []]
        i, j = _index_on(hist, day), _index_on(spy, day)
        if i is None or j is None:
            cache.setdefault(key, row)
            continue
        base = float(c.get("price") or hist[i][1])
        spy_base = float(c.get("spy") or spy[j][1])
        for k in HORIZONS:
            if i + k < len(hist) and j + k < len(spy):
                ret = (hist[i + k][1] / base - 1) * 100
                sret = (spy[j + k][1] / spy_base - 1) * 100
                row[f"ret_{k}"], row[f"spy_{k}"] = round(ret, 2), round(sret, 2)
                row[f"vs_spy_{k}"] = round(ret - sret, 2)
        if cache.get(key) != row:
            cache[key], changed = row, True
    if changed:
        OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
        OUTCOMES.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    return cache


def record(p=None, outcomes=None, month=None):
    """His record. `month` ("2026-10") limits it to trades closed and calls
    made in that month."""
    p = p if p is not None else T.load()
    closed = T.closed_trades(p)
    passes = list((outcomes if outcomes is not None else pass_outcomes()).values())
    if month:
        closed = [c for c in closed if str(c.get("sold_utc") or "").startswith(month)]
        passes = [x for x in passes if str(x.get("day") or "").startswith(month)]
    measured = [x for x in passes if x.get("vs_spy_10") is not None]
    return {
        "closed": summary(closed, "pct"),
        "by_catalyst": grouped(closed, lambda r: r["catalyst_type"], "pct"),
        "by_score": grouped(closed, lambda r: score_band(r["score"]), "pct"),
        "by_cluster": grouped(closed, lambda r: r["cluster"], "pct"),
        "by_regime": grouped(closed, lambda r: r.get("regime") or "unknown", "pct"),
        "passes": {"called": len(passes), "measured": len(measured),
                   "by_score": grouped(measured, lambda r: score_band(r.get("score")), "vs_spy_10"),
                   "by_cluster": grouped(measured, lambda r: r.get("cluster") or "?", "vs_spy_10")},
        "trades": closed[-15:][::-1],
    }


def _row(groups, order=None, label=lambda k: k, won="won"):
    keys = sorted(groups, key=lambda k: (order.index(k) if order and k in order else 99, -groups[k]["n"], k))
    return " · ".join(f"{label(k)} {g['n']} ({g['won']} {won}, {g['avg_pct']:+.1f}%)" for k in keys
                      for g in [groups[k]] if g["n"])


def wake_lines(r):
    """The few lines of his record that go in every wake message."""
    c, ps = r["closed"], r["passes"]
    lines = [f"Your record, worked out by code (a group under {MIN_SAMPLE} is mostly luck - it informs, it does not change the rules):"]
    if not c["n"]:
        lines.append("  closed trades: none yet")
    else:
        lines.append(f"  closed trades: {c['n']}, {c['won']} won ({c['win_rate']:g}%), average {c['avg_pct']:+.1f}%")
        lines.append("  by catalyst: " + _row(r["by_catalyst"]))
        lines.append("  by your score: " + _row(r["by_score"], BAND_ORDER, lambda k: f"{k}:"))
    if ps["measured"]:
        lines.append("  names you passed on, 10 trading days later against SPY: "
                     + _row(ps["by_score"], BAND_ORDER, lambda k: f"scored {k}:", "beat it"))
    else:
        lines.append(f"  names you passed on: {ps['called']} so far, each measured 10 trading days after your call")
    return lines


def suggestions(r, same=None):
    """What the evidence says, only where a group has MIN_SAMPLE or more.
    Suggestions for the owner - none of them is applied by anything."""
    out = []
    for typ, g in r["by_catalyst"].items():
        if g["n"] >= MIN_SAMPLE and g["avg_pct"] < 0:
            out.append(f"**{typ}** buys have lost on average: {g['n']} closed, {g['win_rate']:g}% won, "
                       f"{g['avg_pct']:+.1f}% a trade. Consider requiring a score of 8+ for {typ}, or not buying on it.")
    g = r["by_score"].get("7") or {"n": 0}
    if g["n"] >= MIN_SAMPLE and g["avg_pct"] < 0:
        out.append(f"Buys he scored **7** have lost on average ({g['n']} closed, {g['avg_pct']:+.1f}% a trade). "
                   f"Consider raising the bar to 8.")
    for band in ("6", "4-5"):
        g = r["passes"]["by_score"].get(band) or {"n": 0}
        if g["n"] >= MIN_SAMPLE and g["avg_pct"] >= EDGE_PCT and g["win_rate"] >= BEAT_SHARE:
            out.append(f"Names he scored **{band}** and passed on went on to beat SPY by {g['avg_pct']:+.1f}% in "
                       f"10 trading days ({g['n']} calls, {g['win_rate']:g}% beat it). The 7+ bar may be stricter "
                       f"than it needs to be.")
    traded = {k for k, g in r["by_cluster"].items() if g["n"]}
    for cl, g in r["passes"]["by_cluster"].items():
        if g["n"] >= MIN_SAMPLE and g["avg_pct"] >= EDGE_PCT and g["win_rate"] >= BEAT_SHARE and cl not in traded:
            out.append(f"He passes on **{cl}** names that go on to beat SPY ({g['n']} calls, {g['avg_pct']:+.1f}% "
                       f"in 10 days, {g['win_rate']:g}% beat it) and has bought none. Worth asking why.")
    if same and same.get("belfort_pct") is not None and r["closed"]["n"] >= MIN_SAMPLE \
            and same["shadow_pct"] - same["belfort_pct"] >= NO_AI_GAP_PCT:
        out.append(f"The no-AI book is ahead by {same['shadow_pct'] - same['belfort_pct']:.1f} points over the same "
                   f"days since {same['since']}: so far his judgement is costing money, not making it.")
    return out


def largest_group(r):
    groups = [g["n"] for key in ("by_catalyst", "by_score", "by_cluster") for g in r[key].values()]
    groups += [g["n"] for g in r["passes"]["by_score"].values()] + [g["n"] for g in r["passes"]["by_cluster"].values()]
    return max(groups or [0])


def _table(title, groups, order=None, what="trades", field="average"):
    if not groups:
        return f"**{title}:** none yet.\n"
    keys = sorted(groups, key=lambda k: (order.index(k) if order and k in order else 99, -groups[k]["n"], k))
    rows = [f"| {k} | {groups[k]['n']} | {groups[k]['win_rate']:g}% | {groups[k]['avg_pct']:+.2f}% |"
            for k in keys if groups[k]["n"]]
    return (f"**{title}**\n\n| | {what} | {'won' if what == 'trades' else 'beat SPY'} | {field} |\n"
            f"|---|---|---|---|\n" + "\n".join(rows) + "\n")


def last_month(now=None):
    now = now or et_time.eastern_now()
    return (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def review(month=None):
    """The monthly review as markdown: the month, the whole record, the books,
    and suggestions with the evidence behind each."""
    month = month or last_month()
    outcomes = pass_outcomes()
    p = T.load()
    whole, this = record(p, outcomes), record(p, outcomes, month)
    books = [T.book_stats(p, "belfort")]
    if T.SHADOW.exists():
        books.append(T.book_stats(T.load(T.SHADOW), "shadow"))
    same = T.same_days(books)
    fmt = lambda v: "-" if v is None else f"{v:+.2f}%"
    tips = suggestions(whole, same)
    c, mc = whole["closed"], this["closed"]
    lines = [
        f"# Belfort - review of {month}",
        "",
        f"Written by code on {et_time.eastern_now().strftime('%Y-%m-%d')} from his trades and his calls. Nothing here "
        f"changes a rule: to adopt a suggestion, ask for that change in a Claude session.",
        "",
        "## The month",
        "",
        (f"{mc['n']} trade(s) closed, {mc['won']} won, {mc['avg_pct']:+.2f}% on average." if mc["n"]
         else "No trades closed this month."),
        f"{this['passes']['called']} pass(es) called, {this['passes']['measured']} measured 10 trading days on so far.",
        "",
        "## The books",
        "",
    ]
    for b in books:
        name = "Belfort (AI)" if b["book"] == "belfort" else "No-AI book"
        lines.append(f"- **{name}** since {b['since']}: {fmt(b['return_pct'])}. QQQ {fmt(b['qqq_return_pct'])}, "
                     f"SPY {fmt(b['spy_return_pct'])} over the same time. Worst drop {b['max_drawdown_pct']:.2f}%.")
    for name, b in (("Belfort", books[0]), ("No-AI book", books[1] if len(books) > 1 else None)):
        t = (b or {}).get("trim_test")
        if t:
            better = "half, then the stop" if t["difference"] > 0 else "selling it all at +25%"
            lines.append(f"- **Take profit, {name}:** {t['positions']} trimmed position(s) closed. Half at +25% "
                         f"and the rest on its stop made ${t['actual']:,.2f}; selling all at +25% would have "
                         f"made ${t['all_at_trim']:,.2f} ({t['difference']:+,.2f}) - {better} did better.")
    if same:
        lines.append(f"- **Same days** since {same['since']}: Belfort {fmt(same['belfort_pct'])}, no-AI "
                     f"{fmt(same['shadow_pct'])}, QQQ {fmt(same['qqq_pct'])}, SPY {fmt(same.get('spy_pct'))}.")
    lines += [
        "",
        "## Suggestions",
        "",
        *([f"{n}. {t}" for n, t in enumerate(tips, 1)] or [
            f"None. A suggestion needs at least {MIN_SAMPLE} in a group, and the largest so far has "
            f"{largest_group(whole)}. With one or two decisions a day that takes a while - fewer would be "
            f"reading luck as a pattern."]),
        "",
        "## His whole record",
        "",
        (f"{c['n']} closed trade(s), {c['won']} won ({c['win_rate']:g}%), {c['avg_pct']:+.2f}% on average."
         if c["n"] else "No closed trades yet."),
        "",
        _table("By the catalyst he bought on", whole["by_catalyst"]),
        _table("By the score he gave", whole["by_score"], BAND_ORDER),
        _table("By cluster", whole["by_cluster"]),
        _table("By market regime when he bought", whole["by_regime"]),
        _table("Names he passed on, 10 trading days later, by his score", whole["passes"]["by_score"],
               BAND_ORDER, "calls", "vs SPY"),
        _table("Names he passed on, by cluster", whole["passes"]["by_cluster"], None, "calls", "vs SPY"),
    ]
    return "\n".join(lines) + "\n"


# --- the replay -------------------------------------------------------------
#
# Each position from its first buy to its last sale is walked through real
# daily bars, checking the sell rule - exit_check in belfort-trade.py, the one
# the book uses, never a copy - at the open, the day's low, its high and the
# close. A stop crossed during the day fills AT the stop; a gap through it
# fills at the open, as it does for real. The book checked hourly until
# 2026-10-22 and every 10 minutes since, so real fills land a little under
# the stop: the rule a sale was made under, replayed, is how far to trust
# the other columns.
# His own judgement sales happen on their real day at their real price, if
# the replayed position still holds shares then. A position added to is
# replayed at its average cost from the first buy.

# (column, settings for exit_check) - an empty one is the book's own rules.
PLANS = (("Rules now", {}),
         ("Old rules", {"trail": "own", "take": 25.0}))   # trail = the stop's own distance
RULES_CHANGED = T.RULES_CHANGED


def _fetch():
    spec = importlib.util.spec_from_file_location("belfort_fetch_learn",
                                                  Path(__file__).resolve().parent / "belfort-fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def daily_bars(sym, get=None):
    """[(day, open, high, low, close)] for a year, from the chart the fetcher
    already reads."""
    f = _fetch()
    raw = json.loads((get or f.http_get)(f.CHART_URL.format(sym=sym)))
    r = raw["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for ts, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]):
        if None not in (h, l, c):
            day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
            out.append((day, float(o if o is not None else c), float(h), float(l), float(c)))
    return out


def lots(p):
    """Every position from its first buy until it was sold out, or still open."""
    held, out = {}, []
    for t in p.get("trades", []):
        sym, side = str(t.get("symbol", "")).upper(), str(t.get("side", "")).upper()
        if side == "BUY":
            held.setdefault(sym, {"symbol": sym, "buys": [], "sells": []})["buys"].append(t)
        elif side == "SELL" and sym in held:
            lot = held[sym]
            lot["sells"].append(t)
            if sum(float(x["shares"]) for x in lot["sells"]) >= sum(float(x["shares"]) for x in lot["buys"]):
                out.append(held.pop(sym))
    out += held.values()
    for lot in out:
        lot["shares"] = sum(float(x["shares"]) for x in lot["buys"])
        lot["cost"] = sum(float(x["shares"]) * float(x["price"]) for x in lot["buys"]) / lot["shares"]
        lot["bought"] = str(lot["buys"][0].get("utc") or "")[:10]
        lot["open"] = sum(float(x["shares"]) for x in lot["sells"]) < lot["shares"]
    return out


def own_distance(lot, p):
    """The stop distance the book used for this position: stored on an open
    one, and on its buy since 2026-10-22; for an older closed one, read back
    from the sale its rule wrote. None means the -10% every older position had."""
    pos = next((x for x in p.get("positions", []) if x["symbol"].upper() == lot["symbol"]), None)
    if lot["open"] and pos and pos.get("risk_pct"):
        return float(pos["risk_pct"])
    if lot["buys"][0].get("risk_pct"):
        return float(lot["buys"][0]["risk_pct"])
    for t in lot["sells"]:
        why = str(t.get("reason") or "")
        m = re.search(r"its stop: -([\d.]+)%", why)
        if m:
            return float(m.group(1))
        m = re.search(r"\(high \$([\d,.]+)\).*its stop \$([\d,.]+)", why)
        # Until RULES_CHANGED the trail WAS the stop's own distance; after
        # it, a protect sale says only TRAIL, which is not the stop.
        if m and str(t.get("utc") or "") < RULES_CHANGED:
            high, stop = (float(x.replace(",", "")) for x in m.groups())
            if stop > lot["cost"] * 1.001:          # a stop at cost says nothing about the trail
                return round((1 - stop / high) * 100, 2)
    return None


def real_result(lot, bars):
    pnl = sum(float(t.get("realised_pnl") or 0) for t in lot["sells"])
    left = lot["shares"] - sum(float(t["shares"]) for t in lot["sells"])
    if left > 0 and bars:
        pnl += (bars[-1][4] - lot["cost"]) * left
    last = str(lot["sells"][-1].get("utc") or "")[:10] if lot["sells"] and left <= 0 else None
    return round(pnl, 2), last


def replay_lot(lot, bars, qqq, distance=None, rules=None):
    """(profit, day it was sold out or None while still held) for one position
    under one set of rules (exit_check's trail and take)."""
    rules = rules or {}
    cb, shares, entry = lot["cost"], lot["shares"], lot["bought"]
    pos = {"symbol": lot["symbol"], "cost_basis": cb, "shares": shares, "entry_utc": entry,
           "risk_pct": distance}
    judged = {}
    for t in lot["sells"]:
        if not any(str(t.get("reason", "")).startswith(pre) for pre, _ in T.RULE_NAMES):
            judged.setdefault(str(t.get("utc"))[:10], []).append(t)
    qqq_close = {d: c for d, _o, _h, _l, c in qqq}
    pnl, prior, out = 0.0, [], None

    def sell(n, px):
        nonlocal shares, pnl
        n = min(n, shares)
        pnl += (px - cb) * n
        shares -= n
        pos["shares"] = shares

    for day, o, h, l, c in bars:
        if day < entry:
            continue
        if day == entry:
            prior.append([day, c])
            continue
        for kind, px in (("open", o), ("low", l), ("high", h), ("close", c)):
            if kind == "close":
                for t in judged.get(day, []):
                    sell(float(t["shares"]), float(t["price"]))
            if shares <= 0:
                break
            mkt = None
            if kind == "close" and day in qqq_close:
                mkt = {"price": qqq_close[day], "history": [[d, x] for d, x in qqq_close.items() if d <= day]}
            q = {"price": px, "history": prior + [[day, px]]}
            rule, _why, stop = T.exit_check(pos, q, mkt, **rules)
            if rule in ("stop", "protect"):
                sell(shares, px if kind in ("open", "close") else stop)
            elif rule in ("trim", "take"):
                fill = px if kind == "open" else round(cb * (1 + rules.get("take", T.TAKE_PROFIT) / 100), 2)
                if rule == "trim":
                    sell(int(shares) // 2, fill)
                    pos["trimmed"] = True
                else:
                    sell(shares, fill)
            elif rule == "dead":
                sell(shares, px)
        prior.append([day, c])
        if shares <= 0:
            out = day
            break
    if shares > 0 and bars:
        pnl += (bars[-1][4] - cb) * shares
    return round(pnl, 2), out


def saved_bars(path=None):
    """{symbol: [(day, open, high, low, close)]} from data/bars.json, the 90
    daily bars belfort-fetch.py already keeps for every watched name and QQQ -
    read first, so a replay asks Yahoo only for what is not there."""
    try:
        doc = json.loads((path or T.BARS).read_text())
    except Exception:
        return {}
    return {sym: [(b[0], b[1], b[2], b[3], b[4]) for b in (c.get("bars") or []) if None not in b[1:5]]
            for sym, c in (doc.get("bars") or {}).items()}


def replay(p=None, plans=PLANS, get_bars=None, saved=None):
    """Every position, real against each plan."""
    p = p or T.load()
    saved = saved_bars() if saved is None else saved
    every = lots(p)
    first = {}
    for lot in every:
        first[lot["symbol"]] = min(first.get(lot["symbol"], lot["bought"]), lot["bought"])
    first["QQQ"] = min(first.values(), default="")

    def fetched(sym):
        time.sleep(0.5)                     # Yahoo refuses quick runs of calls
        return (get_bars or daily_bars)(sym)

    cache = {}

    def bars_of(sym):
        if sym not in cache:
            have = saved.get(sym) or []
            if have and have[0][0] < first.get(sym, ""):
                cache[sym] = have
            else:
                try:
                    cache[sym] = fetched(sym)
                except Exception as exc:
                    print(f"replay: no prices for {sym} - {exc}", file=sys.stderr)
                    cache[sym] = []
        return cache[sym]

    qqq = bars_of("QQQ")
    rows, skipped = [], []
    for lot in sorted(every, key=lambda x: x["bought"]):
        bars = bars_of(lot["symbol"])
        if not bars:
            skipped.append(lot["symbol"])
            continue
        dist = own_distance(lot, p)
        pnl, last = real_result(lot, bars)
        row = {"symbol": lot["symbol"], "bought": lot["bought"], "cost": round(lot["cost"], 2),
               "shares": lot["shares"], "open": lot["open"], "distance": dist,
               "real": {"pnl": pnl, "out": last}}
        for name, rules in plans:
            r_pnl, r_out = replay_lot(lot, bars, qqq, dist, rules)
            row[name] = {"pnl": r_pnl, "out": r_out}
        rows.append(row)
    total = {k: round(sum(r[k]["pnl"] for r in rows), 2) for k in ["real"] + [n for n, _r in plans]}
    return {"rows": rows, "total": total, "plans": [n for n, _r in plans], "skipped": skipped,
            "last_day": max((b[-1][0] for b in cache.values() if b), default=None)}


def replay_text(r):
    def short(day):
        return datetime.strptime(day, "%Y-%m-%d").strftime("%b %d") if day else "held"

    cols = ["real"] + r["plans"]
    head = ["Real"] + r["plans"]
    out = [f"Belfort's positions replayed on real daily prices, up to {r['last_day']}.",
           "Each cell: profit, then the day it was sold ('held': still held, at the last close).", "",
           f"{'':16}" + "".join(f"{h:>13}" for h in head)]
    for row in r["rows"]:
        tag = f"{row['symbol']} {short(row['bought'])}"
        out.append(f"{tag:16}" + "".join(f"{row[c]['pnl']:>+13,.2f}" for c in cols))
        out.append(f"{'':16}" + "".join(f"{short(row[c]['out']):>13}" for c in cols))
    out.append(f"{'TOTAL':16}" + "".join(f"{r['total'][c]:>+13,.2f}" for c in cols))
    if r.get("skipped"):
        out.append(f"NOT IN THE TOTAL - no prices could be read for: {', '.join(r['skipped'])}. "
                   "Run it again in a few minutes.")
    out += ["", f"Rules now = the sell rules since {RULES_CHANGED}: half sold at +{T.TAKE_PROFIT:g}%, and once",
            f"up {T.PROTECT_AT:g}% the stop follows the best close {T.TRAIL:g}% below it. Old rules (until",
            "Oct 22) = half at +25%, the trail the stop's own distance (10%, or 5-15%). For a",
            "sale made before then, Old rules' gap to Real is the replay's own error",
            "(checks were hourly then; real stops filled a little under). Nothing here",
            "changes a rule."]
    return "\n".join(out)


def cmd_replay(a):
    plans = PLANS + (((f"{a.trail:g}% trail", {"trail": a.trail}),) if a.trail else ())
    p = T.load(T.SHADOW if a.shadow else None)
    r = replay(p, plans)
    print(json.dumps(r, indent=1) if a.json else replay_text(r))
    return 0


def reviews():
    files = sorted(REVIEWS.glob("*.md"), reverse=True) if REVIEWS.is_dir() else []
    return [{"name": f.name, "text": f.read_text(errors="replace")} for f in files[:24]]


def cmd_record(a):
    r = record()
    if a.json:
        print(json.dumps({**r, "wake_lines": wake_lines(r), "min_sample": MIN_SAMPLE,
                          "reviews": reviews()}, separators=(",", ":")))
    else:
        print("\n".join(wake_lines(r)))
    return 0


def cmd_review(a):
    if a.month and not (len(a.month) == 7 and a.month[4] == "-" and a.month.replace("-", "").isdigit()):
        print(f"--month {a.month!r}: write it as YYYY-MM", file=sys.stderr)
        return 2
    text = review(a.month)
    if a.write:
        REVIEWS.mkdir(parents=True, exist_ok=True)
        path = REVIEWS / f"{a.month or last_month()}.md"
        path.write_text(text)
        print(f"wrote {path.relative_to(T.ROOT)}")
    else:
        print(text)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Belfort's record and monthly review, worked out in code")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_record)
    v = sub.add_parser("review")
    v.add_argument("--month", help="YYYY-MM; the month before this one if left out")
    v.add_argument("--write", action="store_true", help="save it as reviews/<month>.md")
    v.set_defaults(fn=cmd_review)
    x = sub.add_parser("replay", help="his positions under the sell rules now and the alternatives")
    x.add_argument("--trail", type=float, help="also try this trailing distance, in percent")
    x.add_argument("--shadow", action="store_true", help="the no-AI book instead of Belfort's")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_replay)
    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
