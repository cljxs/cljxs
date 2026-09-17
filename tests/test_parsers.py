#!/usr/bin/env python3
"""
Every test here is a bug that actually shipped.

The pattern is always the same: a parser written against an imagined value,
never run against a real one, wrong in a way that only showed up hours later
on the droplet. So each test names the incident it guards, and the fixtures
are captured output rather than invented strings.

Standard library only - unittest, not pytest - so this runs on the droplet
with nothing installed:

    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))


def load(name, path):
    """Import a hyphenated script by path - most of scripts/ cannot be
    imported by name."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


preflight = load("preflight", "preflight.py")
healthcheck = load("healthcheck", "health-check.py")
trade = load("belfort_trade", "belfort-trade.py")
import et_time


class ModelSlug(unittest.TestCase):
    """preflight reported a healthy ace as broken, 2026-09-16.

    ace is configured `openrouter/openai/gpt-5-mini`: the provider prefix in
    front of the model. The pattern stopped at the second slash, truncated it
    to `openrouter/openai`, and blocked a deploy over a config that was fine.
    """

    def test_three_segment_slug_is_not_truncated(self):
        m = preflight.SLUG_RE.search("openrouter/openai/gpt-5-mini")
        self.assertEqual(m.group(0), "openrouter/openai/gpt-5-mini")

    def test_two_segment_slug_still_works(self):
        m = preflight.SLUG_RE.search("google/gemini-2.5-flash-lite")
        self.assertEqual(m.group(0), "google/gemini-2.5-flash-lite")

    def test_slug_survives_quotes_and_a_key_prefix(self):
        for line in ('"openrouter/openai/gpt-5-mini"',
                     "agents.entries.ace.model.primary = openrouter/openai/gpt-5-mini"):
            with self.subTest(line=line):
                self.assertEqual(preflight.SLUG_RE.search(line).group(0),
                                 "openrouter/openai/gpt-5-mini")

    def test_variant_suffix_is_kept(self):
        m = preflight.SLUG_RE.search("anthropic/claude-sonnet-4.5:thinking")
        self.assertEqual(m.group(0), "anthropic/claude-sonnet-4.5:thinking")

    def test_catalogue_lookup_strips_only_the_provider_prefix(self):
        preflight._catalogue = {"openai/gpt-5-mini"}
        self.assertTrue(preflight.in_catalogue("openrouter/openai/gpt-5-mini"))
        self.assertFalse(preflight.in_catalogue("openrouter/openai/gpt-4.1-mini"))


class InstructionCommands(unittest.TestCase):
    """ace's instructions said `ace-judge.py mark` before mark existed. It
    improvised, hand-edited bankroll.json in a loop and timed out - three
    cycles lost to a command that was not there."""

    def test_finds_a_command_behind_a_python3_invocation(self):
        found = set(preflight.CMD_RE.findall(
            "then run: python3 ../../scripts/ace-judge.py mark"))
        self.assertIn(("ace-judge.py", "mark"), found)

    def test_ignores_a_numeric_positional(self):
        # ace-verify.py takes a timestamp, not a subcommand.
        self.assertEqual(preflight.CMD_RE.findall("scripts/ace-verify.py 1758067200"), [])

    def test_ignores_a_flag(self):
        self.assertEqual(preflight.CMD_RE.findall("scripts/deploy.sh ace --yes-headers"), [])

    def test_reads_real_subcommands_out_of_argparse(self):
        # Not a hardcoded list: whatever --help actually accepts today.
        subs = preflight.subcommands("ace-judge.py")
        self.assertIsNotNone(subs, "ace-judge.py should expose subparsers")
        self.assertIn("mark", subs)

    def test_a_script_without_subparsers_reports_none(self):
        # signoff.py takes an agent name; `signoff.py ace` must not be read
        # as a bad subcommand.
        self.assertIsNone(preflight.subcommands("signoff.py"))


