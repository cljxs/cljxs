# Fury — daily briefing

One short briefing every morning, so you do not have to SSH in to know whether
anything needs you.

**Fury is plain code, not a model.** `scripts/fury-collect.py` reads every
agent's state, last run and memory line, the failed services, and the task
queue, and writes `reports/YYYY-MM-DD.md` from those facts. It costs nothing
to run and cannot silently skip a morning.

It was a model agent first. It was asked five times, in five different
wordings, to write the briefing to a file. Every run made one tool call, read
the system state, composed a perfectly good briefing into its reply, and saved
nothing — including the run where the file already existed on disk with the
headings in place and all it had to do was fill them in.

Everything the briefing needed was already in the collector's own output. The
model was only turning known facts into sentences. So the script does that
part too. What was lost is fluent prose and judgement about what matters most;
what was gained is a briefing that is correct every morning, never missing,
and free.

The OpenClaw agent registration is left in place, unused. Nothing wakes it, so
it costs nothing.

## Install

    git -C /root/ecosystem pull
    cp /root/ecosystem/deploy/fury-cycle.service /root/ecosystem/deploy/fury-cycle.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now fury-cycle.timer

No model, no API key, no AGENTS.md merge. It is a Python script on a timer.

    systemctl start fury-cycle.service            # run it now
    cat agents/fury/reports/$(date -u +%F).md     # read this morning's briefing


## What runs when

| | Schedule | Costs |
|---|---|---|
| `fury-collect.py` | just before each briefing | **nothing** - plain Python |
| `fury-cycle.timer` | 08:30 ET, once a day | **fuel** - one wake a day |

08:30 ET is after Scout's 08:00 ideas and before Belfort's 09:35 open, so the
briefing covers yesterday's close and this morning's proposals.

## The guard

A briefing that only appears in the reply is not a briefing. The service checks
whether a new `.md` landed in `reports/` in the last ten minutes and exits 1 if
not, so a run that writes nothing is reported as failed rather than as success.

## Checking on it

    systemctl list-timers 'fury*' --no-pager
    journalctl -u fury-cycle -n 40 --no-pager
    cat reports/$(ls -t reports/ | head -1)
    cat data/system.json | head -40

Or just open the dashboard.

## Pausing it

    systemctl disable --now fury-cycle.timer
