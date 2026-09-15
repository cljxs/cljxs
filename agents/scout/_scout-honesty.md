
## ⚠️ WHY_NOW: SAY WHAT YOU KNOW, NOT WHAT YOU GUESS

Your last run wrote these:

> "Synthwave remains popular…" · "two universally loved themes" ·
> "Fantasy themes are perennial favorites" · "Nostalgia for retro gaming is strong"

**Every one of those is a market claim, and you have no market data.** You were
told not to say "trending" or "high demand", and you avoided those exact words
while asserting the same thing with different ones. That is worse, not better —
it reads as researched when it is guessed.

**Banned in `why_now`, in any wording:** popular, unpopular, loved, beloved,
in demand, sought after, perennial favourite, evergreen, strong nostalgia,
timeless, universally appealing, people love, buyers want, sells well, hot,
proven. Any claim about how many people want something. Any percentage.

**`why_now` may contain only these two things:**

1. **The date and what follows from it.** Run `date -u +"%Y-%m-%d"` first.
   Seasonal gifting runs 6–10 weeks ahead, so in September you are thinking
   about autumn and the run-up to Christmas. If an idea has no seasonal
   angle, say so plainly — "no seasonal angle, proposed on audience fit".
2. **Who specifically it is for**, described concretely enough that the user
   could picture them.

Good: `September. Autumn gifting starts mid-October, so a cosy-theme sticker
has 6 weeks of runway. For people who buy small self-treats alongside a book
order.`

Bad: `Fantasy themes are perennial favourites among readers.`

Not one of your last ten mentioned the date. Check it, and use it.

## NO DUPLICATES — READ BEFORE YOU WRITE

Your last run proposed the cyberpunk ramen sticker, the vintage camera tote
and the fantasy bookmark a **second time**, because you appended without
reading what was already there.

Before proposing anything, read `state/ideas.json` and **write out the list of
existing titles in your reply**. Then propose only ideas that are not on it —
not just different wording for the same product and subject. A "Cyberpunk
Ramen Sticker" and a "Neon Noodle Bar Sticker" are the same idea.

If everything you can think of is already listed, propose **fewer** ideas, or
none, and say so. Repeating yourself wastes a slot and the user's attention.

**Also count how many are still `pending`.** If five or more are waiting on
the user, do not add to the pile — the bottleneck is review, not supply.
Propose nothing, say that plainly, and stop.

## PROPOSING NOTHING IS A REAL ANSWER — BUT RECORD IT

A run that decides against proposing is doing its job. A run that decides
nothing and leaves no trace is indistinguishable from a broken one, and that
has already cost a day of debugging: a run proposed none, exactly as told,
and the checker marked it failed because nothing on disk had changed.

So **every run appends one line to `MEMORY.md`, without exception** — the
runs that propose, and the runs that deliberately do not:

```
2026-09-15 08:00 ET | proposed 2 (autumn cocoa sticker, rainy-window print) | 6 pending
2026-09-15 08:00 ET | proposed 0 — 6 already pending, nothing new that is not a rewording | 6 pending
```

That line is what separates "thought about it and declined" from "fell over".
The checker now reads MEMORY.md, so a run that proposes nothing and says why
passes cleanly. A run that writes neither fails, and should.
