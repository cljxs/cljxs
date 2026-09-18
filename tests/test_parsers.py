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
import re
import struct
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

    def test_they_agree_when_the_report_is_there(self):
        # The name comes from the clock now, not the stamp, so the test has to
        # ask for it rather than hardcode a slot - hardcoding one made this
        # fail at 11am and pass at 4pm.
        want, _ = et_time.expected_report(self.agent, "belfort")
        (self.agent / "data" / "_meta.json").write_text(
            json.dumps({"report_name": want, "slot": _}))
        (self.agent / "reports" / want).write_text("# report\n")
        a, b = self.verdicts()
        self.assertEqual(a, b, "both sides should see the report")
        self.assertTrue(a)

    def test_they_agree_when_the_report_is_genuinely_absent(self):
        want, slot_ = et_time.expected_report(self.agent, "belfort")
        (self.agent / "data" / "_meta.json").write_text(
            json.dumps({"report_name": want, "slot": slot_}))
        a, b = self.verdicts()
        self.assertEqual(a, b, "no file: both sides should see it missing")
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

    def test_a_slotted_agent_always_gets_a_real_slot_name(self):
        # The fallback used to be the word "this", which the verifier then
        # printed as a slot: "the this slot files as `-this.md`". A slotted
        # agent now resolves from the clock, so the slot is always one of its
        # own and never a word from a sentence.
        tmp = tempfile.TemporaryDirectory()
        agent = Path(tmp.name)
        (agent / "data").mkdir()
        (agent / "data" / "_meta.json").write_text(json.dumps({"report_name": "x.md"}))
        for name, slots in (("belfort", {"open", "close"}),
                            ("ace", {"afternoon", "night"})):
            with self.subTest(agent=name):
                _, slot_ = et_time.expected_report(agent, name)
                self.assertIn(slot_, slots)
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

    def test_a_stamp_that_matches_the_clock_is_what_you_get(self):
        # Pin the clock. This was written before the stamp stopped deciding
        # anything, so it asserted a hardcoded "open" against whatever time the
        # suite happened to run at - green all morning, red from noon Eastern.
        # The second time-of-day bomb in this file today.
        morning = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        self.stamp(report_name="2026-09-17-open.md", slot="open")
        name, slot_ = et_time.expected_report(self.agent, "belfort", now=morning)
        self.assertEqual(name, "2026-09-17-open.md")
        self.assertEqual(slot_, "open")

    def test_no_test_in_this_file_depends_on_the_time_of_day(self):
        """Both bombs were the same shape: a hardcoded slot with no `now`.
        expected_report is clock-driven, so every call that asserts a specific
        filename has to pin the clock or it is only true for part of the day."""
        src = Path(__file__).read_text()
        for i, line in enumerate(src.splitlines(), 1):
            if "expected_report(" not in line or "def " in line:
                continue
            window = "\n".join(src.splitlines()[i - 1:i + 2])
            if "now=" in window or "started=" in window:
                continue
            # Calls that do not assert a specific name are fine.
            self.assertNotRegex(
                window, r'assertEqual\(\s*name,\s*"\d{4}-\d{2}-\d{2}-',
                f"line {i}: asserts a dated filename without pinning the clock")

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


