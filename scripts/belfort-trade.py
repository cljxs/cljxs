#!/usr/bin/env python3
"""
belfort-trade.py — apply a buy or sell to the paper portfolio, in code.

Belfort decides WHAT to trade. This does the arithmetic, because arithmetic
is not a judgement call and a model doing it approximately is how $3,731.90
disappeared: three positions were closed by setting shares to 0 and the
proceeds were never added to cash. The account read -41% while the actual
trading loss was under 4%.

    belfort-trade.py buy  MRVL 8 --price 237.47 --reason "TYPE: the fact"
    belfort-trade.py sell MU   2 --price 927.60 --reason "stop loss -10%"
    belfort-trade.py show
    belfort-trade.py exits [--apply]   # the sell rules, in code; --apply sells what is due
    belfort-trade.py mark        # end of cycle: re-mark, bump cycle_count, stamp the file

Every trade is validated against the rules before it is applied:
  * you cannot spend cash you do not have
  * you cannot sell shares you do not hold
  * no single position may exceed 25% of portfolio value
  * at least 5% of the portfolio must stay in cash after a buy

A refused trade changes nothing and says why.

THE SELL RULES are code too (exit_check). They lived only in Belfort's
instructions until 2026-10-01, and the "+8%: protect it" rule could not be
followed at all: nothing remembered how high a position had been. The
fetcher now keeps each name's daily closes, and the highest close since
entry is that memory. belfort-cycle.sh runs `exits --apply` before Belfort
wakes; he is told what was sold and judges only a broken thesis.
Standard library only.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
STATE = ROOT / "agents" / "belfort" / "state" / "portfolio.json"
QUOTES = ROOT / "agents" / "belfort" / "data" / "quotes.json"

MAX_POSITION_PCT = 25.0
MIN_CASH_PCT = 5.0          # the owner lowered it from 15% on 2026-10-01

# The exit rules, in percent from cost.
STOP_LOSS = -10.0           # close, no exceptions, no averaging down
TAKE_PROFIT = 25.0          # close
PROTECT_AT = 8.0            # once a position has been up this much...
TRAIL = 10.0                # ...its stop follows the highest price, this far below,
                            #    and never below what was paid
DEAD_DAYS, DEAD_BAND = 5, 2.0   # 5+ trading days within +/-2%: dead money


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load():
    try:
        p = json.loads(STATE.read_text())
    except Exception as exc:
        print(f"cannot read {STATE}: {exc}", file=sys.stderr)
        sys.exit(2)
    p.setdefault("positions", [])
    p.setdefault("trades", [])
    p.setdefault("cash", 0.0)
    p.setdefault("starting_cash", 10000)
    return p


def save(p):
    p["last_cycle_utc"] = now()
    STATE.write_text(json.dumps(p, indent=2, sort_keys=True) + "\n")


def quote(symbol):
    try:
        return json.loads(QUOTES.read_text())["quotes"][symbol.upper()]
    except Exception:
        return None


def exit_check(pos, q):
    """(rule, reason, stop_price) for one open position against its quote;
    rule is None when nothing is due. stop_price is where it is sold: the
    -10% stop, or - once it has been up 8% - TRAIL below its highest price,
    never below what was paid."""
    cb = float(pos.get("cost_basis") or 0)
    if not q or not cb or q.get("price") is None:
        return None, "no quote", None
    px = float(q["price"])
    entry_day = str(pos.get("entry_utc") or "")[:10]
    # No known entry date: the closes say nothing about this position, and a
    # high from before it was bought must never sell it.
    history = (q.get("history") or []) if entry_day else []
    peak = max([float(c) for d, c in history if d >= entry_day] + [px])
    # Rounded before comparing: 90 against a cost of 100 is -9.999999999999998%
    # in floating point, and a stop at exactly -10% would never fire.
    pnl = round((px / cb - 1) * 100, 6)
    peak_pnl = round((peak / cb - 1) * 100, 6)
    stop = round(cb * (1 + STOP_LOSS / 100), 2)
    if peak_pnl >= PROTECT_AT:
        stop = round(max(cb, peak * (1 - TRAIL / 100)), 2)
    held = sum(1 for d, _ in history if d > entry_day)

    if pnl <= STOP_LOSS:
        return "stop", f"STOP LOSS: {pnl:+.1f}% from cost ${cb:,.2f}", stop
    if pnl >= TAKE_PROFIT:
        return "take", f"TAKE PROFIT: {pnl:+.1f}% from cost ${cb:,.2f}", stop
    if peak_pnl >= PROTECT_AT and px <= stop:
        return "protect", (f"PROTECT GAIN: was up {peak_pnl:+.1f}% (high ${peak:,.2f}), now {pnl:+.1f}%, "
                           f"at or under its stop ${stop:,.2f}"), stop
    if held >= DEAD_DAYS and abs(pnl) <= DEAD_BAND:
        return "dead", f"DEAD MONEY: {pnl:+.1f}% after {held} trading days", stop
    return None, f"{pnl:+.1f}%, stop ${stop:,.2f}", stop


def entry_of(p, pos):
    """The position with its entry date: its own, or its latest BUY's."""
    if pos.get("entry_utc"):
        return pos
    buys = [t.get("utc") for t in p.get("trades", [])
            if str(t.get("side", "")).upper() == "BUY" and str(t.get("symbol", "")).upper() == pos["symbol"].upper()]
    return dict(pos, entry_utc=max(buys)) if any(buys) else pos


