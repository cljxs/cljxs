# Stan - the opportunity hunter

Software pains people say out loud on Hacker News ("I would pay for...",
"why isn't there..."), judged once a day, for the owner to approve into the
Agent Room or reject with a reason. Built 2026-10-14 from the owner's Stan
spec (a trevsagents.com template), reworked to this ecosystem's rules: plain
code does everything it can, the AI reads and writes text only, and code
checks what it wrote.

    06:12 Central  stan-fetch.timer -> scripts/stan-fetch.py   no AI, free
    06:22 Central  stan-cycle.timer -> scripts/stan.py cycle   one cheap AI call

**Not an OpenClaw agent** (`NO_MODEL`): preflight's model column says "plain
code - no model", which is true of OpenClaw. The one direct call is to
`watchlist.json`'s model (google/gemini-2.5-flash-lite), on the shared
OpenRouter key, checked against OpenRouter before every call.

## What happens each day

1. **Fetch** (no AI). Each phrase in `watchlist.json` is searched on Hacker
   News as an exact phrase (Algolia's public HN search - no account). A post
   is kept only if the phrase is really in it; each post is fetched once
   (`state/seen-posts.json`). New posts go into `data/backlog.json`, as a
   short excerpt round the phrase.
2. **Read** (one AI call). Up to 40 unread posts, likeliest first, go to the
   model as a block of data. It says which describe a real, specific,
   buildable software pain, with quotes. Posts are never sent twice.
3. **Check** (code). Every quote must be in the post it names, word for word,
   or the problem is dropped. Scores: frequency 30% (different people,
   counted), pay 30% (capped at 3 without a checked "would pay" quote),
   buildability 25%, gap 15%. Kept at 6.0+, at most 5 a day. Anything like a
   problem ever surfaced before (`state/ledger.jsonl`, kept for good) is
   dropped, however worded.
4. **You decide** on the Deck's Stan tab, or in his house in the village.
   *Approve* starts an Agent Room run with the problem and its quotes (a
   practice run until the Agent Room has its own key). *Reject* needs a
   reason and goes to `state/dead-ideas.jsonl`. Both go to
   `state/lessons.jsonl`; the last 20 are in the next day's prompt.

## Money

About a fifth of a cent a day (believed - measured prices, estimated size).
Before the call: its worst case must fit `stan_daily_ai` ($0.05) and leave
`stan_leave_for_others` ($0.50) of the shared day, as OpenRouter counts it.
Unreadable is no. A day with no new posts makes no call at all.

## Safety

Posts are strangers' text. The model is told never to act on them, and has
nothing to act with: it returns text, and code reads it as data. Backticks
are neutralised so a post cannot close its data block. Nothing here posts,
comments or follows a link.

**Reddit is off**, on purpose (owner, 2026-10-14): its API needs approved
access and its terms may restrict commercial research and sending posts to
an AI. Not to be switched on until that is confirmed for this use.

## Files

    watchlist.json            the phrases, their weights, and Stan's settings (tracked)
    data/backlog.json         unread and recent posts (fetcher-owned)
    data/fetch-status.json    each phrase's last fetch
    state/problems.json       everything surfaced, and what you decided
    state/ledger.jsonl        every problem ever surfaced - never shown twice
    state/dead-ideas.jsonl    rejections and why
    state/lessons.jsonl       every decision, fed back into the prompt
    state/triaged.json        posts already read
    state/spend.json          what each UTC day's call cost
    state/last-cycle.json     the last run, as the page shows it
    reports/YYYY-MM-DD.md     each day's report
    MEMORY.md                 one line a day, kept under 2KB

## Checking and pausing

    python3 /root/ecosystem/scripts/stan.py cycle --dry       # a practice read: no AI, nothing written
    journalctl -u stan-cycle -n 20 --no-pager                  # what the last run said
    systemctl disable --now stan-fetch.timer stan-cycle.timer  # pause him
    systemctl enable --now stan-fetch.timer stan-cycle.timer   # start him again
