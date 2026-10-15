"""wake.py: an agent wakes on OpenClaw (OpenRouter) or on the owner's Claude
plan (`claude -p`), and everything that reads a run reads both.

    python3 -m unittest tests.test_wake -b

The Claude results in tests/fixtures/claude-cli-result-*.json are real,
captured from Claude Code 2.1.300 on 2026-10-15: a one-word answer, a run
that used tools, a run stopped at its turn limit, a login the API rejected,
and a wake built by wake.py itself (its own system prompt, tools and folder).
Only the session ids were replaced. The CLI here is a stand-in that records
what it was given and prints one of those results.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def result(name):
    return (FIX / f"claude-cli-result-{name}.json").read_text()


FAKE_CLAUDE = """#!/bin/bash
printf '%s\\n' "$@" > "$REC/claude-args"
pwd > "$REC/claude-cwd"
env > "$REC/claude-env"
cat "$REC/claude-says"
exit "${FAKE_EXIT:-0}"
"""
FAKE_OPENCLAW = """#!/bin/bash
printf '%s\\n' "$@" > "$REC/openclaw-args"
echo '{"openclaw": "ran"}'
"""


class WakeOnEitherEngine(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rec = self.root / "rec"
        self.rec.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("claude", FAKE_CLAUDE), ("openclaw", FAKE_OPENCLAW)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        ws = self.root / "agents" / "ace"
        ws.mkdir(parents=True)
        (ws / "AGENTS.md").write_text("# Ace\nBet only when the bar is cleared.\n")
        (ws / "MEMORY.md").write_text("- cycle 41: passed on everything\n")
        (ws / "HEARTBEAT.md").write_text("heartbeat chores\n")
        (self.root / "tasks").mkdir()
        (self.root / "plan").mkdir()
        (self.root / "plan" / "credentials.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=plan-login-placeholder\n")

    def engines(self, agents):
        (self.root / "tasks" / "engines.json").write_text(json.dumps({"agents": agents}))

    def wake(self, says="ok", code=0):
        (self.rec / "claude-says").write_text(result(says))
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root), REC=str(self.rec), FAKE_EXIT=str(code),
                   CLAUDE_BIN=str(self.bin / "claude"), OPENCLAW_BIN=str(self.bin / "openclaw"),
                   PATH=f"{self.bin}:{os.environ.get('PATH', '')}", ANTHROPIC_API_KEY="api-key-placeholder")
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return subprocess.run([sys.executable, str(SCRIPTS / "wake.py"), "--agent", "ace", "--message", "Scheduled cycle.",
                               "--session-id", "wake-ace-1760", "--timeout", "60", "--json"],
                              capture_output=True, text=True, env=env, timeout=60)

    def ran(self, which):
        f = self.rec / f"{which}-args"
        return f.read_text().splitlines() if f.exists() else None

    def test_an_agent_not_in_the_file_runs_on_openclaw_exactly_as_before(self):
        # The cycle scripts swapped `openclaw agent` for wake.py. With no
        # engines.json at all, nothing may change: same flags, same order.
        r = self.wake()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ran("openclaw"), ["agent", "--agent", "ace", "--message", "Scheduled cycle.",
                                                "--session-id", "wake-ace-1760", "--timeout", "60", "--json"])
        self.assertIsNone(self.ran("claude"))

    def test_on_the_plan_the_agent_gets_its_own_files_and_nothing_else(self):
        self.engines({"ace": {"engine": "claude", "model": "haiku"}})
        r = self.wake()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), json.loads(result("ok")), "the CLI's result is passed on as it is")
        args = self.ran("claude")
        self.assertEqual(args[:2], ["-p", "Scheduled cycle."])
        self.assertEqual(args[args.index("--model") + 1], "haiku")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk", "nobody is there to answer a prompt")
        self.assertIn("Bash", args)
        self.assertEqual((self.rec / "claude-cwd").read_text().strip(), str(self.root / "agents" / "ace"))
        env = dict(l.split("=", 1) for l in (self.rec / "claude-env").read_text().splitlines() if "=" in l)
        self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), "plan-login-placeholder")
        # Set, the CLI bills the API instead of the plan.
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        # The repo's CLAUDE.md is for whoever develops the ecosystem; read by
        # the agent it became instructions (a real run, 2026-10-15).
        self.assertEqual(env.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS"), "1")

    def test_the_system_prompt_is_openclaws_bootstrap_files_in_its_order(self):
        w = load("wake_t1", "wake.py")
        text = w.system_prompt("ace", self.root / "agents" / "ace")
        self.assertLess(text.index("Bet only when the bar is cleared."), text.index("cycle 41: passed"))
        self.assertNotIn("heartbeat chores", text)
        self.assertIn("Bash", text.split("# AGENTS.md")[0], "the tool names are mapped before the instructions")

    def test_a_run_the_plan_refused_before_starting_goes_through_openclaw(self):
        self.engines({"ace": {"engine": "claude", "model": "haiku"}})
        r = self.wake("401", code=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self.ran("openclaw"), "a login failure cost the wake")
        self.assertIn("sending this wake through OpenClaw instead", r.stderr)

    def test_no_fallback_when_the_owner_said_none(self):
        self.engines({"ace": {"engine": "claude", "model": "haiku", "fallback": "none"}})
        r = self.wake("401", code=1)
        self.assertEqual(r.returncode, 1)
        self.assertIsNone(self.ran("openclaw"))

    def test_a_run_that_had_started_is_never_run_twice(self):
        # Stopped at its turn limit after doing some of the work: running the
        # cycle again on OpenClaw would do that work a second time.
        self.engines({"ace": {"engine": "claude", "model": "haiku"}})
        r = self.wake("max-turns", code=1)
        self.assertEqual(r.returncode, 1)
        self.assertIsNone(self.ran("openclaw"))

    def test_the_plan_with_no_model_named_is_refused_not_guessed(self):
        self.engines({"ace": {"engine": "claude"}})
        r = self.wake()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no model", r.stderr)
        self.assertIsNone(self.ran("claude"))
        self.assertIsNone(self.ran("openclaw"))


class EveryReaderReadsBothShapes(unittest.TestCase):

    def test_last_run_reads_a_real_wake(self):
        lr = load("last_run_wake", "last-run.py")
        raw = result("wake")
        d = lr.last_block(raw)
        doc = json.loads(raw)
        mu = doc["modelUsage"]
        self.assertEqual(d["engine"], "claude")
        self.assertEqual(d["model"], next(iter(mu)))
        self.assertEqual((d["result"], d["assistantTurns"]), ("ok", doc["num_turns"]))
        # Summed over modelUsage, which counts the CLI's own small calls too.
        self.assertEqual(d["usage"]["input"], sum(m["inputTokens"] + m["cacheCreationInputTokens"] for m in mu.values()))
        self.assertNotEqual(d["usage"]["input"], doc["usage"]["input_tokens"] + doc["usage"]["cache_creation_input_tokens"],
                            "the fixture must tell the two sums apart, or this test proves nothing")
        self.assertEqual(d["costUsd"], doc["total_cost_usd"])

    def test_a_refused_run_has_no_model_and_says_why(self):
        lr = load("last_run_wake2", "last-run.py")
        d = lr.run_view(json.loads(result("401")))
        self.assertIsNone(d["model"])
        self.assertEqual(d["result"], "api_error")

    def test_the_trial_report_counts_a_refused_run(self):
        mt = load("model_trial_wake", "model-trial.py")
        rows = mt.runs(result("401") + "\n" + result("ok"))
        self.assertEqual([r["model"] for r in rows], ["claude plan (refused)", "claude-haiku-5-5"])
        self.assertEqual(rows[1]["engine"], "claude")

    def test_provider_check_names_the_plan_not_openrouter(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(result("401"))
        self.addCleanup(os.unlink, f.name)
        r = subprocess.run([sys.executable, str(SCRIPTS / "check-provider-error.py"), f.name],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("CLAUDE PLAN LOGIN REJECTED", r.stdout)
        self.assertNotIn("OPENROUTER_API_KEY", r.stdout)

    def test_a_refusal_wake_recovered_from_is_not_a_failure(self):
        log = result("401") + ("\nwake: the Claude plan refused ace's run before it started (x) - "
                               "sending this wake through OpenClaw instead\n") + '{"status": "ok"}\n'
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(log)
        self.addCleanup(os.unlink, f.name)
        r = subprocess.run([sys.executable, str(SCRIPTS / "check-provider-error.py"), f.name],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_successful_runs_are_not_provider_errors(self):
        for name in ("ok", "tools", "wake", "max-turns"):
            r = subprocess.run([sys.executable, str(SCRIPTS / "check-provider-error.py"),
                                str(FIX / f"claude-cli-result-{name}.json")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stdout}")

    def test_the_dispatcher_says_why_a_task_run_stopped(self):
        os.environ.setdefault("ECOSYSTEM_ROOT", str(ROOT))
        td = load("dispatcher_wake", "task-dispatcher.py")
        self.assertIn("used all its turns", td.why_it_stopped(result("max-turns")))
        self.assertIn("refused the run", td.why_it_stopped(result("401")))
        self.assertIsNone(td.why_it_stopped(result("ok")))


if __name__ == "__main__":
    unittest.main()
