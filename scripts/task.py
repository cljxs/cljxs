#!/usr/bin/env python3
"""
task.py — an agent's two dealings with the task queue, as commands.

    python3 scripts/task.py read          what am I supposed to build?
    python3 scripts/task.py done "<one line about what I made>"

Task #6 was failed with "agent exited with code 0". The log shows Emily
printed the curl she had been told to write:

    Here's the command to complete the task:
    ```bash
    curl -s -X POST .../tasks/6/complete -H '...' -d '{"result":"...",...}'
    ```
    Proceeding with the completion now.

and then stopped. She narrated the command instead of running it, and the
dispatcher - correctly - failed a task still in_progress at exit.

Three things were being asked of a cheap model at once: substitute a number
into a `<placeholder>`, hand-write JSON inside single quotes inside a shell
command, and remember a flag. Every one of those is deterministic, and this
repo's rule is that deterministic work belongs in a script. So it is one here.

THE TASK NUMBER IS NOT TYPED. The dispatcher exports it, so `read` and `done`
default to the task the agent was woken for. A `<placeholder>` in an
instruction is a thing to get wrong; the same shape cost this project three
broken commands in one session.

Standard library only.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("MISSION_CONTROL_API", "http://127.0.0.1:3001")
TASK_ENV = "ECOSYSTEM_TASK_ID"


def wake_message(task_id, api_base=API_BASE):
    """What the dispatcher tells an agent on waking it.

    Here rather than in the dispatcher so that the commands an agent is given
    and the commands that exist are the same fact. They drifted once already:
    the header, the wake message and this queue's API each described completing
    a task in their own words.
    """
    return (f"Task #{task_id} is assigned to you.\n"
            f"Read it first:\n"
            f"  python3 ../../scripts/task.py read\n"
            f"The payload holds everything you need. When you have finished, "
            f"and only then:\n"
            f'  python3 ../../scripts/task.py done "<one line about what you made>"\n'
            f"Both commands already know the task number - do not type one.\n"
            f"If you cannot finish, run `done` anyway with an honest line "
            f"saying why: a task left unfinished blocks your next one.")


def task_id_from(argv_value=None):
    """The task number: an explicit argument, else the one we were woken for."""
    if argv_value:
        return str(argv_value).lstrip("#")
    env = os.environ.get(TASK_ENV, "").strip()
    if env:
        return env.lstrip("#")
    return None


def call(path, payload=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def explain(exc):
    """Say what actually went wrong, in a sentence an agent can act on."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "no task with that number - check the number in your wake message"
        return f"the queue answered {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return (f"cannot reach the queue at {API_BASE} "
                f"({exc.reason}) - the Command Deck may be down")
    return f"{type(exc).__name__}: {exc}"


def cmd_read(task_id):
    try:
        task = call(f"/tasks/{task_id}")
    except Exception as exc:
        print(f"could not read task #{task_id}: {explain(exc)}", file=sys.stderr)
        return 1

    # The payload is a JSON string inside a JSON field, so every agent that
    # read it had to unwrap it by hand. Unwrap it here.
    payload = task.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            pass

    print(f"Task #{task.get('id')}  [{task.get('status')}]  for {task.get('assignee')}")
    for field in ("notes", "kill_criteria"):
        if task.get(field):
            print(f"  {field}: {task[field]}")
    print("\npayload:")
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"  {key}: {value}")
    else:
        print(f"  {payload}")
    print(f"\nWhen you are finished:\n"
          f'  python3 ../../scripts/task.py done "<one line>"')
    return 0


def cmd_done(task_id, result, cost):
    if not result.strip():
        print("say what you did, in one line:\n"
              '  python3 ../../scripts/task.py done "drafted the hoodie"',
              file=sys.stderr)
        return 2
    try:
        call(f"/tasks/{task_id}/complete",
             {"result": result.strip(), "cost_actual": cost})
    except Exception as exc:
        print(f"could not complete task #{task_id}: {explain(exc)}", file=sys.stderr)
        return 1
    print(f"task #{task_id} completed: {result.strip()}")
    return 0


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "read"
    rest = argv[1:]

    # `done 6 "a line"` and `done "a line"` both work: a leading all-digits
    # argument is the task number. An agent that types the number anyway is
    # not punished for it.
    explicit = None
    if rest and rest[0].lstrip("#").isdigit():
        explicit, rest = rest[0], rest[1:]

    task_id = task_id_from(explicit)
    if task_id is None:
        print(f"no task number. The dispatcher normally sets {TASK_ENV}; "
              f"if you are running this by hand, give it:\n"
              f"  python3 scripts/task.py {cmd} 6"
              + (' "<one line>"' if cmd == "done" else ""), file=sys.stderr)
        return 2

    if cmd == "read":
        return cmd_read(task_id)
    if cmd == "done":
        cost = 0.0
        if "--cost" in rest:
            i = rest.index("--cost")
            try:
                cost = float(rest[i + 1])
            except (IndexError, ValueError):
                print("--cost wants a number, e.g. --cost 0.25", file=sys.stderr)
                return 2
            rest = rest[:i] + rest[i + 2:]
        return cmd_done(task_id, " ".join(rest), cost)

    print("usage: task.py read | task.py done \"<one line>\" [--cost 0.0]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
