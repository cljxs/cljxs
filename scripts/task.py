#!/usr/bin/env python3
"""
task.py — an agent's two dealings with the task queue, as commands.

    python3 scripts/task.py read          what am I supposed to build?
    python3 scripts/task.py done "<one line about what I made>"

    python3 scripts/task.py new emily --idea "..." --brief "..."   (you, not an agent)
    python3 scripts/task.py list

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
from pathlib import Path

API_BASE = os.environ.get("MISSION_CONTROL_API", "http://127.0.0.1:3001")
TASK_ENV = "ECOSYSTEM_TASK_ID"


def wake_message(task_id, api_base=API_BASE):
    """What the dispatcher tells an agent on waking it.

    Here rather than in the dispatcher so that the commands an agent is given
    and the commands that exist are the same fact. They drifted once already:
    the header, the wake message and this queue's API each described completing
    a task in their own words.

    THE NUMBER IS IN THE MESSAGE. It used to say "do not type one", because
    the dispatcher exported it - except the export reaches the openclaw CLI,
    not the agent, which openclaw runs from its gateway in another process.
    Emily ran the command, got "no task number", and was forbidden by this
    very message from doing the one thing that would have worked. An
    instruction that rules out the fallback turns a degraded path into a dead
    one.
    """
    return (f"Task #{task_id} is assigned to you.\n"
            f"Read it first:\n"
            f"  python3 ../../scripts/task.py read {task_id}\n"
            f"The payload holds everything you need. When you have finished, "
            f"and only then:\n"
            f'  python3 ../../scripts/task.py done {task_id} "<one line about '
            f'what you made>"\n'
            f"If you cannot finish, run `done` anyway with an honest line "
            f"saying why: a task left unfinished blocks your next one.")


# The dispatcher drops the task number here before waking an agent, and
# removes it when the agent exits. The environment variable below travels only
# as far as the openclaw CLI - openclaw runs the agent from its gateway, in
# another process that never sees it - so a file in the agent's own folder is
# what actually arrives.
TASK_FILE = Path("state") / "current-task"


def task_id_from(argv_value=None):
    """The task number: an argument, the file the dispatcher wrote, or the env.

    Three sources because the first two can each be absent, and being unable
    to name your own task is a failed cycle. An agent runs with its own folder
    as the working directory, so the relative path resolves to that agent.
    """
    if argv_value:
        return str(argv_value).lstrip("#")
    try:
        written = TASK_FILE.read_text().strip()
        if written:
            return written.lstrip("#")
    except OSError:
        pass
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


def cmd_list(status=None):
    """What is in the queue, newest first."""
    path = "/tasks" + (f"?status={status}" if status else "")
    try:
        tasks = call(path)
    except Exception as exc:
        print(f"could not read the queue: {explain(exc)}", file=sys.stderr)
        return 1
    if not tasks:
        print("nothing in the queue" + (f" with status {status}" if status else ""))
        return 0
    for t in sorted(tasks, key=lambda t: -(t.get("id") or 0)):
        line = (f"  #{t.get('id'):<4} {str(t.get('status')):<12} "
                f"{str(t.get('assignee')):<9}")
        # Absent, unreadable and empty are three different things. Rendering
        # all three as a blank line is how a broken payload looks like a task
        # with nothing in it - the same mistake scout-ideas.py had to unlearn.
        raw = t.get("payload")
        payload, note = raw, ""
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except Exception:
                payload, note = None, "(payload will not parse)"
        elif raw is None:
            note = "(no payload)"
        idea = payload.get("idea") if isinstance(payload, dict) else None
        print(line + (idea or note or t.get("type") or ""))
        if t.get("result"):
            print(f"       -> {t['result']}")
    return 0


def cmd_new(a):
    """Create a task, with this script owning the JSON.

    The queue takes a POST with a payload that is itself JSON, nested inside
    JSON, inside a shell quote. That is precisely the shape that made Emily
    print her completion command instead of running it - and leaving it for a
    human to hand-write, on a terminal that flattens multi-line pastes, is the
    same trap with a different victim. Fields go in as arguments; the quoting
    is not anyone's problem.
    """
    payload = {"idea": a.idea, "brief": a.brief}
    if a.product:
        payload["product"] = a.product
    if a.slug:
        payload["slug"] = a.slug
        payload["build_dir"] = f"builds/{a.slug}"
    if a.artwork:
        payload["artwork"] = a.artwork

    body = {
        "created_by": "you",
        "assignee": a.agent,
        "type": a.type,
        "payload": payload,
        "cost_estimate": a.cost,
        "notes": a.notes,
        "kill_criteria": a.kill,
    }
    if a.slug:
        body["dedupe_key"] = f"{a.agent}-build-{a.slug}"

    try:
        task = call("/tasks", body)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("error") or ""
        except Exception:
            pass
        if exc.code == 409:
            print(f"there is already an open task for this: {detail}\n"
                  f"  see it with: python3 scripts/task.py list", file=sys.stderr)
        elif exc.code == 429:
            print(f"a spend cap would be broken: {detail}\n"
                  f"  caps live in tasks/limits.json", file=sys.stderr)
        else:
            print(f"the queue refused it ({exc.code}): {detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"could not create the task: {explain(exc)}", file=sys.stderr)
        return 1

    print(f"task #{task.get('id')} created for {a.agent}: {a.idea}")
    print(f"  the dispatcher picks it up within a couple of seconds. Watch:\n"
          f"    journalctl -u task-dispatcher -f")
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

    # `new` and `list` are for a person at a terminal, not for an agent, so
    # they take flags and never touch ECOSYSTEM_TASK_ID.
    if cmd == "new":
        import argparse
        ap = argparse.ArgumentParser(prog="task.py new")
        ap.add_argument("agent")
        ap.add_argument("--idea", required=True)
        ap.add_argument("--brief", required=True)
        ap.add_argument("--product", default="")
        ap.add_argument("--slug", default="")
        ap.add_argument("--artwork", default="")
        ap.add_argument("--type", default="product-build")
        ap.add_argument("--cost", type=float, default=0.25)
        ap.add_argument("--notes", default="DRAFT ONLY - do not publish.")
        ap.add_argument("--kill", default="Stop if assets cannot be produced, "
                                          "or if the idea requires trademarked IP.")
        return cmd_new(ap.parse_args(rest))

    if cmd == "list":
        return cmd_list(rest[0] if rest else None)

    # `done 6 "a line"` and `done "a line"` both work: a leading all-digits
    # argument is the task number. An agent that types the number anyway is
    # not punished for it.
    explicit = None
    if rest and rest[0].lstrip("#").isdigit():
        explicit, rest = rest[0], rest[1:]

    task_id = task_id_from(explicit)
    if task_id is None:
        print(f"no task number, and none in {TASK_FILE} or {TASK_ENV}.\n"
              f"The number is in your wake message - pass it:\n"
              f"  python3 ../../scripts/task.py {cmd} 7"
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

    print("usage: task.py read | task.py done \"<one line>\" [--cost 0.0]\n"
          "       task.py new <agent> --idea \"...\" --brief \"...\" | task.py list",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