def exits_due(p):
    """[(symbol, rule, reason)] for every open position whose exit is due."""
    out = []
    for pos in p["positions"]:
        if float(pos.get("shares") or 0) <= 0:
            continue
        rule, reason, _ = exit_check(entry_of(p, pos), quote(pos["symbol"]))
        if rule:
            out.append((pos["symbol"], rule, reason))
    return out


def last_price(symbol, fallback=None):
    """Prices come from the fetcher's file, never from an argument alone, so a
    trade cannot be booked at a price nobody observed."""
    try:
        q = json.loads(QUOTES.read_text())["quotes"][symbol.upper()]
        return float(q["price"])
    except Exception:
        return fallback


def book_gap(p):
    """Cash is a closed system:  starting - bought + sold == cash.

    Returns (gap, bought, sold, expected, actual). belfort-verify.py imports
    this rather than restating the formula - two copies that disagree would be
    worse than no check at all, because one of them would keep saying fine.
    """
    bought = sold = 0.0
    unknown = []
    for t in p.get("trades", []):
        side = str(t.get("side", "")).upper()
        notional = t.get("notional")
        notional = float(notional) if notional is not None else (
            float(t.get("shares") or 0) * float(t.get("price") or 0))
        if side == "BUY":
            bought += notional
        elif side == "SELL":
            sold += notional
        else:
            unknown.append(t)
    start = float(p.get("starting_cash") or 10000.0)
    expected = start - bought + sold
    actual = float(p.get("cash") or 0)
    return actual - expected, bought, sold, expected, actual, unknown


def cmd_check(a):
    """Does the book balance? Exit 0 yes, 1 no. Meant for scripts."""
    p = load()
    gap, bought, sold, expected, actual, unknown = book_gap(p)
    for t in unknown:
        print(f"trade with no recognisable side: {t!r}", file=sys.stderr)
    if abs(gap) <= 0.01 and not unknown:
        print(f"book balances: ${float(p['starting_cash']):,.2f} - ${bought:,.2f} "
              f"+ ${sold:,.2f} = ${actual:,.2f}")
        return 0
    print(f"BOOK DOES NOT BALANCE: expected ${expected:,.2f}, cash reads "
          f"${actual:,.2f} - ${gap:+,.2f} "
          f"{'invented' if gap > 0 else 'destroyed'}", file=sys.stderr)
    return 1


def position(p, symbol):
    for pos in p["positions"]:
        if pos.get("symbol", "").upper() == symbol.upper():
            return pos
    return None


def market_value(p):
    total = float(p["cash"])
    for pos in p["positions"]:
        sh = float(pos.get("shares") or 0)
        if sh <= 0:
            continue
        px = last_price(pos["symbol"], pos.get("cost_basis"))
        total += sh * float(px or 0)
    return total


def cmd_show(a):
    print(summary(load()))
    return 0


