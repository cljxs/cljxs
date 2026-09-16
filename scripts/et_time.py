"""
et_time.py — the Eastern clock and the wake slots, in one place.

Ace named a report `2026-09-16-afternoon.md` for a wake that happened at
23:31 Eastern on 2026-09-15. Both halves were wrong, and both are what you
get from reading a UTC clock: the date had already rolled over, and 03:31
is not any of the three slots.

Slots and dates are deterministic. Neither is a judgement call, so neither
belongs to a language model. The fetcher writes both into candidates.json
and Ace reads them; the verifier imports the same two functions, so the name
it demands and the name Ace is handed can never disagree.

It happened a second time, to a different agent: Belfort filed its 09:35 ET
open as `2026-09-16-close.md`. Same cause - 09:35 ET is 13:35 UTC, which looks
like the afternoon. Hence the shared module rather than a per-agent copy.

Underscored filename so it is importable - the scripts around it have
hyphens and cannot be.
"""

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                   # no tzdata on the host
    _ET = None


def eastern_now():
    """Real Eastern time where the host has tzdata, otherwise UTC-4.

    The fallback is deliberately the summer offset: the wakes are at 15:00 and
    23:30 (09:35 and 15:55 for Belfort), and each slot boundary below is hours
    away from all of them, so an hour of drift cannot land on the wrong one. The only case it
    would get wrong is a 23:30 wake in winter, which lands at 04:30 UTC the
    next day either way - and that is exactly why `day` is computed here too.
    """
    if _ET is not None:
        return datetime.now(_ET)
    return datetime.now(timezone.utc) - timedelta(hours=4)


def day(now=None):
    """The Eastern date. A 23:30 ET wake belongs to the day it is in ET, not
    the UTC day that has already started."""
    return (now or eastern_now()).strftime("%Y-%m-%d")


# Each agent's wakes, as (hour it stops applying, what that slot is called).
# Boundaries sit hours away from every actual wake, so an hour of drift in the
# fallback offset can never select the wrong one.
ACE = ((20, "afternoon"), (24, "night"))                         # 15:00 23:30
BELFORT = ((12, "open"), (24, "close"))                          # 09:35 15:55

SLOTS = {"ace": ACE, "belfort": BELFORT}


def slot(schedule, now=None):
    """Which wake this is. `schedule` is one of the tuples above, or an agent
    name."""
    if isinstance(schedule, str):
        schedule = SLOTS[schedule]
    h = (now or eastern_now()).hour
    for cutoff, name in schedule:
        if h < cutoff:
            return name
    return schedule[-1][1]


def report_name(schedule, now=None):
    now = now or eastern_now()
    return f"{day(now)}-{slot(schedule, now)}.md"
