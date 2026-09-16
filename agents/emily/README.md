# Emily — product creator (DRAFTS ONLY)

Builds a complete Etsy listing from an approved idea. Creates drafts; never
publishes. **Task-driven** — no timer, no heartbeat, costs nothing while idle.

## How Emily is triggered

The dispatcher only watches the **queue**. A folder, an email or a note is
inert. Emily fires when a row exists in `tasks/queue.db` with
`assignee="emily"`. The bridge that writes that row is:

    python3 /root/ecosystem/scripts/emily-new-build.py "Cosy Cabin Reading Poster" \
      --brief "warm muted cabin, rain on the window, for book lovers" \
      --product poster

It refuses duplicate ideas (dedupe on the slug) and caps at 3 builds a day
unless you pass `--force`. Within a couple of seconds the dispatcher claims
the task and wakes Emily.

## Install

Register FIRST — `openclaw agents add` seeds `AGENTS.md`, so Emily's
instructions merge in afterwards. **No systemd timer**: that is deliberate.

    openclaw agents add emily --workspace /root/ecosystem/agents/emily --non-interactive
    # merge the instructions on top of the seeded AGENTS.md.
    # Re-runnable: it REBUILDS AGENTS.md rather than prepending, so editing the
    # header and running it again does not leave two copies of every rule.
    # The first run on an already-merged file shows you the seam and asks.
    /root/ecosystem/scripts/merge-header.sh emily
    cp -n /root/ecosystem/agents/emily/MEMORY.seed.md /root/ecosystem/agents/emily/MEMORY.md
    cp -n /root/ecosystem/agents/emily/state/lessons.seed.md /root/ecosystem/agents/emily/state/lessons.md
    openclaw models auth paste-api-key --provider openrouter --agent emily
    openclaw agents list          # confirm emily's index before the next two lines
    openclaw config set 'agents.list[3].model' 'openrouter/openai/gpt-4o-mini'
    openclaw config set 'agents.list[3].thinkingDefault' 'low'
    openclaw gateway restart

The dispatcher re-scans `agents/` on every loop, so it finds Emily within a
couple of seconds. No restart is needed.

## External accounts

Emily builds locally without any of these. She just cannot create the drafts.

| Account | For | Where |
|---|---|---|
| **OpenRouter** | cover art and mockups | you already have it — same key, not a new provider |
| **Printify** | the product, and the Etsy listing | printify.com/app/account/api |

**No Etsy API key is needed.** Connect your Etsy shop inside Printify
(Stores → Connect → Etsy). Pressing Publish there creates the Etsy listing.
Etsy's own v3 API would mean an OAuth flow, hourly-expiring tokens and app
review, for a route that ends in the same place.

    cd /root/ecosystem/agents/emily/state
    cp credentials.env.example credentials.env
    chmod 600 credentials.env
    python3 /root/ecosystem/scripts/set-credential.py

That prompts for each value, hides it as you paste, strips the stray spaces
and quotes a phone paste brings, refuses a value with a space in it, and sets
the file to 600. Use it rather than an editor: nano is genuinely hard to
escape on a phone keyboard, and an edit you cannot save loses the paste
silently.

Then check it landed properly:

    python3 /root/ecosystem/scripts/check-credentials.py

That prints each key with a character count and four characters from each end
— enough to spot an empty or mangled value, safe to screenshot, and it never
prints a secret. It also repairs the paste this file invites: on a phone, a
long token often lands on the line *after* `KEY=`, which leaves the key empty
and orphans the token on a line every reader silently skips.

`credentials.env` is gitignored. **This repo is public — never commit it.**

## Which model

`gpt-4o-mini`. Emily's cycle is the longest in the ecosystem — generate the
art, write the listing copy, call Printify, write `build.json`, write a report,
append to memory. Every Gemini model tried in this ecosystem died partway
through a job of that shape, usually after doing the thinking and before
saving anything. Her build costs real money in image generation, so a model
that gives up halfway is the expensive kind of cheap.

## The build is checked

`scripts/emily-verify.py` runs after every build, fired by the dispatcher.
Completing a task goes through the API, so "done" only ever meant Emily said
so — and four agents here have finished a run having written nothing.

It fails the task unless the folder holds real artwork (`design.png` over 2KB),
a `listing.json` with a title, description and at most 13 tags of 20
characters or fewer, and a `build.json` with a status. A placeholder build
passes but is labelled loudly: it is a legitimate outcome when no image key is
set, and it is not sellable.

## Image generation

`scripts/emily-assets.py` turns a prompt into a real PNG.

- `OPENROUTER_API_KEY` set -> real art via `google/gemini-2.5-flash-image`
- no key -> a deterministic geometric placeholder at the right dimensions

It never writes a blank file, because a blank deliverable is a failed build.
The output JSON says which mode ran, and Emily records that in the build so a
placeholder build is never mistaken for one ready to publish.

## Checking on it

    curl -s 'http://127.0.0.1:3001/tasks?assignee=emily' | head -40
    journalctl -u task-dispatcher -n 50 --no-pager
    ls -t builds/ | head
    cat builds/<slug>/build.json
    tail MEMORY.md state/lessons.md

## Pausing it

Emily has no timer, so she is already silent unless you queue work. To stop
her entirely: `openclaw agents delete emily`, or move `agents/emily/` aside so
the dispatcher stops discovering her. Simply not queueing tasks costs nothing.

## Files

    _emily-agents-header.md       operating spec, merged into AGENTS.md
    MEMORY.seed.md                seed for the memory log
    state/lessons.seed.md         seed for the approve/reject log
    state/credentials.env.example template for the API tokens
    state/credentials.env         YOUR TOKENS (gitignored, never committed)
    builds/<slug>/                one folder per product (gitignored)
    reports/                      one report per build (gitignored)
