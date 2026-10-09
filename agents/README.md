# agents/

One folder per agent. The dispatcher auto-discovers every subdirectory here —
no names are hardcoded anywhere.

Each agent folder should contain:

    agents/<name>/
      IDENTITY.md    who the agent is, its role and boundaries
      PLAYBOOK.md    how it does its work, step by step
      MEMORY.md      what it has learned and should remember
      data/          its working files
      reports/       its output

The folder name IS the agent name. A task is routed to an agent by setting
the task's `assignee` field to that folder name.

This directory is intentionally empty for now — agents arrive in the next step.
