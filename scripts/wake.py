#!/usr/bin/env python3
"""
wake.py — wake one agent for one cycle, on the engine it is set to run on.

    wake.py --agent ace --message "..." --session-id wake-ace-123 --timeout 300 --json
    wake.py engines                 which engine and model each agent wakes on
    wake.py check                   is the Claude plan ready? one tiny run on it
    wake.py test [agent ...]        each plan agent, woken once with its real
                                    instructions but no tools - not a cycle

The flags are OpenClaw's own, so a cycle script swaps `openclaw agent` for
`python3 scripts/wake.py` and changes nothing else.

ENGINES (tasks/engines.json - one place, tracked):

  openclaw  OpenRouter through the OpenClaw gateway, paid per token. The
            default: an agent not named in the file runs exactly as before.
  claude    The owner's Claude subscription, through the Claude Code CLI
            (`claude -p`). No per-token bill; it draws on the plan's usage
            limits instead, the same limits the owner's own Claude use draws
            on.

THE CLAUDE ENGINE, and why each part is there:

  * Instructions. OpenClaw injects an agent's bootstrap files (AGENTS.md,
    SOUL.md, IDENTITY.md, USER.md, TOOLS.md, MEMORY.md) into its system
    prompt on every call. The same files, in the same order, become the
    system prompt here (--system-prompt-file), with a short preamble mapping
    OpenClaw's tool names onto Claude Code's. Claude Code's own coding-
    assistant prompt is replaced, not appended to: these agents are not
    coding assistants, and every turn re-reads the whole prompt.
  * No CLAUDE.md. An agent's folder sits inside this repo, whose CLAUDE.md is
    written for whoever develops the ecosystem. Auto-discovered, it would be
    read by Ace as instructions. CLAUDE_CODE_DISABLE_CLAUDE_MDS and
    CLAUDE_CODE_DISABLE_AUTO_MEMORY keep the agent to its own files.
  * Tools. Allowed by name, everything else refused without asking
    (--permission-mode dontAsk): nobody is there to answer a prompt.
  * Login. The plan's long-lived token from `claude setup-token`, kept in
    plan/credentials.env (mode 600, via set-credential.py plan) and passed as
    CLAUDE_CODE_OAUTH_TOKEN. ANTHROPIC_API_KEY is removed from the run's
    environment: if it were set, the CLI would bill the API instead of the
    plan, which is the one thing this engine exists not to do.
  * Fallback. When the plan refuses the run before the model does anything -
    login rejected, usage limit reached - and the agent allows it, the same
    wake is sent through OpenClaw instead, so a limit hit at 10pm does not
    cost Ace his grading wake. A run that failed after it started is never
    retried: half a cycle run twice is worse than one missed.

Every result is printed to stdout as the engine wrote it, so the journal,
last-run.py and model-trial.py read it - last-run.py knows both shapes.

Standard library only.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent
AGENTS = ROOT / "agents"
ENGINES = ROOT / "tasks" / "engines.json"
PLAN_CREDENTIALS = ROOT / "plan" / "credentials.env"
TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"

# OpenClaw's order. HEARTBEAT.md is left out: nothing here heartbeats.
BOOTSTRAP = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md", "MEMORY.md")

# What the agents do in OpenClaw, by Claude Code's names. Bash covers exec;
# Read covers view_image (it reads image files).
CLAUDE_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch")
DEFAULT_MAX_TURNS = 60

PREAMBLE = """You are {agent}, one of the agents in this ecosystem, woken on a schedule.
Nobody is watching this run and nobody will answer a question: when something
is unclear, make the sensible call and say so in your notes. Your working
directory is your own folder, {workspace}. Your instructions follow, taken from
the files in it.