class TheClockDecidesNotTheStamp(unittest.TestCase):
    """Belfort's 14:12 UTC cycle - 10:12 ET, the open slot - wrote
    2026-09-17-close.md, and belfort-verify PASSED it, reporting that name.
    The only place it could have read that name is data/_meta.json, which the
    fetcher had stamped `open` and re-stamped `open` half an hour later.

    Checking the stamp's shape was not enough: `close` is a real belfort slot,
    just the wrong half of the day. A file inside the workspace of the agent
    being graded cannot decide what it is graded on.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = Path(self.tmp.name)
        (self.agent / "data").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def stamp(self, **meta):
        (self.agent / "data" / "_meta.json").write_text(json.dumps(meta))

    def test_the_exact_case_that_passed_and_should_not_have(self):
        morning = datetime(2026, 9, 17, 10, 12, tzinfo=timezone.utc)   # open
        self.stamp(report_name="2026-09-17-close.md", slot="close")
        name, slot_ = et_time.expected_report(self.agent, "belfort", now=morning)
        self.assertEqual(name, "2026-09-17-open.md")
        self.assertEqual(slot_, "open")

    def test_a_stamp_that_agrees_with_the_clock_changes_nothing(self):
        afternoon = datetime(2026, 9, 17, 15, 55, tzinfo=timezone.utc)  # close
        self.stamp(report_name="2026-09-17-close.md", slot="close")
        name, _ = et_time.expected_report(self.agent, "belfort", now=afternoon)
        self.assertEqual(name, "2026-09-17-close.md")

    def test_no_stamp_at_all_is_fine(self):
        morning = datetime(2026, 9, 17, 10, 12, tzinfo=timezone.utc)
        name, _ = et_time.expected_report(self.agent, "belfort", now=morning)
        self.assertEqual(name, "2026-09-17-open.md")

    def test_the_cycle_start_epoch_beats_the_wall_clock(self):
        # A cycle that starts in one slot must be graded on that slot even if
        # it is still running when the boundary passes.
        started = datetime(2026, 9, 17, 10, 12, tzinfo=timezone.utc).timestamp()
        name, _ = et_time.expected_report(self.agent, "belfort", started=started)
        self.assertEqual(name, "2026-09-17-open.md")

    def test_ace_is_covered_too(self):
        night = datetime(2026, 9, 16, 23, 30, tzinfo=timezone.utc)
        self.stamp(report_name="2026-09-16-afternoon.md", slot="afternoon")
        name, slot_ = et_time.expected_report(self.agent, "ace", now=night)
        self.assertEqual(name, "2026-09-16-night.md")
        self.assertEqual(slot_, "night")


class InstructionsMatchTheVerifier(unittest.TestCase):
    """Belfort wrote 43 words and was failed for being under 60. Its
    instructions said "two honest paragraphs" and named no number - the
    threshold existed only inside the verifier. The agent was graded against a
    rule it had never been given, which it could not have satisfied except by
    accident.

    A header is prose and cannot import a constant, so this is the only thing
    that keeps the two in step. If someone changes MIN_REPORT_WORDS and not the
    headers, or a header and not the constant, the build fails here rather than
    an agent failing at 4am.
    """

    ENFORCED_BY_A_VERIFIER = ("ace", "belfort")

    def header(self, agent):
        return (ROOT / "agents" / agent / f"_{agent}-agents-header.md").read_text()

    def test_every_header_states_the_real_minimum(self):
        for agent in self.ENFORCED_BY_A_VERIFIER:
            with self.subTest(agent=agent):
                m = re.search(r"[Aa]t least (\d+) words", self.header(agent))
                self.assertIsNotNone(
                    m, f"{agent} is failed for short reports but is never told the minimum")
                self.assertEqual(int(m.group(1)), et_time.MIN_REPORT_WORDS,
                                 f"{agent} is told a different number from the one "
                                 f"the verifier enforces")

    def test_no_verifier_hardcodes_the_number_any_more(self):
        for f in ("ace-verify.py", "belfort-verify.py"):
            with self.subTest(script=f):
                src = (SCRIPTS / f).read_text()
                self.assertNotRegex(src, r"words <\s*\d",
                                    f"{f} has its own copy of the threshold again")
                self.assertIn("et_time.MIN_REPORT_WORDS", src)


class WhatCountsAsJudged(unittest.TestCase):
    """ace-verify counted every row in the ledger as judged; signoff counted
    only rows whose status is "passed" or "bet". They agreed solely because
    ace-judge.py always sets one - so a hand-written row, or any future path
    that forgets, would have told the agent its ledger was complete and empty
    at the same time. That contradiction has cost this project more than any
    other single thing.
    """

    def setUp(self):
        self.judge = load("ace_judge", "ace-judge.py")

    def rows(self, *statuses):
        return {"candidates": [
            ({"selection": f"S{i}", "match": "A @ B", "status": s} if s else
             {"selection": f"S{i}", "match": "A @ B"})
            for i, s in enumerate(statuses)]}

    def test_only_rows_with_a_status_count(self):
        doc = self.rows("passed", "bet", None)
        self.assertEqual(len(self.judge.rows_of(doc)), 3)
        self.assertEqual(len(self.judge.judged_rows(doc)), 2)

    def test_an_unknown_status_does_not_count(self):
        self.assertEqual(len(self.judge.judged_rows(self.rows("maybe", "skipped"))), 0)

    def test_a_bare_array_is_still_readable(self):
        # A bare list was what crashed the verifier once, by calling .get() on it.
        self.assertEqual(len(self.judge.rows_of([{"status": "passed"}])), 1)
        self.assertEqual(len(self.judge.judged_rows([{"status": "passed"}])), 1)

    def test_neither_reader_keeps_its_own_copy(self):
        for f in ("ace-verify.py", "signoff.py"):
            with self.subTest(script=f):
                src = (SCRIPTS / f).read_text()
                self.assertIn("ace_judge.judged_rows", src)
                self.assertNotRegex(
                    src, r'status\"?\)\s*in\s*\(\"passed\"',
                    f"{f} has grown its own copy of the judged predicate again")


class BackgroundKnockout(unittest.TestCase):
    """A sticker is die-cut so an opaque square is fine. A garment prints the
    background as a visible rectangle - a white box on a black tee. Nothing
    Emily generates has an alpha channel, so this is the one step between her
    art and anything wearable.
    """

    def setUp(self):
        self.ko = load("knockout", "knockout.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def solid(self, w, h, fn):
        px = bytearray()
        for y in range(h):
            for x in range(w):
                px += bytes(fn(x, y)) + b"\xff"
        return px

    def png(self, name, w, h, fn):
        p = self.d / name
        self.ko.encode(p, w, h, self.solid(w, h, fn))
        return p

    # --- the reason it floods rather than thresholds ------------------------

    def test_background_colour_inside_the_art_survives(self):
        """THE design decision. A white highlight inside a dark shape is the
        same colour as the background; a global threshold erases it and leaves
        a hole you only see on the printed garment."""
        W = H = 64

        def art(x, y):
            dx, dy = x - W / 2, y - H / 2
            in_disc = dx * dx + dy * dy < 22 * 22
            in_eye = (x - 26) ** 2 + (y - 26) ** 2 < 4 * 4
            return (255, 255, 255) if (not in_disc or in_eye) else (20, 20, 20)

        p = self.png("eye.png", W, H, art)
        w, h, px = self.ko.decode(p)
        self.ko.knockout(w, h, px)
        a = lambda x, y: px[(y * w + x) * 4 + 3]
        self.assertEqual(a(0, 0), 0, "the corner is background and must go")
        self.assertEqual(a(32, 32), 255, "the disc is art")
        self.assertEqual(a(26, 26), 255, "the white eye is enclosed - it must stay")

    def test_it_writes_a_real_alpha_channel(self):
        p = self.png("x.png", 32, 32, lambda x, y: (255, 255, 255) if x < 24 else (10, 10, 10))
        w, h, px = self.ko.decode(p)
        self.ko.knockout(w, h, px)
        out = self.d / "out.png"
        self.ko.encode(out, w, h, px)
        colour_type = out.read_bytes()[25]
        self.assertEqual(colour_type, 6, "PNG colour type must be 6 (RGBA), not 2 (RGB)")

    # --- guards: it refuses rather than writing a plausible wrong file ------

    def test_an_image_that_is_all_background_is_refused(self):
        p = self.png("blank.png", 32, 32, lambda x, y: (255, 255, 255))
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(p), str(self.d / "no.png")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("the artwork itself matched the background", r.stderr)
        self.assertFalse((self.d / "no.png").exists(), "nothing should be written")

    def test_art_with_no_flat_background_is_refused(self):
        # A gradient everywhere: no border colour to fill from.
        p = self.png("grad.png", 48, 48, lambda x, y: (x * 5 % 256, y * 5 % 256, 128))
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(p), str(self.d / "no.png")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no flat background", r.stderr)
        self.assertFalse((self.d / "no.png").exists())

    def test_art_that_runs_off_the_edge_is_refused(self):
        """The best case the border guard catches. When the design bleeds to
        one side, the most common border colour is the ARTWORK - so a naive
        knockout removes the design and keeps the background. Inverted, and
        it would look fine in a thumbnail."""
        p = self.png("bleed.png", 96, 96,
                     lambda x, y: (20, 90, 160) if x < 70 else (250, 250, 250))
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(p), str(self.d / "no.png")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no flat background", r.stderr)
        self.assertFalse((self.d / "no.png").exists())

    def test_a_jpeg_is_named_as_such(self):
        p = self.d / "photo.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(p), str(self.d / "no.png")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("JPEG", r.stderr)

    # --- the decoder paths the docstring claims ----------------------------

    def test_round_trip_is_lossless(self):
        px = self.solid(16, 16, lambda x, y: (x * 16 % 256, y * 16 % 256, 77))
        p = self.d / "rt.png"
        self.ko.encode(p, 16, 16, px)
        w, h, back = self.ko.decode(p)
        self.assertEqual((w, h), (16, 16))
        self.assertEqual(bytes(back), bytes(px))

    def test_it_reads_emilys_own_placeholder(self):
        # colour type 2, written by emily-assets.py - the real input.
        out = self.d / "ph.png"
        r = subprocess.run([sys.executable, str(SCRIPTS / "emily-assets.py"),
                            "--prompt", "a fox", "--out", str(out),
                            "--size", "64", "--placeholder-only"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_bytes()[25], 2, "placeholder should be RGB, no alpha")
        w, h, px = self.ko.decode(out)
        self.assertEqual((w, h), (64, 64))

    def test_greyscale_and_palette_decode(self):
        def build(colour_type, body_rows, palette=None):
            def chunk(tag, b):
                import zlib as z
                return (struct.pack(">I", len(b)) + tag + b
                        + struct.pack(">I", z.crc32(tag + b) & 0xFFFFFFFF))
            import zlib as z
            raw = b"".join(b"\x00" + r for r in body_rows)
            png = b"\x89PNG\r\n\x1a\n"
            png += chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 2, 8, colour_type, 0, 0, 0))
            if palette:
                png += chunk(b"PLTE", palette)
            png += chunk(b"IDAT", z.compress(raw))
            png += chunk(b"IEND", b"")
            return png

        grey = self.d / "grey.png"
        grey.write_bytes(build(0, [bytes([0, 64, 128, 255])] * 2))
        w, h, px = self.ko.decode(grey)
        self.assertEqual((w, h), (4, 2))
        self.assertEqual((px[0], px[1], px[2], px[3]), (0, 0, 0, 255))

        pal = self.d / "pal.png"
        pal.write_bytes(build(3, [bytes([0, 1, 0, 1])] * 2,
                              palette=bytes([255, 0, 0, 0, 0, 255])))
        w, h, px = self.ko.decode(pal)
        self.assertEqual((px[0], px[1], px[2]), (255, 0, 0))
        self.assertEqual((px[4], px[5], px[6]), (0, 0, 255))


class PricingApparelBySize(unittest.TestCase):
    """Stickers have five variants and you type five prices in order. A tee is
    sizes x colours - a hundred or more - and typing a hundred numbers in the
    right order is not a workflow, it is a way to get one size wrong and not
    notice until it sells.
    """

    def setUp(self):
        self.pf = load("emily_printify", "emily-printify.py")

    def test_size_is_found_in_either_order(self):
        for title, want in (("Black / S", "S"), ("S / Black", "S"),
                            ("Heather Grey / 2XL", "2XL"), ("2XL / Heather Grey", "2XL"),
                            ("White / XXL", "2XL"),          # alias
                            ("navy / m", "M")):              # case
            with self.subTest(title=title):
                self.assertEqual(self.pf.size_of(title), want)

    def test_a_sticker_size_is_not_a_garment_size(self):
        # Sticker titles are inches. Reading one as a size would route stickers
        # into the by-size path and silently change how they are priced.
        for title in ('2" x 2"', '5.5" x 5.5"', 'Matte', ''):
            with self.subTest(title=title):
                self.assertIsNone(self.pf.size_of(title))

    def test_L_inside_a_colour_name_is_not_a_size(self):
        # "Slate" contains no standalone size token; the parser splits on "/"
        # and matches whole tokens, so a colour cannot be read as a size.
        self.assertIsNone(self.pf.size_of("Slate"))
        self.assertEqual(self.pf.size_of("Slate / L"), "L")

    def test_every_title_is_kept_not_the_first_twelve(self):
        # cmd_pick used to truncate variant_titles to 12. Invisible with five
        # sticker variants; on a tee it dropped 88 of them, and --by-size has
        # nothing to read a size from in a title that was never saved.
        src = (SCRIPTS / "emily-printify.py").read_text()
        self.assertNotIn('for v in chosen][:12]', src)
        self.assertIn('"variant_titles": [v.get("title") for v in chosen],', src)


class BlueprintSearchIgnoresTrademarks(unittest.TestCase):
    """Searching the catalogue for "heavy blend hooded" returned only the
    Youth version. Printify writes the one we wanted as "Unisex Heavy Blend(TM)
    Hooded Sweatshirt" - the symbol sits between two of the words typed, so a
    plain substring match fails. It read as the product being absent from the
    catalogue rather than as a search that could not see it.
    """

    def setUp(self):
        self.pf = load("emily_printify", "emily-printify.py")

    def test_the_search_that_failed(self):
        self.assertTrue(self.pf.matches("Unisex Heavy Blend™ Hooded Sweatshirt",
                                        "heavy blend hooded"))

    def test_every_symbol_printify_uses(self):
        for sym in ("™", "®", "©", "℠"):
            with self.subTest(symbol=sym):
                self.assertTrue(self.pf.matches(f"Unisex EcoSmart{sym} Crewneck", "ecosmart crewneck"))

    def test_it_does_not_match_everything_now(self):
        self.assertFalse(self.pf.matches("Unisex Jersey Short Sleeve Tee", "heavy blend hooded"))
        self.assertFalse(self.pf.matches("Youth Heavy Blend Hooded Sweatshirt", "ecosmart"))

    def test_spacing_and_case_do_not_matter(self):
        self.assertTrue(self.pf.matches("Unisex  Heavy   Blend™ Hooded Sweatshirt",
                                        "  HEAVY blend   hooded "))


class PickingColoursNotTheFirstTwelve(unittest.TestCase):
    """The Gildan 18500 has 274 variants. `pick` defaulted to --limit 12 and
    silently kept the first twelve, which for this product is six sizes of Ash
    and a bit of Dark Heather - a hoodie listing with no Black in it. A sticker
    has five variants and never reached that line; a garment reaches it every
    time.

    Titles are real: "Ash / S", "Dark Heather / 5XL", from blueprint 77
    provider 99.
    """

    def setUp(self):
        self.pf = load("emily_printify", "emily-printify.py")

    def test_colour_and_size_split_from_the_real_titles(self):
        for title, colour, size in (
                ("Ash / S", "Ash", "S"),
                ("Dark Heather / 5XL", "Dark Heather", "5XL"),
                ("Maroon / 2XL", "Maroon", "2XL"),
                ("2XL / Black", "Black", "2XL")):      # order does not matter
            with self.subTest(title=title):
                self.assertEqual(self.pf.colour_of(title), colour)
                self.assertEqual(self.pf.size_of(title), size)

    def test_every_size_this_garment_offers_is_known(self):
        # S through 5XL. A size the parser cannot read becomes a variant with
        # no price, which --by-size then refuses - correct, but only if the
        # sizes are known in the first place.
        for s in ("S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"):
            with self.subTest(size=s):
                self.assertEqual(self.pf.size_of(f"Navy / {s}"), s)
                self.assertIn(s, self.pf.SIZES)

    def test_a_colourless_title_has_no_colour(self):
        self.assertIsNone(self.pf.colour_of("S"))
        self.assertIsNone(self.pf.colour_of(""))


class ClosingTheCycle(unittest.TestCase):
    """Ace's 19:01 scheduled run judged 14 of 14 rows, joined them all to the
    board, wrote a 90-word report and recorded its memory. signoff printed
    "All deliverables present". The verifier then failed it:

        cycle_count did not advance (4 -> 4)

    signoff checked the counter EXISTS; the verifier checked it ADVANCED. The
    wrapper computes the wake value and hands it only to the verifier, so
    signoff had no way to apply the same rule. Fourth instance of one fact with
    two opinions.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = self.root / "agents" / "ace"
        for d in ("data", "state", "reports"):
            (self.agent / d).mkdir(parents=True)
        self.started = int(time.time()) - 120
        want, _ = et_time.expected_report(self.agent, "ace", started=self.started)
        rows = [{"selection": f"P{i}", "match": "A @ B", "status": "passed",
                 "why_not": ["no edge"]} for i in range(14)]
        (self.agent / "data" / "candidates.json").write_text(json.dumps({"candidates": rows}))
        (self.agent / "state" / "ledger.json").write_text(
            json.dumps({"candidates": rows, "verdict": "Quiet slate."}))
        (self.agent / "reports" / want).write_text("word " * 90)
        (self.agent / "MEMORY.md").write_text("- a line\n")
        (self.agent / "state" / ".cycle-started").write_text(str(self.started))
        (self.agent / "state" / ".cycle-before").write_text("4")
        self.bank(4)

    def tearDown(self):
        self.tmp.cleanup()

    def bank(self, count):
        (self.agent / "state" / "bankroll.json").write_text(json.dumps(
            {"starting_bankroll": 10000.0, "bankroll": 10000.0, "open_bets": [],
             "settled_bets": [], "cycle_count": count}))

    def signoff(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "signoff.py"), "ace"],
                              capture_output=True, text=True, env=env)

    def test_an_unadvanced_counter_is_not_a_finished_cycle(self):
        r = self.signoff()
        self.assertEqual(r.returncode, 1, "signoff must not pass a cycle that never closed")
        self.assertIn("NOT CLOSED", r.stdout)
        self.assertNotIn("All deliverables present", r.stdout)

    def test_it_names_the_command_that_fixes_it(self):
        # The agent reads this mid-cycle. A complaint it cannot act on costs a
        # whole run; naming the command is the difference.
        self.assertIn("ace-judge.py mark", self.signoff().stdout)

    def test_a_closed_cycle_passes(self):
        self.bank(5)
        r = self.signoff()
        self.assertEqual(r.returncode, 0)
        self.assertIn("All deliverables present", r.stdout)

    def test_a_counter_going_backwards_is_also_refused(self):
        self.bank(3)
        self.assertEqual(self.signoff().returncode, 1)

    def test_belfort_is_told_its_own_command(self):
        sign = load("signoff", "signoff.py")
        self.assertIn("belfort-trade.py mark", sign.MARK_COMMAND["belfort"])
        self.assertIn("ace-judge.py mark", sign.MARK_COMMAND["ace"])

    def test_both_wrappers_publish_the_wake_count(self):
        for f in ("ace-cycle.sh", "belfort-cycle.sh"):
            with self.subTest(wrapper=f):
                src = (SCRIPTS / f).read_text()
                self.assertIn(".cycle-before", src,
                              f"{f} must publish CYCLES_BEFORE or signoff cannot check it")


