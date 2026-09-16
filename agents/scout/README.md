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

One file. `_scout-agents-header.md` already opens with the file-writing rule
that fixed Scout's first run (it printed its ideas into the chat and saved
none), and already carries the honesty and no-duplicate rules at the end.
Those were once separate files; they were folded in, and merging them
alongside the header duplicated every rule and contradicted several.

## Proposing nothing is a pass

Scout is told to propose fewer ideas, or none, when everything it can think
of is already listed or when five or more are already waiting on you. Those
runs are correct behaviour, not failures.

The way the two are told apart is `state/last-run.txt`: **every** run writes
one line there, including the runs that decline. The service then appends that
line to `MEMORY.md` itself.

Scout does not append to `MEMORY.md`, deliberately. It was asked twice, in two
wordings, at the top of its instructions, and replaced the file both times -
destroying the log. Appending a line is deterministic work, so it moved into
code, the same reason the fetchers compute indicators instead of the agent.
Scout now owns a scratch file it is free to overwrite.

    2026-09-15 08:00 ET | proposed 0 - 6 already pending, nothing new | 6 pending

The service checks that file, not `ideas.json`. A run that proposes nothing
and says why passes; a run that writes neither fails. An earlier version
checked `ideas.json` alone and so failed every run that correctly declined -
which made a working agent look broken for a day.

If Scout keeps declining, that is not a Scout problem. It means ideas are
piling up unreviewed, and the fix is at your end: approve or reject them.

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