def cmd_buy(a):
    p = load()
    sym = a.symbol.upper()
    px = a.price if a.price is not None else last_price(sym)
    if px is None:
        print(f"no price for {sym} in quotes.json and none given - refusing.", file=sys.stderr)
        return 1
    shares = int(a.shares)
    if shares <= 0:
        print("shares must be positive.", file=sys.stderr)
        return 1

    cost = shares * float(px)
    if cost > float(p["cash"]):
        print(f"REFUSED: that costs ${cost:,.2f} and cash is ${float(p['cash']):,.2f}.", file=sys.stderr)
        return 1

    mv = market_value(p)
    existing = position(p, sym)
    held_value = float(existing["shares"]) * float(px) if existing and float(existing.get("shares") or 0) > 0 else 0
    if mv and (held_value + cost) / mv * 100 > MAX_POSITION_PCT:
        print(f"REFUSED: {sym} would be "
              f"{(held_value + cost) / mv * 100:.1f}% of the portfolio, cap is {MAX_POSITION_PCT:.0f}%.",
              file=sys.stderr)
        return 1
    if mv and (float(p["cash"]) - cost) / mv * 100 < MIN_CASH_PCT:
        print(f"REFUSED: that would leave "
              f"{(float(p['cash']) - cost) / mv * 100:.1f}% cash, minimum is {MIN_CASH_PCT:.0f}%.",
              file=sys.stderr)
        return 1

    p["cash"] = round(float(p["cash"]) - cost, 2)
    if existing and float(existing.get("shares") or 0) > 0:
        old_sh = float(existing["shares"])
        existing["cost_basis"] = round(
            (old_sh * float(existing["cost_basis"]) + cost) / (old_sh + shares), 2)
        existing["shares"] = old_sh + shares
    else:
        p["positions"] = [x for x in p["positions"] if x.get("symbol", "").upper() != sym]
        p["positions"].append({"symbol": sym, "shares": shares, "cost_basis": round(float(px), 2),
                               "entry_utc": now(), "reason": a.reason or "", "score": a.score})
    p["trades"].append({"cycle": p.get("cycle_count", 0), "side": "BUY", "symbol": sym,
                        "shares": shares, "price": round(float(px), 2),
                        "notional": round(cost, 2), "utc": now(), "reason": a.reason or ""})
    save(p)
    print(f"BOUGHT {shares} {sym} @ ${float(px):,.2f} = ${cost:,.2f}   cash now ${float(p['cash']):,.2f}")
    return 0


def cmd_sell(a):
    p = load()
    code = sell(p, a.symbol, a.shares, a.price, a.reason)
    if code == 0:
        save(p)
    return code


def sell(p, symbol, shares_arg="all", price=None, reason=""):
    """Apply one sale to p in memory; the caller saves. 0, or 1 if refused."""
    sym = symbol.upper()
    pos = position(p, sym)
    held = float(pos.get("shares") or 0) if pos else 0
    if held <= 0:
        print(f"REFUSED: no open position in {sym}.", file=sys.stderr)
        return 1

    shares = int(held) if shares_arg in (None, "all") else int(shares_arg)
    if shares <= 0 or shares > held:
        print(f"REFUSED: you hold {held:.0f} {sym}, cannot sell {shares}.", file=sys.stderr)
        return 1

    px = price if price is not None else last_price(sym)
    if px is None:
        print(f"no price for {sym} in quotes.json and none given - refusing.", file=sys.stderr)
        return 1

    proceeds = shares * float(px)
    cb = float(pos.get("cost_basis") or 0)
    realised = (float(px) - cb) * shares

    # THE BUG THIS SCRIPT EXISTS FOR: the proceeds go back into cash.
    p["cash"] = round(float(p["cash"]) + proceeds, 2)
    pos["shares"] = held - shares
    if pos["shares"] <= 0:
        # A closed position leaves the book. Leaving zero-share rows in
        # positions is what filled the dashboard with $0.00 lines.
        p["positions"] = [x for x in p["positions"] if x.get("symbol", "").upper() != sym]

    p["trades"].append({"cycle": p.get("cycle_count", 0), "side": "SELL", "symbol": sym,
                        "shares": shares, "price": round(float(px), 2),
                        "notional": round(proceeds, 2), "realised_pnl": round(realised, 2),
                        "utc": now(), "reason": reason or ""})
    print(f"SOLD {shares} {sym} @ ${float(px):,.2f} = ${proceeds:,.2f}  "
          f"realised {realised:+,.2f}   cash now ${float(p['cash']):,.2f}")
    return 0


def cmd_exits(a):
    """The sell rules, checked in code. Without --apply it says what is due;
    with it, it sells each one, with the rule as the trade's reason."""
    p = load()
    due = exits_due(p)
    if not due:
        held = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
        print("exits: nothing due" + "".join(
            f"\n  {x['symbol']}: {exit_check(entry_of(p, x), quote(x['symbol']))[1]}" for x in held))
        return 0
    for sym, rule, reason in due:
        if not a.apply:
            print(f"DUE {sym}: {reason}")
        elif sell(p, sym, "all", None, reason) != 0:
            return 1
    if a.apply:
        save(p)
    return 0


