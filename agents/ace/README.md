# Ace — sports betting analyst (PAPER)

$10,000 of simulated money. No sportsbook is connected. Nothing here can place
a real wager.

## Install

Register FIRST — `openclaw agents add` seeds `AGENTS.md`, so Ace's
instructions get merged in afterwards.

    openclaw agents add ace --workspace /root/ecosystem/agents/ace --non-interactive
    cd /root/ecosystem/agents/ace && cat _ace-agents-header.md AGENTS.md > .a && mv .a AGENTS.md
    cp -n /root/ecosystem/agents/ace/state/bankroll.seed.json /root/ecosystem/agents/ace/state/bankroll.json
    cp -n /root/ecosystem/agents/ace/MEMORY.seed.md /root/ecosystem/agents/ace/MEMORY.md
    openclaw models auth paste-api-key --provider openrouter --agent ace
    openclaw config set 'agents.list[2].model' 'openrouter/google/gemini-2.5-flash-lite'
    openclaw config set 'agents.list[2].thinkingDefault' 'low'
    openclaw gateway restart
    cp /root/ecosystem/deploy/ace-*.service /root/ecosystem/deploy/ace-*.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now ace-fetch.timer ace-cycle.timer

Check the agents.list index with `openclaw agents list` before running the
`config set` lines — the index must be Ace's, not another agent's.

## What runs when

| | Schedule | Costs |
|---|---|---|
| `ace-fetch.timer` | every 30 min, 09:00-23:30 ET, daily | **nothing** - plain Python |
| `ace-cycle.timer` | 09:00, 15:00, 23:30 ET, daily | **fuel** - 3 AI wakes per day |

## Data source

ESPN's public API via `site.web.api.espn.com` (no API key). Note that
`site.api.espn.com` — the host most guides use — is blocked from many
datacentre IPs; the `site.web` host serves the same API and works.

The fetcher writes a compact context file per *scheduled* game, capped at 12
deep fetches per sport. Everything numeric is computed in plain Python:

- implied probability from each moneyline
- the book's **vig**
- **vig-free** probabilities, which sum to 100%

That no-vig number is the one Ace's estimate must beat by 8+ points. Comparing
against the raw implied probability instead would mean betting into the vig
every time.

## Checking on it

    systemctl list-timers 'ace*' --no-pager
    journalctl -u ace-cycle -n 50 --no-pager
    cat state/bankroll.json
    ls -t reports/ | head
    tail MEMORY.md

## Pausing it

    systemctl disable --now ace-cycle.timer     # stops AI wakes (stops spend)
    systemctl disable --now ace-fetch.timer     # stops data collection too

## Files

    _ace-agents-header.md     operating spec, merged into AGENTS.md
    MEMORY.seed.md            seed for the memory log
    state/bankroll.seed.json  seed for the $10,000 paper bankroll
    state/bankroll.json       LIVE bankroll (gitignored)
    data/                     fetcher output (gitignored, agent reads only)
    data/context/             one file per scheduled game
    reports/                  daily reports (gitignored)
