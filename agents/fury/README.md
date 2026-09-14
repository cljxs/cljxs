# Fury — daily briefing

Reads the whole ecosystem and writes one short briefing you can read in thirty
seconds. Fury is the reason you do not have to SSH in to know what happened.

## How it works

Same split the other agents use, pointed at the ecosystem itself:

    fury-collect.py  (plain code, no AI)  ->  data/system.json  ->  Fury reads it

The collector gathers every agent's state file, latest report and memory line,
every service's exit status, the timers, and the task queue. Fury may only
state facts that are in that file. It cannot guess whether something ran.

The briefing then appears **at the top of your Command Deck**, so it is read on
a phone rather than over SSH.

## Install

    openclaw agents add fury --workspace /root/ecosystem/agents/fury --non-interactive
    cd /root/ecosystem/agents/fury && cat _fury-agents-header.md AGENTS.md > .a && mv .a AGENTS.md
    cp -n /root/ecosystem/agents/fury/MEMORY.seed.md /root/ecosystem/agents/fury/MEMORY.md
    openclaw models auth paste-api-key --provider openrouter --agent fury
    openclaw agents list        # confirm fury's index before the next two lines
    openclaw config set 'agents.list[5].model' 'openrouter/google/gemini-2.5-flash-lite'
    openclaw config set 'agents.list[5].thinkingDefault' 'low'
    cp /root/ecosystem/deploy/fury-*.service /root/ecosystem/deploy/fury-cycle.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now fury-cycle.timer
    systemctl restart mission-control-api      # picks up the briefing panel

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
