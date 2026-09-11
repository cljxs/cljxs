# Belfort — swing trader (PAPER)

$10,000 of simulated money. No brokerage is connected. Nothing here can place
a real order.

## Install

Run these in order. Registering FIRST matters: `openclaw agents add` seeds
`AGENTS.md`, so the Belfort instructions get merged in afterwards.

    # 1. register the agent, pointing its workspace at this folder
    openclaw agents add belfort --workspace /root/ecosystem/agents/belfort --non-interactive

    # 2. merge Belfort's instructions on TOP of the seeded AGENTS.md
    cd /root/ecosystem/agents/belfort && cat _belfort-agents-header.md AGENTS.md > .a && mv .a AGENTS.md

    # 3. seed runtime state (cp -n never overwrites an existing live file)
    cp -n /root/ecosystem/agents/belfort/state/portfolio.seed.json /root/ecosystem/agents/belfort/state/portfolio.json
    cp -n /root/ecosystem/agents/belfort/MEMORY.seed.md /root/ecosystem/agents/belfort/MEMORY.md

    # 4. set the cheap model and restart the gateway
    openclaw gateway restart

    # 5. install the timers
    cp /root/ecosystem/deploy/belfort-*.service /root/ecosystem/deploy/belfort-*.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now belfort-fetch.timer belfort-cycle.timer

## Why the workspace flag matters

OpenClaw reads an agent's bootstrap files (`AGENTS.md`, `MEMORY.md`, `SOUL.md`,
`IDENTITY.md`, `USER.md`, `HEARTBEAT.md`, `TOOLS.md`) from **that agent's
workspace**, which defaults to `~/.openclaw/workspace`. Without
`--workspace`, Belfort would read a different folder entirely and never see
these instructions. All of those files are injected into the system prompt on
every call, so keep them short — that is fuel spent on every single wake.

## What runs when

| | Schedule | Costs |
|---|---|---|
| `belfort-fetch.timer` | every 10 min, 09:00-16:50 ET, Mon-Fri | **nothing** - plain Python, no AI |
| `belfort-cycle.timer` | 09:35 and 15:55 ET, Mon-Fri | **fuel** - 2 AI wakes per trading day |

The frequent polling is deliberately in the free half. Only the twice-daily
cycle spends money.

## Files

    _belfort-agents-header.md   the operating spec, merged into AGENTS.md
    MEMORY.seed.md              seed for the append-only memory log
    state/portfolio.seed.json   seed for the $10,000 paper portfolio
    state/portfolio.json        LIVE portfolio (gitignored)
    data/                       fetcher output (gitignored, agent reads only)
    reports/                    daily reports (gitignored)

## Checking on it

    systemctl list-timers 'belfort*' --no-pager      # when it next runs
    journalctl -u belfort-cycle -n 50 --no-pager     # what it did last wake
    cat state/portfolio.json                          # positions and cash
    ls -t reports/ | head                             # latest reports
    tail MEMORY.md                                    # one line per cycle

## Pausing it

    systemctl disable --now belfort-cycle.timer      # stop AI wakes (stops spend)
    systemctl disable --now belfort-fetch.timer      # stop data collection too

Re-enable with `systemctl enable --now <timer>`.

## Data source

Yahoo Finance's public chart endpoint. No API key, no account. It is an
unofficial endpoint, so it can rate-limit or change shape without notice — if
it breaks, the fetcher logs the failure and leaves the previous data files
alone rather than writing garbage. Belfort refuses to act on stale data.
