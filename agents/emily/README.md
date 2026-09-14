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
    cd /root/ecosystem/agents/emily && cat _emily-agents-header.md AGENTS.md > .a && mv .a AGENTS.md
    cp -n /root/ecosystem/agents/emily/MEMORY.seed.md /root/ecosystem/agents/emily/MEMORY.md
    cp -n /root/ecosystem/agents/emily/state/lessons.seed.md /root/ecosystem/agents/emily/state/lessons.md
    openclaw models auth paste-api-key --provider openrouter --agent emily
    openclaw agents list          # confirm emily's index before the next two lines
    openclaw config set 'agents.list[3].model' 'openrouter/google/gemini-2.5-flash-lite'
    openclaw config set 'agents.list[3].thinkingDefault' 'low'
    openclaw gateway restart
    systemctl restart task-dispatcher

## External accounts

Emily builds locally without any of these. She just cannot create the drafts.

| Account | For | Where |
|---|---|---|
| **OpenRouter** | cover art and mockups | you already have it — same key, not a new provider |
| **Etsy API v3** | creating draft listings | etsy.com/developers/your-apps |
| **Printify** | the physical product, unpublished | printify.com/app/account/api |

    cd /root/ecosystem/agents/emily/state
    cp credentials.env.example credentials.env
    chmod 600 credentials.env
    nano credentials.env        # paste your tokens here, never into a chat

`credentials.env` is gitignored. **This repo is public — never commit it.**

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