These instructions were written for another runtime, so some tool names
differ. Where they say exec, run the command with Bash. Read, write and edit
files with Read, Write and Edit. web_search is WebSearch and web_fetch is
WebFetch. To look at an image (view_image), Read the image file. Tools that do
not exist here - progress_card, sessions_spawn, ask_user - are never needed:
do the work yourself, now, in this run.
"""


def _ea():
    spec = importlib.util.spec_from_file_location("emily_assets_wake", SCRIPTS / "emily-assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def engines(path=None):
    """The engines file, or an empty one. A file that will not parse is an
    error, not a silent fall back to OpenClaw: an agent the owner moved to
    the plan would otherwise quietly go back to spending money."""
    p = Path(path or ENGINES)
    if not p.is_file():
        return {"agents": {}}
    return json.loads(p.read_text())


def setting(agent, doc=None):
    """{engine, model, max_turns, fallback} for one agent."""
    doc = engines() if doc is None else doc
    s = dict((doc.get("agents") or {}).get(agent) or {})
    s.setdefault("engine", "openclaw")
    if s["engine"] not in ("openclaw", "claude"):
        raise SystemExit(f"wake: {agent}'s engine is {s['engine']!r} in {ENGINES} - "
                         f"it must be openclaw or claude")
    if s["engine"] == "claude" and not s.get("model"):
        raise SystemExit(f"wake: {agent} is set to the claude engine with no model in {ENGINES} - "
                         f"name one (haiku, sonnet, opus, or a full claude-... id)")
    s.setdefault("max_turns", DEFAULT_MAX_TURNS)
    s.setdefault("fallback", "openclaw")
    return s


def openclaw_cmd(a):
    return [os.environ.get("OPENCLAW_BIN", "openclaw"), "agent", "--agent", a.agent,
            "--message", a.message, "--session-id", a.session_id,
            "--timeout", str(a.timeout)] + (["--json"] if a.json else [])


def claude_bin():
    """The CLI, wherever its installer put it. The native installer writes to
    ~/.local/bin, which is not on a systemd unit's PATH."""
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "claude", Path("/root/.local/bin/claude")):
        if p.is_file():
            return str(p)
    return None


def system_prompt(agent, workspace):
    """The preamble, then each bootstrap file that exists, in OpenClaw's order."""
    parts = [PREAMBLE.format(agent=agent, workspace=workspace)]
    for name in BOOTSTRAP:
        f = Path(workspace) / name
        if f.is_file():
            parts.append(f"\n# {name}\n\n{f.read_text().strip()}\n")
    return "\n".join(parts)


def session_uuid(tag):
    """Claude wants a UUID; the cycle scripts make tags like wake-ace-1760.
    Derived, not random, so the journal's tag finds the session."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cljxs-wake:{tag}"))


def claude_cmd(binary, agent, s, message, session_id, prompt_file):
    return [binary, "-p", message,
            "--output-format", "json",
            "--model", str(s["model"]),
            "--max-turns", str(int(s["max_turns"])),
            "--system-prompt-file", str(prompt_file),
            "--permission-mode", "dontAsk",
            "--allowedTools", *CLAUDE_TOOLS,
            "--strict-mcp-config",
            # Not kept on disk: a 2 GB droplet does not need every cycle's
            # transcript twice - the journal already has the result.
            "--no-session-persistence",
            "--session-id", session_uuid(session_id)]


def claude_env(token, base=None):
    env = dict(os.environ if base is None else base)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    env[TOKEN_KEY] = token
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def plan_token():
    return os.environ.get(TOKEN_KEY) or _ea().read_env_file(PLAN_CREDENTIALS).get(TOKEN_KEY, "")


def refused_before_starting(result):
    """True when the plan turned the run away before the model did anything:
    an API error and no model usage at all (tests/fixtures/
    claude-cli-result-401.json). Only then is a fallback safe."""
    return (isinstance(result, dict) and result.get("is_error") is True
            and result.get("terminal_reason") == "api_error" and not result.get("modelUsage"))


def last_json(text):
    """The result object `claude -p --output-format json` printed: one JSON
    document, possibly after warnings."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.rfind("\n{")
    if start >= 0:
        try:
            return json.loads(text[start + 1:])
        except ValueError:
            return None
    return None


def run_claude(a, s):
    """(exit code, result dict or None). Prints the CLI's output as it is."""
    binary = claude_bin()
    if not binary:
        print("wake: the claude CLI is not installed - see plan/README.md", file=sys.stderr)
        return 127, None
    token = plan_token()
    if not token:
        print(f"wake: no {TOKEN_KEY} in {PLAN_CREDENTIALS} - run: claude setup-token, then "
              f"python3 scripts/set-credential.py plan {TOKEN_KEY}", file=sys.stderr)
        return 2, None
    workspace = AGENTS / a.agent
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix=f"wake-{a.agent}-", delete=False) as f:
        f.write(system_prompt(a.agent, workspace))
        prompt_file = f.name
    try:
        print(f"wake: {a.agent} on the Claude plan ({s['model']}, at most {s['max_turns']} turns), "
              f"session {a.session_id} = {session_uuid(a.session_id)}", file=sys.stderr)
        try:
            r = subprocess.run(claude_cmd(binary, a.agent, s, a.message, a.session_id, prompt_file),
                               cwd=workspace, env=claude_env(token), capture_output=True, text=True,
                               timeout=a.timeout)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            sys.stdout.write(out)
            # Said in the words check-provider-error.py already knows.
            print(f"wake: request timed out before a response - {a.agent} was cut off "
                  f"at {a.timeout}s", file=sys.stderr)
            return 124, None
        sys.stdout.write(r.stdout)
        if r.stdout and not r.stdout.endswith("\n"):
            sys.stdout.write("\n")
        if r.stderr:
            sys.stderr.write(r.stderr)
        return r.returncode, last_json(r.stdout)
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass


