#!/usr/bin/env python3
"""
budget.py — what this month has cost, against the monthly budget.

    python3 scripts/budget.py            # the month so far, in plain words
    python3 scripts/budget.py --json     # the same, for the Deck
    python3 scripts/budget.py check      # exit 4 when the AI allowance is spent

THE BUDGET is `monthly_budget` in tasks/limits.json, next to the other spend
caps. It is everything: the droplet and any other fixed bill come off the top,
and what is left is the AI allowance. The owner set it at $25 a month until
the ecosystem earns its keep.

THE AI SPEND is OpenRouter's own count, `usage_monthly` on the key endpoint -
the current UTC month, in dollars, measured by the provider on every call.
Nothing here estimates it. cost-estimate.py plans a cycle; this reads the bill.

THE HARD STOP is not this script. OpenRouter can cap a key and reset the cap
monthly; a capped key refuses the call that would cross it, whatever any agent
does. This script says whether that cap is set and whether it matches the
allowance. `check` is the soft stop for things that choose to wake a model -
the GM calls it and stays asleep when it exits 4.

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

OVER = 4          # `check` exit code: the AI allowance for this month is spent


def monthly_budget(path=LIMITS):
    """The owner's monthly ceiling, or None when it has not been set."""
    try:
        v = json.loads(Path(path).read_text()).get("monthly_budget")
    except Exception:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def assess(key_data, budget, fixed=None, now=None):
    """Everything the month's money question needs, from one key read.

    Pure: no network, no files. `key_data` is the `data` object of
    OpenRouter's /api/v1/key. The month is the UTC month because that is the
    month OpenRouter counts `usage_monthly` in - pacing against an Eastern
    month would compare two different windows.
    """
    now = now or datetime.now(timezone.utc)
    fixed = FIXED_MONTHLY if fixed is None else fixed
    fixed_total = round(sum(fixed.values()), 2)

    raw = key_data.get("usage_monthly")
    ai = float(raw) if isinstance(raw, (int, float)) else None

    days = calendar.monthrange(now.year, now.month)[1]
    day = now.day
    out = {
        "month": now.strftime("%Y-%m"),
        "day": day, "days": days,
        "budget": budget,
        "fixed": dict(fixed), "fixed_total": fixed_total,
        "ai_spent": None if ai is None else round(ai, 2),
        "ai_allowance": None, "ai_left": None,
        "projected_ai": None, "per_day_left": None,
        "total_spent": None,
        "state": "unknown",
        "key_limit": key_data.get("limit"),
        "key_limit_reset": key_data.get("limit_reset"),
        "hard_stop": None,
    }
    if ai is None:
        out["state"] = "unknown"
        return out

    out["total_spent"] = round(ai + fixed_total, 2)
    # Straight-line pace: what the whole month costs if the rest of it goes
    # like the part so far. Crude, and honest about being crude - a single
    # expensive day early in the month reads as a warning, which is the
    # direction worth erring in on a small budget.
    out["projected_ai"] = round(ai / day * days, 2)

    if budget is None:
        out["state"] = "no budget set"
    else:
        allowance = round(budget - fixed_total, 2)
        left = round(allowance - ai, 2)
        out["ai_allowance"] = allowance
        out["ai_left"] = left
        remaining_days = days - day + 1
        out["per_day_left"] = round(max(left, 0) / remaining_days, 2)
        if left <= 0:
            out["state"] = "over"
        elif out["projected_ai"] > allowance:
            out["state"] = "on pace to go over"
        else:
            out["state"] = "ok"

    out["hard_stop"] = hard_stop(out["key_limit"], out["key_limit_reset"],
                                 out["ai_allowance"])
    return out


def hard_stop(limit, reset, allowance):
    """Whether OpenRouter itself will stop spending at the allowance.

    Only a cap that resets monthly is a monthly budget. A one-off cap is a
    lifetime total that runs out in some month nobody chose; no cap at all
    means only this script and the owner stand between an agent loop and
    the card.
    """
    if limit is None:
        return "none - OpenRouter will keep spending"
    if reset != "monthly":
        return f"${float(limit):.2f} lifetime cap, not monthly"
    if allowance is not None and float(limit) > allowance + 0.005:
        return f"${float(limit):.2f} monthly - above the ${allowance:.2f} AI allowance"
    return f"${float(limit):.2f} monthly"


def lines(a):
    """The assessment as the few sentences a person reads."""
    out = [f"Budget, {a['month']} (day {a['day']} of {a['days']}, UTC month)"]
    if a["state"] == "unknown":
        out.append("  AI spend    unreadable - OpenRouter did not report usage_monthly")
        return out
    fixed = ", ".join(f"{k} ${v:.2f}" for k, v in a["fixed"].items()) or "none"
    out.append(f"  AI spend    ${a['ai_spent']:.2f} so far "
               f"(on pace for ${a['projected_ai']:.2f} this month)")
    out.append(f"  fixed       ${a['fixed_total']:.2f} ({fixed})")
    out.append(f"  total       ${a['total_spent']:.2f}")
    if a["budget"] is None:
        out.append("  budget      not set - add monthly_budget to tasks/limits.json")
    else:
        out.append(f"  budget      ${a['budget']:.2f} a month, "
                   f"leaving ${a['ai_allowance']:.2f} for AI")
        out.append(f"  AI left     ${a['ai_left']:.2f} "
                   f"(${a['per_day_left']:.2f} a day for the rest of the month)")
    out.append(f"  hard stop   {a['hard_stop']}")
    out.append(f"  verdict     {a['state']}")
    return out


def read(key=None):
    """One live read. Raises if there is no key or OpenRouter will not say."""
    key = key if key is not None else preflight.openrouter_key()
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY found")
    return assess(preflight.key_status(key), monthly_budget())


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
        left = result["ai_left"]
        print(f"budget {result['state']}"
              + (f" - ${left:.2f} of AI allowance left" if left is not None else ""))
    else:
        print("\n".join(lines(result)))

    if a.cmd == "check" and result["state"] == "over":
        return OVER
    return 0


if __name__ == "__main__":
    sys.exit(main())
