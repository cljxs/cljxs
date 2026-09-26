#!/usr/bin/env python3
"""
budget.py — what today's AI has cost, against the daily cap.

    python3 scripts/budget.py            # today and the month so far, in plain words
    python3 scripts/budget.py --json     # the same, for the Deck
    python3 scripts/budget.py check      # exit 4 when today's AI allowance is spent

THE CAP is `daily_ai_budget` in tasks/limits.json, next to the other spend
caps: dollars of AI a day. The owner set it at $1 a day (2026-09-25),
replacing a $25-a-month-all-in budget that left nine cents a day for AI once
the droplet came off the top. The month is shown too, as what it can cost at
most - the cap times the days, plus the fixed bills - so the bigger number is
never a surprise.

THE DAY IS OpenRouter's: `usage_daily` counts the current UTC day, so the
day turns over at midnight UTC - 8pm Eastern in summer, 7pm in winter. Pacing
against an Eastern day would compare two different windows, and the provider's
hard stop resets on its clock, not ours.

THE AI SPEND is OpenRouter's own count, measured by the provider on every
call. Nothing here estimates it. cost-estimate.py plans a cycle; this reads
the bill.

THE HARD STOP is not this script. OpenRouter can cap a key and reset the cap
daily; a capped key refuses the call that would cross it, whatever any agent
does. This script says whether that cap is set and whether it matches.
`check` is the soft stop for things that choose to wake a model - the GM
calls it and stays asleep when it exits 4.

Reads nothing secret into its output: the key is sent to OpenRouter and never
printed, stored or returned.

Standard library only.
"""

import argparse
import calendar
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent
LIMITS = ROOT / "tasks" / "limits.json"

# preflight.py owns the key lookup and the call to OpenRouter's key endpoint.
_spec = importlib.util.spec_from_file_location("preflight", SCRIPTS / "preflight.py")
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

# Bills that do not depend on how hard the agents work, dollars a month. The
# droplet is the owner's figure from the setup notes, not read from
# DigitalOcean - correct it here if the plan changes. Obsidian was considered
# and declined (2026-09-25), so it is not on this list.
FIXED_MONTHLY = {
    "droplet": 12.00,
}

OVER = 4          # `check` exit code: today's AI allowance is spent


def daily_budget(path=LIMITS):
    """The owner's daily AI cap in dollars, or None when it has not been set."""
    try:
        v = json.loads(Path(path).read_text()).get("daily_ai_budget")
    except Exception:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _dollars(key_data, field):
    v = key_data.get(field)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def assess(key_data, cap, fixed=None, now=None):
    """Everything the money question needs, from one key read.

    Pure: no network, no files. `key_data` is the `data` object of
    OpenRouter's /api/v1/key; `cap` is dollars of AI a day.
    """
    now = now or datetime.now(timezone.utc)
    fixed = FIXED_MONTHLY if fixed is None else fixed
    fixed_total = round(sum(fixed.values()), 2)
    days = calendar.monthrange(now.year, now.month)[1]

    today = _dollars(key_data, "usage_daily")
    month = _dollars(key_data, "usage_monthly")

    out = {
        "day": now.strftime("%Y-%m-%d"), "month": now.strftime("%Y-%m"),
        "days_in_month": days,
        "cap": cap,
        "fixed": dict(fixed), "fixed_total": fixed_total,
        "ai_today": None if today is None else round(today, 2),
        "ai_left_today": None,
        "ai_month": None if month is None else round(month, 2),
        "month_at_most": None if cap is None else round(cap * days + fixed_total, 2),
        "state": "unknown",
        "key_limit": key_data.get("limit"),
        "key_limit_reset": key_data.get("limit_reset"),
        "hard_stop": hard_stop(key_data.get("limit"), key_data.get("limit_reset"), cap),
    }
    # A missing count is unknown, never $0: reading it as zero would report a
    # quiet day in the middle of a runaway loop.
    if today is None:
        return out
    if cap is None:
        out["state"] = "no cap set"
        return out
    left = round(cap - today, 2)
    out["ai_left_today"] = left
    out["state"] = "over" if left <= 0 else "ok"
    return out


def hard_stop(limit, reset, cap):
    """Whether OpenRouter itself will stop spending at the daily cap.

    Only a cap that resets daily is a daily cap. A monthly one lets a bad day
    spend a month's worth before it bites; a one-off cap is a lifetime total
    that runs out on some day nobody chose; no cap at all means only this
    script and the owner stand between an agent loop and the card.
    """
    if limit is None:
        return "none - OpenRouter will keep spending"
    limit = float(limit)
    if reset is None:
        return f"${limit:.2f} lifetime cap, not daily"
    if reset != "daily":
        return f"${limit:.2f} {reset} - not a daily cap"
    if cap is not None and limit > cap + 0.005:
        return f"${limit:.2f} daily - above the ${cap:.2f} cap"
    return f"${limit:.2f} daily"


def lines(a):
    """The assessment as the few sentences a person reads."""
    out = [f"Budget, {a['day']} (UTC day - it turns over at 7pm Central, 8pm Eastern)"]
    if a["state"] == "unknown":
        out.append("  AI today    unreadable - OpenRouter did not report usage_daily")
        out.append(f"  hard stop   {a['hard_stop']}")
        return out
    if a["cap"] is None:
        out.append(f"  AI today    ${a['ai_today']:.2f}")
        out.append("  cap         not set - add daily_ai_budget to tasks/limits.json")
    else:
        out.append(f"  AI today    ${a['ai_today']:.2f} of ${a['cap']:.2f} "
                   f"(${max(a['ai_left_today'], 0):.2f} left)")
    fixed = ", ".join(f"{k} ${v:.2f}" for k, v in a["fixed"].items()) or "none"
    if a["ai_month"] is not None:
        out.append(f"  AI month    ${a['ai_month']:.2f} so far in {a['month']}")
    out.append(f"  fixed       ${a['fixed_total']:.2f} a month ({fixed})")
    if a["month_at_most"] is not None:
        out.append(f"  month max   ${a['month_at_most']:.2f} "
                   f"({a['days_in_month']} days x ${a['cap']:.2f} + fixed)")
    out.append(f"  hard stop   {a['hard_stop']}")
    out.append(f"  verdict     {a['state']}")
    return out


def read(key=None):
    """One live read. Raises if there is no key or OpenRouter will not say."""
    key = key if key is not None else preflight.openrouter_key()
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY found")
    return assess(preflight.key_status(key), daily_budget())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", nargs="?", default="show", choices=["show", "check"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        result = read()
    except Exception as exc:
        # Unreadable is not "over": a network blip must not silence the GM
        # for a day. But it is not "fine" either, so it is said out loud.
        if a.json:
            print(json.dumps({"state": "unreadable", "error": str(exc)}))
        else:
            print(f"budget unreadable: {exc}", file=sys.stderr)
        return 1

    if a.json:
        print(json.dumps(result, indent=2))
    elif a.cmd == "check":
        left = result["ai_left_today"]
        print(f"budget {result['state']}"
              + (f" - ${max(left, 0):.2f} of today's AI allowance left" if left is not None else ""))
    else:
        print("\n".join(lines(result)))

    if a.cmd == "check" and result["state"] == "over":
        return OVER
    return 0


if __name__ == "__main__":
    sys.exit(main())
