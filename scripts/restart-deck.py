#!/usr/bin/env python3
"""
restart-deck.py - restart the Command Deck without killing the work it started.

    restart-deck.py            wait until nothing it started is running, then restart
    restart-deck.py --status   say what it is running now and stop there
    restart-deck.py --now      restart at once, whatever is running

`systemctl restart mission-control-api` stops every process in the service's
control group, not just the Deck. The Deck runs the long jobs its buttons
start - an approval drawing a design, Try again making a Printify draft, a
TikTok kit, Paul's search - so a restart in the middle of one kills it half
way. On 2026-10-19 the gallery showed a build with its order written and
nothing else ("pond side reaction frog sticker"): an approval stopped while
drawing, after a run of pulls each followed by a restart.

So this waits for a moment when the Deck has no child process, and restarts
then. Its quick reads (the boards, the badges) finish in under a second, so
that moment comes almost at once unless a real job is running; a job keeps it
waiting, and it says which. Free - no model, no network.
"""

import subprocess
import sys
import time
from pathlib import Path

UNIT = "mission-control-api"
MAX_WAIT_S = 30 * 60        # the longest Deck job: Try again's 25-minute limit (emily.js)
POLL_S = 1
SAY_EVERY_S = 60


def show(prop):
    r = subprocess.run(["systemctl", "show", "-p", prop, "--value", UNIT],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


def procs_text(cgroup):
    """The control group's process list: cgroup v2, else the v1 systemd tree."""
    for base in ("/sys/fs/cgroup", "/sys/fs/cgroup/systemd"):
        p = Path(base + cgroup) / "cgroup.procs"
        if p.is_file():
            return p.read_text()
    return None


def command_of(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "?"
    return " ".join(a.decode(errors="replace") for a in raw.split(b"\0") if a)[:140]


def children(main_pid, text):
    """PIDs in the group other than the Deck itself - the work in flight."""
    pids = [p.strip() for p in (text or "").split() if p.strip().isdigit()]
    return [p for p in pids if p != str(main_pid)]


def busy():
    """[(pid, command)] the Deck is running, or None when it cannot be read."""
    main, cgroup = show("MainPID"), show("ControlGroup")
    if not main or main == "0" or not cgroup:
        return None
    text = procs_text(cgroup)
    if text is None:
        return None
    return [(p, command_of(p)) for p in children(main, text)]


def wait_idle(read=busy, sleep=time.sleep, clock=time.monotonic, say=print, max_s=MAX_WAIT_S):
    """True at the first instant nothing is running; False if max_s passes first."""
    start = last_said = clock()
    first = True
    while True:
        jobs = read()
        if not jobs:
            return True
        now = clock()
        if first or now - last_said >= SAY_EVERY_S:
            say(f"waiting for {len(jobs)} job(s) the Deck is running ({int(now - start)}s so far):")
            for pid, cmd in jobs:
                say(f"  {pid}  {cmd}")
            last_said, first = now, False
        if now - start >= max_s:
            return False
        sleep(POLL_S)


def restart():
    r = subprocess.run(["systemctl", "restart", UNIT], capture_output=True, text=True, timeout=60)
    if r.returncode:
        print(f"restart failed: {r.stderr.strip()}")
        return 1
    print(f"restarted {UNIT}")
    return 0


def main(argv):
    if "--now" in argv:
        return restart()
    if busy() is None:
        print(f"cannot read {UNIT}'s processes (is it running?) - restarting it as asked")
        return restart()
    if "--status" in argv:
        jobs = busy() or []
        print(f"{len(jobs)} job(s) running" + (":" if jobs else " - safe to restart"))
        for pid, cmd in jobs:
            print(f"  {pid}  {cmd}")
        return 0
    if not wait_idle():
        print(f"still busy after {MAX_WAIT_S // 60} minutes - not restarting. "
              f"Run it again later, or with --now to stop those jobs.")
        return 1
    return restart()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