class TheVerdictLine(unittest.TestCase):
    """The village shows one sentence per cycle: the ledger's verdict line.
    ace-verify noted its absence on every passing run; signoff checked nothing,
    so the agent read "All deliverables present" and stopped. AGENTS.md asks
    for it. Nothing enforced it, and the village showed a blank.

    Deliberate asymmetry, unlike the four bugs before it: signoff REQUIRES the
    line, the verifier only notes it. signoff guides a cycle that is still
    running and can still act; the verifier judges one that is over, and a
    missing sentence is not a cycle that failed to happen. Signoff being the
    stricter of the two means the verifier should never fire.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = self.root / "agents" / "ace"
        for d in ("data", "state", "reports"):
            (self.agent / d).mkdir(parents=True)
        started = int(time.time()) - 120
        want, _ = et_time.expected_report(self.agent, "ace", started=started)
        self.rows = [{"selection": f"P{i}", "match": "A @ B", "status": "passed",
                      "why_not": ["no edge"]} for i in range(6)]
        (self.agent / "data" / "candidates.json").write_text(
            json.dumps({"candidates": self.rows}))
        (self.agent / "reports" / want).write_text("word " * 248)
        (self.agent / "MEMORY.md").write_text("- a line\n")
        (self.agent / "state" / ".cycle-started").write_text(str(started))
        (self.agent / "state" / ".cycle-before").write_text("4")
        (self.agent / "state" / "bankroll.json").write_text(json.dumps(
            {"starting_bankroll": 10000.0, "bankroll": 10000.0, "open_bets": [],
             "settled_bets": [], "cycle_count": 5}))

    def tearDown(self):
        self.tmp.cleanup()

    def ledger(self, **extra):
        (self.agent / "state" / "ledger.json").write_text(
            json.dumps(dict({"candidates": self.rows}, **extra)))

    def signoff(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "signoff.py"), "ace"],
                              capture_output=True, text=True, env=env)

    def test_a_missing_verdict_is_not_a_finished_cycle(self):
        self.ledger()
        r = self.signoff()
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO VERDICT", r.stdout)
        self.assertNotIn("All deliverables present", r.stdout)

    def test_it_names_the_command(self):
        self.ledger()
        self.assertIn("ace-judge.py verdict", self.signoff().stdout)

    def test_an_empty_verdict_does_not_count(self):
        self.ledger(verdict="   ")
        self.assertEqual(self.signoff().returncode, 1)

    def test_a_real_verdict_passes(self):
        self.ledger(verdict="Quiet slate - nothing cleared the bar.")
        r = self.signoff()
        self.assertEqual(r.returncode, 0)
        self.assertIn("All deliverables present", r.stdout)
