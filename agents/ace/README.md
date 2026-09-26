# Ace — sports betting analyst (PAPER)

$10,000 of simulated money. No sportsbook is connected. Nothing here can place
a real wager.

## Install

Register FIRST — `openclaw agents add` seeds `AGENTS.md`, so Ace's
instructions get merged in afterwards.

    openclaw agents add ace --workspace /root/ecosystem/agents/ace --non-interactive
    # merge the instructions on top of the seeded AGENTS.md.
    # Re-runnable: it REBUILDS AGENTS.md rather than prepending, so editing the
    # header and running it again does not leave two copies of every rule.
    # The first run on an already-merged file shows you the seam and asks.
    /root/ecosystem/scripts/merge-header.sh ace
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
| `ace-cycle.timer` | 15:00, 23:30 ET, daily | **fuel** - 2 AI wakes per day |

## Data source

ESPN's public API via `site.web.api.espn.com` (no API key). Note that
`site.api.espn.com` — the host most guides use — is blocked from many
datacentre IPs; the `site.web` host serves the same API and works.

The fetcher writes a compact context file per *scheduled* game, capped at 12
deep fetches per sport. Everything numeric is computed in plain Python:

- the book's **vig**
- **vig-free** probabilities, which sum to 100%

The no-vig number is the one Ace's estimate must beat by 8+ points, and it is
the only probability the context file carries. The raw implied figures and
ESPN's own model projection are both computed or fetched and then deliberately
dropped: every report Ace wrote built itself on the model-versus-line
comparison, which is the one thing its instructions said could never justify a
bet. A number that can only ever be misused is not context, it is bait.

## Why two wakes, not three

**15:00 ET** is the betting wake and **23:30 ET** is the grading wake. A 09:00
wake was dropped: the earliest first pitch is around 13:00 ET and most of the
slate is evening, so at 09:00 no lineup is posted and no scratch is known.
Ace's bar requires *real information the market has not priced yet*, and that
information does not exist yet in the morning. It was a third of the fuel
spent on the wake least able to clear the bar.

## Which model, and why

`openai/gpt-5-mini`. Ace ran on `gpt-4o-mini` and timed out three cycles
running - not from errors, zero tool failures throughout, but from taking too
many turns. One run made 87 tool calls over 68 assistant turns and cost $0.29
before the timeout stopped it.

The cost of a cycle is dominated by cache reads, which scale with the number
of turns rather than with the token rate. That inverts the usual reasoning: a
cycle finishing in twelve turns on a dearer model is cheaper than one taking
sixty-eight on a cheap one and producing nothing.

Costed against one healthy cycle (~40K prompt, 8K output, 360K cache read),
across all 444 models in OpenRouter's catalogue, filtered to the 248 that
support tool calling, carry 100K+ context, and serve a live API:

    gpt-5-mini             $0.035/cycle   $ 2.10/month
    gemini-2.5-flash       $0.043/cycle   $ 2.57/month
    gpt-4.1-mini           $0.065/cycle   $ 3.89/month
    claude-haiku-4.5       $0.116/cycle   $ 6.96/month
    gpt-4o-mini (old)      $0.038/cycle   $ 2.27/month - and did not finish

**gpt-5-mini is both cheaper and newer than the model it replaces.** That is
unusual enough to state plainly; it is not a trade-off.

83 of those 248 models are cheaper still. None of them is worth trying, because
the failure was never price - 4o-mini is already cheap and cannot complete the
job. Going cheaper is the wrong direction.

If it still loops, `anthropic/claude-haiku-4.5` is the next real step up.
Gemini is skipped deliberately: both Gemini models failed mid-cycle on
tool-using work for Belfort and Scout with `provider internal error`, which is
why nothing here runs one.

Change it with:

    scripts/set-agent-model.sh ace <slug> --apply

which checks the slug against OpenRouter's catalogue first - a wrong one does
not fail until the next wake, hours later.

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
