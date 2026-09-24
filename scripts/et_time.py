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

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def to_eastern(when):
    """A UTC datetime, or an ISO string such as ESPN's "2026-09-25T00:15Z",
    as Eastern time. None if it cannot be read.

    Every UTC-to-Eastern conversion goes through here, for the same reason the
    slots do: a kickoff at 00:15 UTC is Thursday night in Green Bay, and a
    page that did its own arithmetic would file it under Friday.
    """
    if isinstance(when, str):
        text = when.strip().replace("Z", "+00:00")
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(when, datetime):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if _ET is not None:
        return when.astimezone(_ET)
    return when.astimezone(timezone.utc) - timedelta(hours=4)


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

# The shortest thing that counts as a report. It lived as a bare 60 in two
# verifiers and nowhere in any agent's instructions, so belfort was failed for
# writing 43 words having been told only to write "two honest paragraphs" - a
# rule it was never given and could not have satisfied on purpose. It lives
# here now, the headers quote it, and a test fails the build if they drift.
MIN_REPORT_WORDS = 60


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


def stamp_is_wellformed(name, agent_name):
    """Does a stamped report_name look like one this agent's fetcher wrote?

    The fetcher stamps data/_meta.json and the verifier grades against it - but
    the agent has a write tool and the file sits in its own workspace, so it
    can change the filename it is judged on. That is marking your own exam.

    Belfort was failed for "no reports/2026-09-17-cycle-report.md". No fetcher
    can produce that name: they stamp report_name(), which only ever yields
    <date>-<slot>.md. Whatever wrote it, a stamp that does not match the shape
    this agent's fetcher produces is not evidence of anything and is not
    trusted.

    Agents with no slots - scout, emily, fury - choose their own filenames, so
    there is no shape to check and any stamp stands.
    """
    if agent_name not in SLOTS:
        return True
    names = "|".join(re.escape(n) for _, n in SLOTS[agent_name])
    return re.match(rf"^\d{{4}}-\d{{2}}-\d{{2}}-(?:{names})\.md$", str(name)) is not None


def strays(agent_dir, name):
    """Where a report owed as reports/<name> was filed instead, if anywhere.

    Two ways it goes missing, and both are named rather than reported as
    "no report":

      the right folder, the wrong slot - reports/<date>-close.md for an open;
      the right name, the wrong folder  - <agent>/<name>, because report_name
        is a bare filename and a model writes a bare filename into whatever
        directory it is standing in. Belfort left 2026-09-17-close.md and two
        siblings in its own root, where no verifier looked and git showed
        them as untracked litter.

    Returned as paths relative to the agent folder, same date only. Both
    verifiers call this; it used to be two copies of the first half.
    """
    d = Path(agent_dir)
    date = str(name)[:10]
    found = []
    if (d / "reports").is_dir():
        found += [f"reports/{x.name}" for x in sorted((d / "reports").glob(f"{date}-*.md"))]
    found += [x.name for x in sorted(d.glob(f"{date}-*.md"))]
    return found


def expected_report(agent_dir, agent_name, now=None, started=None):
    """The exact report this cycle owes, for EVERY caller.

    For an agent with scheduled slots this is COMPUTED and the stamp in
    data/_meta.json is not consulted. That file lives in the workspace of the
    agent being graded, which gave belfort a way to change the filename it was
    judged on: at 14:12 UTC - 10:12 ET, the open slot - it wrote
    2026-09-17-close.md, the verifier agreed and passed the cycle, and the
    fetcher overwrote the stamp back to "open" half an hour later. The name the
    verifier reported could only have come from _meta.json.

    Checking the stamp's shape was not enough. `close` is a real belfort slot;
    it was simply the wrong half of the day. The only reliable fix is that the
    graded party has no say: the clock decides.

    `started` is the cycle-start epoch the wrapper passes to the verifier, used
    in preference to the wall clock so a long cycle cannot drift into the next
    slot while it runs. Belfort wakes at 09:35 and 15:55 and ace at 15:00 and
    23:30, all hours from a boundary, so this is belt and braces.

    Agents with no slots - scout, emily, fury - choose their own filenames, and
    their stamp, which no fetcher writes, still stands. Returns (name, slot); a
    name of None means any report this run wrote counts.
    """
    if agent_name in SLOTS:
        if now is None and started:
            now = datetime.fromtimestamp(started, timezone.utc).astimezone(_ET) \
                if _ET else datetime.fromtimestamp(started, timezone.utc) - timedelta(hours=4)
        now = now or eastern_now()
        return report_name(agent_name, now), slot(agent_name, now)

    try:
        meta = json.loads((Path(agent_dir) / "data" / "_meta.json").read_text())
        name = meta.get("report_name")
        if name and stamp_is_wellformed(name, agent_name):
            return name, (str(meta.get("slot") or "").strip() or None)
    except Exception:
        pass
    return None, None