class ProviderErrors(unittest.TestCase):
    """A bare \\b401\\b matched `'schemaChars': 401` in a tool-schema dump and
    reported a working API key as rejected."""

    def run_checker(self, fixture, must_exist=True):
        # A fixture that is not there must fail the test, never pass it.
        # check-provider-error exits 0 for a missing log by design, which is
        # exactly what a clean log returns - so without this the whole class
        # reports green when the fixtures have gone missing. They did: .gitignore
        # ate both of them and CI was the only thing that noticed.
        if must_exist:
            self.assertTrue(Path(fixture).is_file(),
                            f"fixture missing: {fixture} - is it gitignored?")
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "check-provider-error.py"), str(fixture)],
            capture_output=True, text=True)

    def test_a_number_in_a_schema_dump_is_not_a_401(self):
        r = self.run_checker(FIXTURES / "schema-dump-401.log")
        self.assertEqual(r.returncode, 0, f"false positive:\n{r.stdout}")

    def test_a_real_401_is_still_caught(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("openrouter returned status 401 for this request\n")
        r = self.run_checker(f.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("KEY REJECTED", r.stdout)

    def test_the_timeout_ace_actually_hit(self):
        r = self.run_checker(FIXTURES / "ace-timeout.log")
        self.assertEqual(r.returncode, 2)
        self.assertIn("RAN OUT OF TIME", r.stdout)

    def test_no_credit(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("Error: insufficient balance on this account\n")
        r = self.run_checker(f.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("OUT OF CREDIT", r.stdout)

    def test_a_missing_log_is_not_evidence_of_anything(self):
        r = self.run_checker(FIXTURES / "does-not-exist.log", must_exist=False)
        self.assertEqual(r.returncode, 0)


class WakeSlots(unittest.TestCase):
    """Two agents filed reports under the wrong slot, both from reading a UTC
    clock: ace called a 23:31 ET wake `afternoon`, belfort called its 09:35 ET
    open `close`."""

    def test_ace_late_wake_is_night_not_afternoon(self):
        now = datetime(2026, 9, 15, 23, 31, tzinfo=timezone.utc)
        self.assertEqual(et_time.slot("ace", now), "night")

    def test_ace_afternoon_wake(self):
        now = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(et_time.slot("ace", now), "afternoon")

    def test_belfort_morning_open_is_not_close(self):
        now = datetime(2026, 9, 16, 9, 35, tzinfo=timezone.utc)
        self.assertEqual(et_time.slot("belfort", now), "open")

    def test_belfort_afternoon_close(self):
        now = datetime(2026, 9, 16, 15, 55, tzinfo=timezone.utc)
        self.assertEqual(et_time.slot("belfort", now), "close")

    def test_report_name_uses_the_eastern_day_not_the_utc_one(self):
        # 23:31 ET on the 15th is already the 16th in UTC. The name must say
        # the 15th - that was the whole bug.
        now = datetime(2026, 9, 15, 23, 31, tzinfo=timezone.utc)
        self.assertEqual(et_time.report_name("ace", now), "2026-09-15-night.md")


class CashIdentity(unittest.TestCase):
    """The paper account read -41% when the real loss was under 4%: four BUYs,
    zero SELLs, and $3,731.90 of proceeds never credited back to cash."""

    def test_a_balanced_book_has_no_gap(self):
        p = {"starting_cash": 10000.0, "cash": 9000.0,
             "trades": [{"side": "BUY", "notional": 1000.0}]}
        gap, bought, sold, expected, actual, unknown = trade.book_gap(p)
        self.assertEqual(gap, 0.0)
        self.assertEqual(bought, 1000.0)
        self.assertEqual(expected, 9000.0)
        self.assertEqual(unknown, [])

    def test_uncredited_sale_proceeds_show_up_as_a_gap(self):
        p = {"starting_cash": 10000.0, "cash": 9000.0,
             "trades": [{"side": "BUY", "notional": 1000.0},
                        {"side": "SELL", "notional": 500.0}]}
        gap, *_ = trade.book_gap(p)
        self.assertEqual(gap, -500.0, "a sale whose proceeds never reached cash")

    def test_notional_is_derived_when_absent(self):
        p = {"starting_cash": 10000.0, "cash": 9500.0,
             "trades": [{"side": "BUY", "shares": 5, "price": 100.0}]}
        gap, bought, *_ = trade.book_gap(p)
        self.assertEqual(bought, 500.0)
        self.assertEqual(gap, 0.0)

    def test_an_unrecognised_side_is_reported_not_silently_dropped(self):
        p = {"starting_cash": 10000.0, "cash": 10000.0,
             "trades": [{"side": "SHORT", "notional": 100.0}]}
        *_, unknown = trade.book_gap(p)
        self.assertEqual(len(unknown), 1)


class SystemdTimestamps(unittest.TestCase):
    """health-check reads systemd's own format; an unparsed stamp must read as
    'never ran', not as a crash."""

    def test_the_real_format(self):
        line = [l for l in (FIXTURES / "systemd-show.txt").read_text().splitlines()
                if l.startswith("ExecMainStartTimestamp=")][0].split("=", 1)[1]
        ts = healthcheck.parse_stamp(line)
        self.assertIsNotNone(ts)
        self.assertEqual(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                         "2026-09-16 18:55:37")

    def test_never_ran(self):
        for empty in ("", "   ", "n/a"):
            with self.subTest(v=empty):
                self.assertIsNone(healthcheck.parse_stamp(empty))

    def test_ago_reads_never_for_none(self):
        self.assertEqual(healthcheck.ago(None), "never")

    def test_ago_uses_hours_past_ninety_minutes(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=5)).timestamp()
        self.assertTrue(healthcheck.ago(past).endswith("h ago"))


class EveryScriptCompiles(unittest.TestCase):
    """A syntax error in a verifier is not found at edit time. It is found
    hours later, by the timer, after the model call has been paid for."""

    def test_all(self):
        for path in sorted(SCRIPTS.glob("*.py")):
            with self.subTest(script=path.name):
                r = subprocess.run([sys.executable, "-m", "py_compile", str(path)],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ReportNameAgreement(unittest.TestCase):
    """Belfort was told its report was both present and missing, and spent 49
    tool calls before offering to contact technical support.

    Commit 42d660b unified the case where the fetcher stamps a report_name
    into data/_meta.json. It left the FALLBACK divergent: with no stamped
    name, signoff accepted any fresh .md while the verifier demanded one
    computed filename. Same cycle, same files, opposite verdicts.

    So this does not test either function's internals. It puts real files on
    disk and asserts the two sides reach the same verdict about them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = Path(self.tmp.name) / "agents" / "belfort"
        (self.agent / "reports").mkdir(parents=True)
        (self.agent / "data").mkdir(parents=True)
        self.started = 0
        self.signoff = load("signoff", "signoff.py")
        self.verify = load("belfort_verify", "belfort-verify.py")

    def tearDown(self):
        self.tmp.cleanup()

    def verdicts(self):
        """(signoff is satisfied, verifier is satisfied) for what is on disk."""
        found, _why = self.signoff.newest_report(self.agent / "reports", self.started, "belfort")
        path, _slot = self.verify.expected_report("belfort", self.agent)
        return found is not None, Path(path).is_file()

    def test_they_agree_when_the_fetcher_stamped_a_name(self):
        (self.agent / "data" / "_meta.json").write_text(
            json.dumps({"report_name": "2026-09-17-close.md", "slot": "close"}))
        (self.agent / "reports" / "2026-09-17-close.md").write_text("# close\n")
        a, b = self.verdicts()
        self.assertEqual(a, b, "stamped name: both sides should see the report")
        self.assertTrue(a)

    def test_they_agree_when_the_stamped_report_is_genuinely_absent(self):
        (self.agent / "data" / "_meta.json").write_text(
            json.dumps({"report_name": "2026-09-17-close.md", "slot": "close"}))
        a, b = self.verdicts()
        self.assertEqual(a, b, "stamped name, no file: both sides should see it missing")
        self.assertFalse(a)

    def test_they_agree_with_no_meta_at_all(self):
        # THE LIVE BUG. No _meta.json, and a report under a name the agent
        # chose. signoff accepts it; the verifier computes a name of its own
        # and calls it missing - "both present and missing", verbatim.
        (self.agent / "reports" / "belfort-notes.md").write_text("# notes\n")
        a, b = self.verdicts()
        self.assertEqual(
            a, b,
            "no _meta.json: signoff and the verifier must not disagree - that "
            "contradiction is what sent belfort looking for technical support")

    def test_they_agree_when_meta_exists_but_carries_no_report_name(self):
        (self.agent / "data" / "_meta.json").write_text(json.dumps({"asof_utc": "x"}))
        (self.agent / "reports" / "belfort-notes.md").write_text("# notes\n")
        a, b = self.verdicts()
        self.assertEqual(a, b, "meta without report_name is the same case")

    def test_an_agent_that_names_its_own_reports_is_left_alone(self):
        # scout, emily and fury choose their own filenames. Unifying the
        # stricter rule must not start demanding a computed name from them -
        # that would break three working agents to fix one.
        name, slot_ = et_time.expected_report(self.agent, "scout")
        self.assertIsNone(name)
        self.assertIsNone(slot_)

    def test_the_slotted_agents_always_resolve_to_a_name(self):
        # belfort-verify and ace-verify build `reports / name` directly, so a
        # None here would be a TypeError at the worst possible moment.
        for agent in ("ace", "belfort"):
            with self.subTest(agent=agent):
                self.assertIn(agent, et_time.SLOTS)
                name, _ = et_time.expected_report(self.agent, agent)
                self.assertTrue(name and name.endswith(".md"))


class HeadersNameRealCommands(unittest.TestCase):
    """preflight.py catches a command that does not exist - but only on the
    droplet, after deploy.sh has built AGENTS.md from the header.

    The headers themselves are tracked, so the same check runs here, on every
    push. `ace-judge.py mark` was named in ace's instructions before mark
    existed; three cycles were lost to the agent improvising around it. This
    turns that into a failed build instead.
    """

    def test_every_command_in_every_header_exists(self):
        headers = sorted((ROOT / "agents").glob("*/_*-agents-header.md"))
        self.assertTrue(headers, "no instruction headers found - has the layout moved?")

        checked = 0
        for header in headers:
            agent = header.parent.name
            for script, sub in sorted(set(preflight.CMD_RE.findall(header.read_text()))):
                with self.subTest(agent=agent, cmd=f"{script} {sub}"):
                    self.assertTrue((SCRIPTS / script).is_file(),
                                    f"{agent}'s instructions call scripts/{script}, "
                                    f"which does not exist")
                    subs = preflight.subcommands(script)
                    if subs is not None:
                        self.assertIn(sub, subs,
                                      f"{agent}'s instructions say `{script} {sub}`, "
                                      f"but it accepts: {', '.join(sorted(subs))}")
                    checked += 1
        self.assertGreater(checked, 0, "no commands were checked - is CMD_RE matching?")


class MemoryRecording(unittest.TestCase):
    """Ace ran a clean cycle on 2026-09-17 - 32 calls, no failures, all four
    deliverables - and the unit reported failure because MEMORY.md went from
    258 bytes to 175.

    Three rules disagreed about one fact:
        instructions    append one line, trim oldest past ~2KB
        the verifiers   fail if the file did not grow
        signoff.py      pass if the last line is non-empty

    The first two cannot both hold. The first legitimate trim at the cap would
    have failed every cycle from then on, for ace, belfort and emily alike.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = Path(self.tmp.name)
        self.mem = self.agent / "MEMORY.md"
        self.remember = load("remember", "remember.py")

    def tearDown(self):
        self.tmp.cleanup()

    def test_appending_keeps_what_was_there(self):
        self.mem.write_text("- 2026-09-15 an older note\n")
        self.remember.remember(self.agent, "a newer note")
        text = self.mem.read_text()
        self.assertIn("an older note", text)
        self.assertIn("a newer note", text)

    def test_the_line_is_dated(self):
        self.remember.remember(self.agent, "something")
        self.assertRegex(self.mem.read_text().strip(), r"^- \d{4}-\d{2}-\d{2} something$")

    def test_an_empty_line_is_refused(self):
        with self.assertRaises(ValueError):
            self.remember.remember(self.agent, "   ")

    def test_trimming_drops_the_oldest_and_keeps_the_newest(self):
        self.mem.write_text("".join(f"- 2026-09-0{i%9+1} line number {i} padding padding "
                                    f"padding padding padding\n" for i in range(60)))
        before = self.mem.stat().st_size
        self.assertGreater(before, self.remember.MAX_BYTES)
        _, trimmed = self.remember.remember(self.agent, "the newest note")
        text = self.mem.read_text()
        self.assertGreater(trimmed, 0)
        self.assertLessEqual(len(text.encode()), self.remember.MAX_BYTES)
        self.assertIn("the newest note", text)
        self.assertNotIn("line number 0 ", text)

    def test_a_trim_that_SHRINKS_the_file_still_counts_as_recorded(self):
        # THE LANDMINE. Byte growth was the old test, so this exact case - the
        # cycle that records correctly and trims at the cap - was a guaranteed
        # failure for every cycle after the file first filled up.
        self.mem.write_text("".join(f"- 2026-09-01 padding line {i} "
                                    f"{'x' * 60}\n" for i in range(50)))
        before = self.mem.stat().st_size
        started = int(self.mem.stat().st_mtime) - 5
        self.remember.remember(self.agent, "recorded properly")
        after = self.mem.stat().st_size
        self.assertLess(after, before, "this test is pointless unless the file shrank")
        ok, why = self.remember.written_this_cycle(self.mem, started)
        self.assertTrue(ok, f"a trim at the cap must not read as a failed cycle: {why}")

    def test_a_file_untouched_by_this_run_does_not_count(self):
        self.mem.write_text("- 2026-09-15 written long ago\n")
        started = int(self.mem.stat().st_mtime) + 600
        ok, why = self.remember.written_this_cycle(self.mem, started)
        self.assertFalse(ok)
        self.assertIn("recorded nothing", why)

    def test_missing_and_empty_are_reported_apart(self):
        ok, why = self.remember.written_this_cycle(self.mem, 0)
        self.assertFalse(ok)
        self.assertIn("does not exist", why)
        self.mem.write_text("   \n")
        ok, why = self.remember.written_this_cycle(self.mem, 0)
        self.assertFalse(ok)
        self.assertIn("is empty", why)

    def test_it_works_for_the_agents_that_record_elsewhere(self):
        # scout and fury record into state/last-run.txt, not MEMORY.md.
        other = self.agent / "state" / "last-run.txt"
        other.parent.mkdir()
        other.write_text("cycle ok\n")
        ok, _ = self.remember.written_this_cycle(other, int(other.stat().st_mtime) - 5)
        self.assertTrue(ok)


class VerifierMessages(unittest.TestCase):
    """belfort was told: no reports/2026-09-17-cycle-report.md for this wake -
    the `this` slot files as `-this.md`.

    `this` was a display word in the old code that survived as a fallback slot
    NAME, so the verifier ended up instructing the agent to write a file called
    2026-09-17-this.md. A wrong filename in the error message is worse than no
    message: the agent does what it is told.
    """

    def test_no_verifier_invents_a_slot_named_this(self):
        for f in ("ace-verify.py", "belfort-verify.py"):
            with self.subTest(script=f):
                self.assertNotIn('or "this"', (SCRIPTS / f).read_text(),
                                 f"{f} still falls back to a slot called 'this'")

    def test_expected_report_reports_an_absent_slot_as_none(self):
        # A well-formed name with no slot key. This used to be written with
        # report_name "2026-09-17-cycle-report.md", which the stamp guard now
        # rejects outright - so the case has to be built from a name the
        # fetcher could really have written, or it tests the guard instead.
        tmp = tempfile.TemporaryDirectory()
        agent = Path(tmp.name)
        (agent / "data").mkdir()
        (agent / "data" / "_meta.json").write_text(
            json.dumps({"report_name": "2026-09-17-open.md"}))
        name, slot_ = et_time.expected_report(agent, "belfort")
        self.assertEqual(name, "2026-09-17-open.md")
        self.assertIsNone(slot_, "an absent slot must be None, not a word")
        tmp.cleanup()


class StampedNameIsNotTrustedBlindly(unittest.TestCase):
    """belfort was failed for "no reports/2026-09-17-cycle-report.md".

    No fetcher can produce that name - they stamp et_time.report_name(), which
    only yields <date>-<slot>.md, and every report on disk is slot-named. The
    agent has a write tool and data/_meta.json lives in its own workspace, so
    the file it is graded against is one it can edit. Marking your own exam.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = Path(self.tmp.name)
        (self.agent / "data").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def stamp(self, **meta):
        (self.agent / "data" / "_meta.json").write_text(json.dumps(meta))

    def test_a_wellformed_stamp_is_used(self):
        self.stamp(report_name="2026-09-17-open.md", slot="open")
        name, slot_ = et_time.expected_report(self.agent, "belfort")
        self.assertEqual(name, "2026-09-17-open.md")
        self.assertEqual(slot_, "open")

    def test_the_name_that_actually_appeared_is_rejected(self):
        self.stamp(report_name="2026-09-17-cycle-report.md")
        name, _ = et_time.expected_report(self.agent, "belfort")
        self.assertNotEqual(name, "2026-09-17-cycle-report.md")
        self.assertRegex(name, r"^\d{4}-\d{2}-\d{2}-(open|close)\.md$")

    def test_a_slot_from_another_agent_is_rejected(self):
        # belfort files open/close; ace files afternoon/night. A stamp that
        # crosses them is not something either fetcher wrote.
        self.stamp(report_name="2026-09-17-open.md")
        name, _ = et_time.expected_report(self.agent, "ace")
        self.assertRegex(name, r"^\d{4}-\d{2}-\d{2}-(afternoon|night)\.md$")

    def test_a_path_cannot_be_smuggled_through_the_stamp(self):
        self.stamp(report_name="../../../etc/passwd")
        name, _ = et_time.expected_report(self.agent, "belfort")
        self.assertNotIn("..", name)

    def test_self_naming_agents_keep_their_stamp(self):
        # scout, emily and fury have no slots and choose their own filenames,
        # so there is no shape to check and nothing to reject.
        self.stamp(report_name="whatever-they-called-it.md")
        name, _ = et_time.expected_report(self.agent, "scout")
        self.assertEqual(name, "whatever-they-called-it.md")


class SignoffWithoutAnEpoch(unittest.TestCase):
    """state/.cycle-started is written by <agent>-cycle.sh at wake. Without it
    signoff disabled every freshness rule and said nothing about having done
    so, while the verifier - handed the epoch on argv - failed the same run.
    Two checks, opposite verdicts, and the agent believes the one saying it is
    finished. That is what cost belfort 49 tool calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        agent = self.root / "agents" / "belfort"
        (agent / "data").mkdir(parents=True)
        (agent / "state").mkdir()
        (agent / "reports").mkdir()
        (agent / "data" / "_meta.json").write_text(
            json.dumps({"slot": "close", "report_name": "2026-09-17-close.md"}))
        (agent / "reports" / "2026-09-17-close.md").write_text("word " * 120)
        (agent / "MEMORY.md").write_text("- 2026-09-10 an old note\n")
        (agent / "state" / "portfolio.json").write_text(json.dumps(
            {"starting_cash": 10000.0, "cash": 10000.0, "positions": [],
             "trades": [], "cycle_count": 3}))
        self.agent = agent

    def tearDown(self):
        self.tmp.cleanup()

    def run_signoff(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "signoff.py"), "belfort"],
                              capture_output=True, text=True, env=env)

    def test_it_says_so_when_the_epoch_is_missing(self):
        r = self.run_signoff()
        self.assertIn(".cycle-started is missing", r.stdout)
        self.assertIn("NOT being checked", r.stdout)

    def test_it_stays_quiet_when_the_epoch_is_there(self):
        (self.agent / "state" / ".cycle-started").write_text(str(int(time.time()) - 60))
        r = self.run_signoff()
        self.assertNotIn(".cycle-started is missing", r.stdout)
