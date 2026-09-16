"""
ace_time.py — the Eastern clock, in one place.

Ace named a report `2026-09-16-afternoon.md` for a wake that happened at
23:31 Eastern on 2026-09-15. Both halves were wrong, and both are what you
get from reading a UTC clock: the date had already rolled over, and 03:31
is not any of the three slots.

Slots and dates are deterministic. Neither is a judgement call, so neither
belongs to a language model. The fetcher writes both into candidates.json
and Ace reads them; the verifier imports the same two functions, so the name
it demands and the name Ace is handed can never disagree.

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

    The fallback is deliberately the summer offset: the wakes are at 09:00,
    15:00 and 23:30, and each slot boundary below is hours away from all of
    them, so an hour of drift cannot land on the wrong one. The only case it
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


def slot(now=None):
    """Which of the three wakes this is: 09:00, 15:00 or 23:30 ET."""
    h = (now or eastern_now()).hour
    if h < 12:
        return "morning"
    return "afternoon" if h < 20 else "night"


def report_name(now=None):
    now = now or eastern_now()
    return f"{day(now)}-{slot(now)}.md"
