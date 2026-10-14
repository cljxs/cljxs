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
  A real run with the agents' key is refused.
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