def summary(p):
    mv = market_value(p)
    start = float(p["starting_cash"])
    lines = [f"cash      ${float(p['cash']):,.2f}",
             f"value     ${mv:,.2f}   ({(mv / start - 1) * 100:+.2f}% from ${start:,.0f})",
             f"cycles    {p.get('cycle_count', 0)}"]
    open_pos = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
    lines.append(f"\npositions ({len(open_pos)} open)")
    for pos in open_pos:
        sh = float(pos["shares"])
        px = float(last_price(pos["symbol"], pos.get("cost_basis")) or 0)
        cb = float(pos.get("cost_basis") or 0)
        pnl = (px / cb - 1) * 100 if cb else 0
        stop = exit_check(entry_of(p, pos), quote(pos["symbol"]))[2]
        lines.append(f"  {pos['symbol']:<6} {sh:>6.0f} sh  entry ${cb:>8,.2f}  "
                     f"last ${px:>8,.2f}  value ${sh * px:>9,.2f}  {pnl:+.2f}%"
                     + (f"  stop ${stop:,.2f}" if stop else ""))
    lines.append(f"\ntrades    {len(p['trades'])}")
    for t in p["trades"][-8:]:
        lines.append(f"  cycle {t.get('cycle', '?')}  {t.get('side', '?'):<4} "
                     f"{t.get('symbol', '?'):<6} {t.get('shares', 0):>4} @ "
                     f"${float(t.get('price') or 0):>8,.2f}  ${float(t.get('notional') or 0):>9,.2f}")
    return "\n".join(lines)


def cmd_mark(a):
    """End of cycle. Re-marks every position against quotes.json, bumps
    cycle_count and stamps last_cycle_utc.

    Every cycle ends here, including a cycle that traded nothing. That is the
    point: a pass is a real outcome and the file has to say when it happened.
    It is also what tells the service the cycle ran - a portfolio.json whose
    mtime never moved is indistinguishable from an agent that quit early."""
    p = load()
    for pos in p["positions"]:
        sh = float(pos.get("shares") or 0)
        if sh <= 0:
            continue
        px = float(last_price(pos["symbol"], pos.get("cost_basis")) or 0)
        cb = float(pos.get("cost_basis") or 0)
        pos["last_price"] = round(px, 2)
        pos["market_value"] = round(sh * px, 2)
        pos["unrealised_pnl"] = round((px - cb) * sh, 2)
        pos["unrealised_pct"] = round((px / cb - 1) * 100, 2) if cb else 0.0
    p["positions"] = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
    p["cycle_count"] = int(p.get("cycle_count") or 0) + 1
    p["market_value"] = round(market_value(p), 2)
    save(p)
    print(summary(p))
    return 0


def cmd_reset(a):
    """Start the paper account over at starting_cash, keeping the old file.

    This exists because the account once read -41% when the real trading loss
    was under 4%: three positions were closed by zeroing shares without ever
    crediting the proceeds, and $3,731.90 left the book. There is no honest way
    to reconstruct those sales - the prices were never recorded - so the choice
    is a clean restart or a number nobody can stand behind."""
    if not a.confirm:
        print("reset needs --confirm. It archives the current portfolio and "
              "starts over at starting_cash.", file=sys.stderr)
        return 1
    p = load()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Archives live in their own folder: the dashboard falls back to any
    # *portfolio*.json in state/ when portfolio.json is missing, and an old
    # book is the last thing that should stand in for the current one.
    adir = STATE.parent / "archive"
    adir.mkdir(exist_ok=True)
    archive = adir / f"portfolio.{stamp}.json"
    archive.write_text(STATE.read_text())
    fresh = {"mode": p.get("mode", "paper"),
             "note": p.get("note", "Simulated money. No real brokerage is connected to this file."),
             "currency": p.get("currency", "USD"),
             "starting_cash": float(a.cash if a.cash is not None else p["starting_cash"]),
             "cash": float(a.cash if a.cash is not None else p["starting_cash"]),
             "positions": [], "trades": [], "cycle_count": 0,
             "created_utc": now(), "last_cycle_utc": None,
             "reset_from": f"archive/{archive.name}",
             "reset_reason": a.reason or "accounting repair"}
    STATE.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
    print(f"archived to {archive}")
    print(f"portfolio reset to ${fresh['cash']:,.2f} cash, no positions, no trades.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Belfort's paper portfolio, with the arithmetic in code")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show").set_defaults(fn=cmd_show)
    sub.add_parser("mark").set_defaults(fn=cmd_mark)
    sub.add_parser("check").set_defaults(fn=cmd_check)

    r = sub.add_parser("reset")
    r.add_argument("--confirm", action="store_true")
    r.add_argument("--cash", type=float)
    r.add_argument("--reason", default="")
    r.set_defaults(fn=cmd_reset)

    b = sub.add_parser("buy")
    b.add_argument("symbol"); b.add_argument("shares")
    b.add_argument("--price", type=float); b.add_argument("--reason", default="")
    b.add_argument("--score", type=int, default=0); b.set_defaults(fn=cmd_buy)

    e = sub.add_parser("exits")
    e.add_argument("--apply", action="store_true", help="sell every position whose exit is due")
    e.set_defaults(fn=cmd_exits)

    s = sub.add_parser("sell")
    s.add_argument("symbol"); s.add_argument("shares", nargs="?", default="all")
    s.add_argument("--price", type=float); s.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_sell)

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