def cmd_wake(a):
    s = setting(a.agent)
    if s["engine"] == "openclaw":
        sys.stdout.flush()
        os.execvp(openclaw_cmd(a)[0], openclaw_cmd(a))
    code, result = run_claude(a, s)
    if (code == 2 and result is None) or code == 127 or refused_before_starting(result):
        if s.get("fallback") == "openclaw" and shutil.which(os.environ.get("OPENCLAW_BIN", "openclaw")):
            why = result.get("result") if isinstance(result, dict) else "the plan is not set up"
            print(f"wake: the Claude plan refused {a.agent}'s run before it started ({why}) - "
                  f"sending this wake through OpenClaw instead", file=sys.stderr)
            sys.stdout.flush()
            os.execvp(openclaw_cmd(a)[0], openclaw_cmd(a))
    return code


def woken():
    """Agents that wake a model: an instructions header, and either no timer
    (Emily, woken by the task queue) or a timer whose cycle comes through
    here. Fury has a header and a timer that runs plain Python."""
    out = []
    for d in sorted(AGENTS.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or (d / "RETIRED").is_file() or (d / "NO_MODEL").is_file():
            continue
        if not (d / f"_{d.name}-agents-header.md").is_file():
            continue
        unit = ROOT / "deploy" / f"{d.name}-cycle.service"
        sh = SCRIPTS / f"{d.name}-cycle.sh"
        if unit.is_file() and not (sh.is_file() and "wake.py" in sh.read_text()):
            continue
        out.append(d.name)
    return out


def cmd_engines(a):
    doc = engines()
    names = sorted(set(woken()) | set(doc.get("agents") or {}))
    for n in (a.agents or names):
        s = setting(n, doc)
        where = (f"Claude plan · {s['model']} · up to {s['max_turns']} turns"
                 f"{' · falls back to OpenRouter' if s['fallback'] == 'openclaw' else ''}"
                 if s["engine"] == "claude" else "OpenRouter (OpenClaw)")
        print(f"  {n:<9} {where}")
    return 0


def cmd_check(a):
    """Installed, logged in, and answering - one tiny run on the plan."""
    binary = claude_bin()
    if not binary:
        print("not installed. On the droplet:  curl -fsSL https://claude.ai/install.sh | bash")
        return 1
    v = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
    print(f"claude CLI:  {binary}  ({(v.stdout or v.stderr).strip()})")
    token = plan_token()
    if not token:
        print(f"no login token. Make one:  {binary} setup-token")
        print(f"then store it:  python3 scripts/set-credential.py plan {TOKEN_KEY}")
        return 1
    print(f"token:       stored ({len(token)} characters)")
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run([binary, "-p", "Reply with the single word OK.", "--output-format", "json",
                            "--model", a.model, "--max-turns", "1", "--tools", "",
                            "--no-session-persistence"],
                           cwd=d, env=claude_env(token), capture_output=True, text=True, timeout=180)
    res = last_json(r.stdout) or {}
    if res.get("is_error") or r.returncode:
        print(f"the plan refused a test run: {res.get('result') or (r.stderr or r.stdout).strip()[:300]}")
        return 1
    print(f"test run:    answered {res.get('result')!r} on {', '.join(res.get('modelUsage') or {}) or a.model} "
          f"in {res.get('duration_ms', 0) / 1000:.1f}s")
    print("The plan is ready. Move an agent onto it in tasks/engines.json.")
    return 0


TEST_MESSAGE = ("This is a connection test, not a cycle. Do not do your cycle and do not try to "
                "use any tool. Reply with one line: your name, and the first thing your "
                "instructions say every cycle must produce.")


def cmd_test(a):
    """Each agent on the plan, woken once on its own model with its own
    system prompt - the part of every turn that costs the most - and no
    tools, so nothing is written and no cycle runs. Proves the login, the
    model name and the instructions load, and prints what one turn of that
    agent costs at list price."""
    binary, token = claude_bin(), plan_token()
    if not (binary and token):
        print("not ready - run: python3 scripts/wake.py check")
        return 1
    doc = engines()
    names = a.agents or sorted(n for n in (doc.get("agents") or {}) if setting(n, doc)["engine"] == "claude")
    if not names:
        print("no agent is on the plan yet - see tasks/engines.json")
        return 1
    bad = 0
    for n in names:
        s = setting(n, doc)
        if s["engine"] != "claude":
            print(f"  {n:<9} not on the plan (tasks/engines.json)")
            continue
        workspace = AGENTS / n
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(system_prompt(n, workspace))
            prompt_file = f.name
        try:
            r = subprocess.run([binary, "-p", TEST_MESSAGE, "--output-format", "json", "--model", str(s["model"]),
                                "--max-turns", "1", "--tools", "", "--system-prompt-file", prompt_file,
                                "--strict-mcp-config", "--no-session-persistence"],
                               cwd=workspace, env=claude_env(token), capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            print(f"  {n:<9} FAILED - no answer in 5 minutes")
            bad += 1
            continue
        finally:
            os.unlink(prompt_file)
        res = last_json(r.stdout) or {}
        mu = res.get("modelUsage") or {}
        read = sum((m or {}).get(k) or 0 for m in mu.values()
                   for k in ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens"))
        if res.get("is_error") or r.returncode:
            print(f"  {n:<9} FAILED - {res.get('result') or (r.stderr or r.stdout).strip()[:200]}")
            bad += 1
            continue
        said = " ".join(str(res.get("result") or "").split())[:110]
        print(f"  {n:<9} ok  {', '.join(mu) or s['model']}  {read:,} tokens read  "
              f"${res.get('total_cost_usd') or 0:.4f} list price\n            said: {said}")
    print("\nList price is what this would cost on the API; on the plan it comes out of"
          "\nyour usage allowance instead. A real cycle is this times its turns, mostly"
          "\nre-read from the cache at a tenth of the price.")
    return 1 if bad else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["engines"]:
        ap = argparse.ArgumentParser(prog="wake.py engines")
        ap.add_argument("agents", nargs="*")
        return cmd_engines(ap.parse_args(argv[1:]))
    if argv[:1] == ["test"]:
        ap = argparse.ArgumentParser(prog="wake.py test")
        ap.add_argument("agents", nargs="*")
        return cmd_test(ap.parse_args(argv[1:]))
    if argv[:1] == ["check"]:
        ap = argparse.ArgumentParser(prog="wake.py check")
        ap.add_argument("--model", default="haiku")
        return cmd_check(ap.parse_args(argv[1:]))
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--agent", required=True)
    ap.add_argument("--message", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--json", action="store_true")
    return cmd_wake(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
