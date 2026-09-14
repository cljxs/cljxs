# Scout — product idea scout

Proposes product ideas for Emily. **Proposes only** — Scout never queues work.

## The loop

    Scout proposes  ->  YOU approve  ->  Emily builds  ->  YOU publish

Two gates, both yours. Scout writes into `state/ideas.json` with
`status: pending` and stops. Approving is what creates Emily's queue task.

## Install

    openclaw agents add scout --workspace /root/ecosystem/agents/scout --non-interactive
    cd /root/ecosystem/agents/scout && cat _scout-agents-header.md AGENTS.md > .a && mv .a AGENTS.md
    cp -n /root/ecosystem/agents/scout/MEMORY.seed.md /root/ecosystem/agents/scout/MEMORY.md
    cp -n /root/ecosystem/agents/scout/state/ideas.seed.json /root/ecosystem/agents/scout/state/ideas.json
    openclaw models auth paste-api-key --provider openrouter --agent scout
    openclaw agents list        # confirm scout's index before the next two lines
    openclaw config set 'agents.list[4].model' 'openrouter/google/gemini-2.5-flash-lite'
    openclaw config set 'agents.list[4].thinkingDefault' 'low'
    cp /root/ecosystem/deploy/scout-cycle.service /root/ecosystem/deploy/scout-cycle.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now scout-cycle.timer

## Reviewing ideas

    scripts/scout-review.py list
    scripts/scout-review.py approve 3 --reason "good seasonal fit"
    scripts/scout-review.py reject 4 "too close to a licensed character"

Approving runs `emily-new-build.py`, which is subject to Emily's own 3/day
cap and dedupe. Both verdicts are written to `agents/emily/state/lessons.md`,
which Scout reads before proposing again — so a rejected direction does not
come back.

Approving the same idea twice is refused rather than queued twice.

## What runs when

| | Schedule | Costs |
|---|---|---|
| `scout-cycle.timer` | 08:00 ET, once a day | **fuel** - about 9p a month |

Once a day is deliberate. Ideas do not go stale in hours, and this is the
only part of Scout that spends anything.

## What Scout is not

Scout has **no market data** - no sales figures, no search volume, no trend
feed. Its ideas are hypotheses from reasoning about season, audience and what
has already been tried. Its instructions forbid it from claiming otherwise,
because an agent inventing "this is trending" is worse than useless.

It is also told to propose only small-format products - stickers, mugs, totes,
phone cases - because the available image models produce about 1024px, which
is a few inches at print resolution.

## Pausing it

    systemctl disable --now scout-cycle.timer
