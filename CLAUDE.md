# Working in this repo

An ecosystem of scheduled AI agents on a single Ubuntu droplet. Each agent
wakes on a systemd timer, does a cycle of work, and is judged by a verifier
script rather than by what it says it did.

`BACKBONE.md` covers the task queue and API. This file is about how to change
things here without breaking them.

## Layout

    agents/<name>/       workspace: AGENTS.md, MEMORY.md, state/, reports/, data/
    scripts/             everything deterministic, in Python
    deploy/              systemd units, installed by scripts/deploy.sh
    mission-control-api/ the Command Deck (Express, port 3001)

`ECOSYSTEM_ROOT` overrides the root; nothing hardcodes a path.

## The two rules that caused every bug worth remembering

**One fact, one place.** Two pieces of code deciding the same thing always
drift. It has happened here with the report filename, the sign-off freshness
rule, the Deck's heartbeat, the folder resolver, the cash-balance identity and
the cycle timeout - every time discovered hours later, from an agent acting on
the answer that happened to be wrong. When you find a second opinion, delete
it and import the first: `belfort-verify.py` imports `book_gap` from
`belfort-trade.py`, `preflight.py` imports its systemd handling from
`health-check.py`. Do that rather than restating the logic.

**A model asked to do arithmetic will eventually claim it did.** Anything a
script can compute exactly belongs in a script, with the agent calling it.
That is why indicators, briefings, Printify drafts, portfolio arithmetic,
ledger rows and cycle-closing are all Python subcommands. Fury's whole cycle
is now Python and wakes no model at all.

## Verifying, before you say it works

Say which one you mean, every time:

* **verified** - you ran it and read the output.
* **believed** - it should work.

Most damage here came from believed-shipped-as-verified. A regex written
against an imagined value read `openrouter/openai/gpt-5-mini` as
`openrouter/openai` and reported a healthy agent as broken. One real sample
would have caught it. So: never ship a parser without running it against a
real value, and prefer a fixture you can re-run to a claim you cannot.

Two scripts answer different questions and neither answers the other's:

    scripts/preflight.py      could each agent run?  (files, units, model, credit)
    scripts/health-check.py   did it run?            (systemd vs disk vs Deck)

Both are free - no model is woken - so run them freely.

## Changing an agent's instructions

`agents/<name>/AGENTS.md` is **generated and untracked**. Edit
`agents/<name>/_<name>-agents-header.md`, then:

    scripts/deploy.sh <name>          # rebuilds AGENTS.md, installs units, restarts timers

Editing AGENTS.md directly is lost on the next rebuild. Skipping deploy.sh
means the agent keeps running last week's rules while the repo says otherwise
- preflight.py reports that as `stale/broken`.

## Config

The running config is `agents.entries.<name>`, **not** the `agents.list[N]`
in older install notes. Discover before you set:

    openclaw config get agents.entries.<name>.model
    scripts/set-agent-model.sh <name> <slug> --apply   # checks the slug exists first

A wrong model slug fails silently: the agent just reports "model not found"
at its next wake, hours later.

## Things that have bitten

* **Never `pkill -f` on a port or command pattern.** It has matched this
  session's own shell twice and killed it. Use a fresh port instead.
* **Credentials never leave the droplet.** They live in
  `agents/<name>/state/credentials.env`, mode 600. Do not print, echo or paste
  one - `scripts/check-credentials.py` shows enough to diagnose and nothing
  useful to a reader. A shop ID is an account number, not a secret; an API
  token is.
* **Times are Eastern, stored as UTC.** `scripts/et_time.py` owns the clock
  and the wake slots. Do not compute a slot name anywhere else.
* **Cost scales with turns, not tokens.** A 12-turn cycle on a dearer model
  beats a 68-turn cycle on a cheap one, because every turn re-reads the whole
  prompt. Fixing a loop saves more than switching models.
* **The user's terminal flattens multi-line pastes.** Give single-line
  commands - no heredocs, no multi-line `python3 -c`.

## Testing

There is no test suite yet, which is why bugs reach the droplet. When you add
one, fixtures of *real* captured output (an `openclaw config get`, an ESPN
payload, a `_meta.json`, a `last-run` JSON) are worth more than mocks: every
parser bug here was a format assumption, not a logic error.

## Running the tests

    python3 -m unittest discover -s tests -v      # no dependencies, runs anywhere

Every test names the bug it guards, and each was checked by reintroducing that
bug and confirming the test fails - a test that cannot fail is worse than no
test, because it reports safety it is not providing.

`scripts/capture-fixtures.py` refreshes `tests/fixtures/` from this droplet.
Fixtures are committed, so it masks every known credential value literally,
refuses to write a file where one survived, and refuses to overwrite a good
fixture with a command's error output. CI re-checks for credential-shaped
strings on every push, because redaction that depends on someone remembering
to run it is not redaction.
