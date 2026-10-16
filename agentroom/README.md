# The Agent Room

An idea goes in; a phased build plan comes out, reviewed by two other AIs
and waiting for the owner's Approve. Built 2026-10-14 from
[HANDOFF-AGENT-ROOM.md](../HANDOFF-AGENT-ROOM.md), after screenshots of a
tool called CoreOps.

    owner's idea  (the page, /agentroom)
      -> PLANNER (Kimi) writes a phased plan; code checks every phase has a
         check a program can run
      -> REVIEWERS (DeepSeek, Codex) critique it
      -> PLANNER revises it (skipped when every reviewer approves with
         nothing above a low concern)
      -> waits for the owner: Approve / Send back with a note / Stop
      -> Approve writes PLAN.md and plan.json into /root/builds/<project>/
         and commits them there

**Stage 1 only.** Building the phases (a coding agent, then a bug check,
phase by phase) is stage 2 and is not here yet.

## Where things are

    scripts/agentroom.py          everything that decides: states, checks, money, the runner
    agentroom/roles.json          which model plays which part (tracked - change it here)
    agentroom/state/              on the droplet only (gitignored):
      room.db                       runs, messages, plans, reviews
      credentials.env               AGENTROOM_OPENROUTER_KEY, mode 600
      models.json                   OpenRouter's price list, cached a few hours
      captured/                     the first real reply from each model, to make fixtures of
    mission-control-api/agentroom.js         the Deck's routes - no rules, runs agentroom.py
    mission-control-api/public/agentroom.html   the page
    deploy/agentroom-runner.service          the runner, always on
    /root/builds/<project>/       approved plans - outside the ecosystem on purpose

## Money

* Its **own OpenRouter key**, never the agents' key - OpenRouter counts
  spend per key, and on the shared key it would spend the agents' $1.50.
  A real run with any key the agents already use (the shared one, or an
  agent's own, like Paul's) is refused.
* `agentroom_daily_ai` ($3) and `agentroom_run_max` ($1) in
  `tasks/limits.json`. Before every call, the call's worst case (its prompt
  plus `max_tokens`, at OpenRouter's published price) is checked against
  both. The day is OpenRouter's own count on the Agent Room's key.
* `max_calls_per_run` in roles.json stops a loop.
* The key's own daily limit on openrouter.ai is the hard stop.

`python3 scripts/agentroom.py check` - free - says whether each model exists,
what it costs, and the worst case of one planning run.

## Practice runs

Tick **Practice run** and no model is called: made-up replies go through
the same parsers, checks and states. Free, and works with no key.

## Not verified yet

The reader of a real OpenRouter reply (`reply_text`) has not met a real
Agent Room reply - there was no key where it was written. The first real
reply from each model is saved to `agentroom/state/captured/`; turn one
into a fixture in `tests/fixtures/` and test against it.

## Stage 2: the builder (owner, 2026-10-15)

Approve a plan, press **Start building**, and Claude Code builds it phase by
phase - at night, in a sandbox, watched. Everything below is in
`scripts/agentroom.py` (stage 2) and `scripts/agentroom-guard.py`.

**When.** Only 01:00-03:00 Central (`roles.json` builder.window), at most 3
phases a night, 9 sessions, and `night_cap_usd` ($5) of plan use a night as
Claude counts it at list prices. Then the run waits for you (`phase_ready`):
look at the phases and the app, press **Continue**, and it carries on the
next night. At 3:00 a session in progress is stopped and carries on tomorrow.

**Who.** Its own Linux account, `agentbuilder`, which cannot read `/root`.
Its own Claude login, `AGENTROOM_BUILDER_TOKEN` - never the agents' one, so
it can be cancelled alone. The login reaches only the claude program (stdin
to a wrapper - never a file, an argument or a unit setting), and
`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` keeps it from every command Claude runs.

**The sandbox** (systemd-run, enforced by the kernel): 700MB of memory, half
the CPU, 256 processes; the system read-only except the builder's home;
`/root` invisible; no way to become root; everything it started is killed
when the session ends. Tests and git run in it with no network, no login and
no namespaces. Claude's bubblewrap (which hides the login from its commands)
needs namespaces, which Ubuntu 24.04 locks: the setup script gives the builder
its own copy of bwrap and an AppArmor rule for that copy only
(`/etc/apparmor.d/agentroom-bwrap`), and the guard refuses bwrap and unshare
in the AI's commands.

**The guard** sees every tool call first (a PreToolUse hook Claude cannot
switch off: `--setting-sources ""` and a root-owned `--settings`). It refuses
writes outside the project, sudo, cron, background processes, network tools,
the environment and `/proc`, code from the internet (`| sh`, npx, pip...),
and npm with its safety off. **Packages:** install scripts never run; a
package is installed only if that version is a week old, the package a month
old, it is used (500+ downloads a week) and not deprecated - package.json
included.

**Held.** After every session: tripwires (5+ refused commands, files changed
outside the project, a project over 300MB) and the watchdog (a different AI,
`roles.json` watchdog) read every command and change. Anything wrong - or a
watchdog reply that cannot be read - HOLDS the build: the Deck and
Sylvester's lamp turn red until you press **Resume** or **Stop**.

**Done** means `bash check.sh` passed, run by code, offline. Then the bug
checker reads the phase, it is committed, and the next one starts.

The apps open on their own port (the Deck's + 1, so 3002), a different
origin from the Deck: an app's code cannot press the Deck's buttons.

### Setting it up, on the droplet

    bash /root/ecosystem/scripts/agentroom-builder-setup.sh
    /root/.local/bin/claude setup-token
    python3 /root/ecosystem/scripts/set-credential.py agentroom AGENTROOM_BUILDER_TOKEN
    python3 /root/ecosystem/scripts/agentroom.py builder-login
    python3 /root/ecosystem/scripts/agentroom.py builder-check
    python3 /root/ecosystem/scripts/agentroom.py builder-selftest

`builder-check` is free: it tries each guard for real and every line must say
PASS. `builder-selftest` runs one small Claude session on the builder's login
(a few cents of plan use) and proves the guard refuses things and that
commands cannot see the login. Do not build until both pass.

**Not verified where this was written** (no systemd, no Claude login there):
the sandbox, the hook being honoured and the login being hidden were checked
by unit tests against the commands they produce, and are proven on the
droplet only by the two commands above.
