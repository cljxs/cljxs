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

import copy
import contextlib
import io
import importlib.util
import json
import os
import random
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import types
import time
import urllib.error
import urllib.request
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
    'never ran', not as a crash.

    This test used to carry the fixture's timestamp written out beside it -
    "2026-09-16 18:55:37" as a string in the assertion. capture-fixtures.py
    exists to refresh that fixture from the droplet, and the moment someone
    did, the test failed with a diff of two timestamps and no indication that
    the FIXTURE had moved rather than the parser. One fact in two places, and
    the second place was the one nobody would think to update.

    What it checks now is the round trip: whatever instant the captured line
    names, parsing it and printing it back must give that line again. That
    holds for any capture, including tomorrow's.
    """

    def stamp_line(self):
        return [l for l in (FIXTURES / "systemd-show.txt").read_text().splitlines()
                if l.startswith("ExecMainStartTimestamp=")][0].split("=", 1)[1]

    def test_the_real_format(self):
        line = self.stamp_line()
        ts = healthcheck.parse_stamp(line)
        self.assertIsNotNone(ts, f"the captured format stopped parsing: {line!r}")
        _day, date, clock, zone = line.split()
        printed = (datetime.fromtimestamp(ts, timezone.utc)
                   if zone.upper() in ("UTC", "GMT", "Z")
                   else datetime.fromtimestamp(ts))
        self.assertEqual(printed.strftime("%Y-%m-%d %H:%M:%S"), f"{date} {clock}")

    def test_refreshing_the_fixture_cannot_break_this(self):
        # The bug itself. A capture from any other moment must parse the same
        # way - if this test can only pass against one particular timestamp,
        # it is testing the fixture rather than the parser.
        for line in ("Thu 2026-09-17 03:30:51 UTC", "Mon 2019-01-07 00:00:00 UTC",
                     "Sat 2030-12-31 23:59:59 UTC"):
            with self.subTest(line=line):
                ts = healthcheck.parse_stamp(line)
                self.assertEqual(
                    datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    " ".join(line.split()[1:3]))

    def test_a_stamp_with_no_zone_at_all_is_still_read_as_utc(self):
        # Not local. systemd always prints a zone, so a line without one is
        # something else's output - and guessing "local" on a machine that is
        # not UTC would move a timestamp nobody asked to be moved. UTC is what
        # this always assumed; the suffix handling above is an addition to it,
        # not a replacement.
        was = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            ts = healthcheck.parse_stamp("Wed 2026-09-16 18:55:37")
            self.assertEqual(datetime.fromtimestamp(ts, timezone.utc)
                             .strftime("%H:%M:%S"), "18:55:37")
        finally:
            if was is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = was
            time.tzset()

    def test_the_zone_on_the_end_is_not_decoration(self):
        # systemd prints in the machine's own timezone. The suffix was being
        # dropped and everything called UTC, which on an Eastern droplet
        # reports every wake four hours before it happened - silently, and in
        # the one number health-check exists to be trusted on.
        was = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            utc = healthcheck.parse_stamp("Wed 2026-09-16 18:55:37 UTC")
            edt = healthcheck.parse_stamp("Wed 2026-09-16 18:55:37 EDT")
            self.assertEqual(datetime.fromtimestamp(utc, timezone.utc)
                             .strftime("%H:%M:%S"), "18:55:37")
            self.assertEqual(datetime.fromtimestamp(edt, timezone.utc)
                             .strftime("%H:%M:%S"), "22:55:37")
            self.assertEqual(edt - utc, 4 * 3600)
        finally:
            if was is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = was
            time.tzset()

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


class ApparelIsCutOutBeforeItIsUploaded(unittest.TestCase):
    """knockout.py existed as a standalone script nothing called, and `draft`
    uploaded design.png directly. So a hoodie drafted today would have carried
    Emily's opaque artwork - which prints the background as a visible rectangle
    on the garment, a white box on a black hoodie.

    That is the worst failure mode available here: it looks right in the
    listing and arrives wrong on the doorstep. Everything else this week failed
    loudly.
    """

    def setUp(self):
        self.pf = load("emily_printify", "emily-printify.py")

    def test_the_hoodie_entry_saved_before_the_flag_existed(self):
        # No "cutout" key - it was picked yesterday, and it still gets one.
        self.assertTrue(self.pf.needs_cutout(
            {"variant_titles": ["Black / S", "Navy / 2XL", "Maroon / 5XL"]}))

    def test_stickers_are_cut_out_too(self):
        # This test used to assert the opposite, with the comment "die-cut
        # already; an opaque square is correct for them". That was the bug,
        # written down as a guarantee - and it held the wrong answer in place
        # while a real sticker came back as a disc with the whole square
        # printed inside it. A die cut follows the artwork's transparency;
        # given an opaque square it has nothing to follow.
        self.assertTrue(self.pf.needs_cutout(
            {"variant_titles": ['2" x 2"', '4" x 4"', '5.5" x 5.5"']}))

    def test_an_explicit_flag_beats_the_default(self):
        self.assertFalse(self.pf.needs_cutout({"cutout": False,
                                               "variant_titles": ["Black / S"]}))
        self.assertTrue(self.pf.needs_cutout({"cutout": True,
                                              "variant_titles": ['2" x 2"']}))

    def test_an_empty_entry_does_not_crash_and_gets_the_cutout(self):
        # Silence used to mean "leave it opaque". It means "cut it out" now,
        # which is the safe way round: a product type added next year is
        # opaque-by-accident under the old default.
        for entry in ({}, None, {"variant_titles": []}):
            with self.subTest(entry=entry):
                self.assertTrue(self.pf.needs_cutout(entry))

    def test_draft_refuses_rather_than_uploading_the_opaque_file(self):
        src = (SCRIPTS / "emily-printify.py").read_text()
        self.assertIn("upload_from = cut", src)
        self.assertIn("not drafting:", src,
                      "a knockout that refuses must stop the draft, not fall through")
        # The upload must read the cutout, never the original, once cut.
        self.assertIn("upload_from.read_bytes()", src)
        self.assertNotIn("contents\": base64.b64encode(design.read_bytes())", src)


class ScoutCanBeFocusedForOneRun(unittest.TestCase):
    """Steering one run by editing AGENTS.md means remembering to edit it back,
    and a forgotten edit is an agent running last week's rules - what
    preflight.py reports as stale/broken. The focus is an argument instead: it
    is written to no file and nothing remembers it.

    Scout was also the last agent whose whole cycle lived inline in its unit,
    with nested quotes, escaped quotes and systemd %% escaping stacked. The
    comment atop ace-cycle.sh records why the others were extracted: it failed
    silently.
    """

    WRAPPER = SCRIPTS / "scout-cycle.sh"

    def test_the_wrapper_exists_and_parses(self):
        self.assertTrue(self.WRAPPER.is_file())
        r = subprocess.run(["bash", "-n", str(self.WRAPPER)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_unit_calls_the_wrapper_not_a_wall_of_inline_bash(self):
        unit = (ROOT / "deploy" / "scout-cycle.service").read_text()
        exec_line = [l for l in unit.splitlines() if l.startswith("ExecStart=")][0]
        self.assertIn("scout-cycle.sh", exec_line)
        self.assertNotIn("openclaw agent", exec_line,
                         "the cycle is inline in the unit again")

    def test_the_checks_survived_the_move(self):
        # The unit did three things beyond waking the agent. Losing any of them
        # in the move would be silent: a run that recorded nothing would pass.
        src = self.WRAPPER.read_text()
        self.assertIn("SCOUT RECORDED NOTHING", src)
        self.assertIn('printf \'%s\\n\' "$LINE" >> "$MEM"', src)
        # The pass/fell-over distinction moved into scout-ideas.py when the
        # merge took over, so assert the wrapper still calls it rather than
        # pinning a sentence that legitimately moved.
        self.assertIn("scout-ideas.py", src)
        merge = (SCRIPTS / "scout-ideas.py").read_text()
        self.assertIn("unchanged", merge,
                      "a run that proposes nothing must still say the log is intact")

    def test_the_default_run_is_unfocused(self):
        src = self.WRAPPER.read_text()
        self.assertIn('MESSAGE="scheduled idea run"', src)
        self.assertIn('if [ "$#" -gt 0 ]; then', src)

    def test_a_focus_reaches_the_message_verbatim(self):
        r = subprocess.run(
            ["bash", "-c",
             'set -- "the Gildan 18500 hoodie"; '
             'MESSAGE="scheduled idea run"; '
             'if [ "$#" -gt 0 ]; then MESSAGE="Focused idea run. Every idea you '
             'propose must be for: $*"; fi; echo "$MESSAGE"'],
            capture_output=True, text=True)
        self.assertIn("the Gildan 18500 hoodie", r.stdout)

    def test_the_focus_is_not_written_anywhere(self):
        # If it touched a file, it would outlive the run it was meant for.
        src = self.WRAPPER.read_text()
        for line in src.splitlines():
            if ">" in line and "MESSAGE" in line:
                self.fail(f"the focus is being written to a file: {line.strip()}")


class ScoutCannotDestroyTheIdeaLog(unittest.TestCase):
    """On 2026-09-18 Scout wrote "Cleared existing ideas to reflect no new
    proposals" and emptied state/ideas.json. Line 18 of its own instructions
    said, in bold: "every existing entry kept, yours appended". Nothing was
    lost because nothing was pending - luck, not a safeguard.

    Second time: its unit records that it destroyed MEMORY.md twice the same
    way. The fix there was to take the job off it, so Scout owns a scratch file
    and code does the append. Same fix, same reason.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents" / "scout" / "state"
        self.state.mkdir(parents=True)
        self.ideas = self.state / "ideas.json"
        self.proposals = self.state / "proposals.json"
        # merge refuses a row with no measurement behind it now, so these
        # need a scan and rows that name it. The behaviours they guard - the
        # append, the id assignment, the rerun, the inch repair - are
        # unchanged; only the shape of a valid row is.
        scans = self.state / "scans"
        scans.mkdir(parents=True, exist_ok=True)
        (scans / "s.json").write_text(json.dumps({
            "seed": "hoodie", "scanned_at": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [{"phrase": "trail map hoodie", "supply": 1000,
                      "heat": 0.05, "pull": 0.04, "price": 30.0, "match": 1.0,
                      "returned": 25, "heat_n": 25, "pull_n": 25,
                      "score": 0.017}], "excluded": []}))

    def evidenced(self, rows):
        """Rows as `propose` would have written them."""
        out = []
        for r in rows:
            r = dict(r)
            r.setdefault("evidence", {"phrase": "trail map hoodie",
                                      "supply": 1000, "favs_per_day": 0.05})
            out.append(r)
        return out

    def tearDown(self):
        self.tmp.cleanup()

    def merge(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), "merge"],
                              capture_output=True, text=True, env=env)

    def log(self):
        return json.loads(self.ideas.read_text())["ideas"]

    def test_existing_ideas_survive_a_run_that_proposes_nothing(self):
        self.ideas.write_text(json.dumps({"ideas": [
            {"id": 1, "title": "Autumn cocoa sticker", "status": "pending"},
            {"id": 2, "title": "Rainy window print", "status": "approved"}]}))
        self.proposals.write_text("[]")
        self.merge()
        self.assertEqual(len(self.log()), 2, "a quiet run must not empty the log")
        self.assertEqual(self.log()[1]["status"], "approved", "verdicts survive too")

    def test_new_ideas_are_appended_not_substituted(self):
        self.ideas.write_text(json.dumps({"ideas": [{"id": 1, "title": "Old one"}]}))
        self.proposals.write_text(json.dumps(self.evidenced(
            [{"title": "Trail map hoodie", "product": "hoodie"}])))
        self.merge()
        titles = [i["title"] for i in self.log()]
        self.assertEqual(titles, ["Old one", "Trail map hoodie"])

    def test_ids_are_assigned_here_not_by_the_agent(self):
        self.ideas.write_text(json.dumps({"ideas": [{"id": 7, "title": "Old"}]}))
        self.proposals.write_text(json.dumps(
            self.evidenced([{"title": "A"}, {"title": "B"}])))
        self.merge()
        self.assertEqual([i["id"] for i in self.log()], [7, 8, 9])
        self.assertTrue(all(i.get("status") == "pending" for i in self.log()[1:]))

    def test_a_repeated_title_is_not_filed_twice(self):
        self.ideas.write_text(json.dumps({"ideas": [{"id": 1, "title": "Trail map hoodie"}]}))
        self.proposals.write_text(json.dumps(
            self.evidenced([{"title": "  trail MAP hoodie "}])))
        self.merge()
        self.assertEqual(len(self.log()), 1)

    def test_rerunning_the_merge_adds_nothing(self):
        self.ideas.write_text(json.dumps({"ideas": []}))
        self.proposals.write_text(json.dumps(self.evidenced([{"title": "One"}])))
        self.merge()
        self.merge()
        self.assertEqual(len(self.log()), 1)

    def test_an_unparseable_log_is_refused_not_overwritten(self):
        self.ideas.write_text("{ this is not json")
        self.proposals.write_text(json.dumps([{"title": "New"}]))
        r = self.merge()
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not parse", r.stderr)
        self.assertEqual(self.ideas.read_text(), "{ this is not json",
                         "a merge into an unreadable file must change nothing")

    def test_scout_is_told_the_log_is_not_its_to_edit(self):
        header = (ROOT / "agents" / "scout" / "_scout-agents-header.md").read_text()
        self.assertIn("proposals.json", header)
        # Wording changed when proposals.json joined it; the rule did not.
        self.assertIn("Never write `state/ideas.json`", header)
        self.assertIn("proposals.json", header)

    def test_the_wrapper_merges(self):
        src = (SCRIPTS / "scout-cycle.sh").read_text()
        self.assertIn("scout-ideas.py", src)


class ProposalsThatCannotBeReadAreNotAQuietPass(unittest.TestCase):
    """Scout proposed three hoodie ideas on gpt-5-mini - the first good output
    it has produced - and the merge printed "no new proposals". The loader
    swallowed every exception and returned an empty list, so three outcomes had
    one message:

        the file is absent          a run that proposed nothing
        the file does not parse     ideas written and unreadable
        the file is an empty list   a run that proposed nothing

    Written two hours earlier, in a script whose whole purpose was to stop
    ideas being lost, inside an except clause added while fixing this exact
    class of bug elsewhere.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents" / "scout" / "state"
        self.state.mkdir(parents=True)
        (self.state / "ideas.json").write_text('{"ideas":[]}')
        self.proposals = self.state / "proposals.json"

    def tearDown(self):
        self.tmp.cleanup()

    def merge(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), "merge"],
                              capture_output=True, text=True, env=env)

    def test_no_file_is_a_pass(self):
        r = self.merge()
        self.assertEqual(r.returncode, 0)
        self.assertIn("no new proposals", r.stdout)

    def test_an_empty_list_is_a_pass(self):
        self.proposals.write_text("[]")
        self.assertEqual(self.merge().returncode, 0)

    def test_a_file_that_does_not_parse_is_an_error(self):
        self.proposals.write_text('[{"title": "Minimalist Mountain Badge"')
        r = self.merge()
        self.assertEqual(r.returncode, 1, "a broken file must not read as a quiet pass")
        self.assertIn("does not parse", r.stderr)
        self.assertNotIn("no new proposals", r.stdout)

    def test_the_error_shows_the_ideas_so_they_can_be_recovered(self):
        # They are not in the log yet. If the message does not carry them, the
        # only copy is in a file the operator has to know to go and read.
        self.proposals.write_text('[{"title": "Pocket Folklore Deer Silhouette"')
        self.assertIn("Pocket Folklore Deer Silhouette", self.merge().stderr)

    def test_entries_without_titles_are_an_error_not_a_silent_drop(self):
        self.proposals.write_text('[{"name": "x"}, {"name": "y"}]')
        r = self.merge()
        self.assertEqual(r.returncode, 1)
        self.assertIn("none with a title", r.stderr)

    def test_a_broken_file_leaves_the_log_alone(self):
        (self.state / "ideas.json").write_text('{"ideas":[{"id":1,"title":"Keep me"}]}')
        self.proposals.write_text("{ not json")
        self.merge()
        log = json.loads((self.state / "ideas.json").read_text())["ideas"]
        self.assertEqual([i["title"] for i in log], ["Keep me"])


class ScoutDoesNotHandWriteJson(unittest.TestCase):
    """Scout's first good run proposed three hoodie ideas and none were filed.
    Not a reasoning failure - a quoting one:

        "brief": "... sized for a 3.5" chest print."

    The inch mark closed the JSON string. So `propose` takes the fields as
    arguments and does the quoting, the same reason ace-judge.py exists: a
    selection that is never typed cannot be mistyped.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents" / "scout" / "state"
        self.state.mkdir(parents=True)
        (self.state / "ideas.json").write_text('{"ideas":[]}')
        self.proposals = self.state / "proposals.json"
        # propose now refuses an idea with no measurement behind it, so these
        # need one. The bugs they guard - the inch mark, accumulation, the
        # clobber - are unchanged; only the door they come through is.
        scans = self.state / "scans"
        scans.mkdir(parents=True, exist_ok=True)
        (scans / "s.json").write_text(json.dumps({
            "seed": "hoodie", "scanned_at": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [{"phrase": "cosy hoodie", "supply": 1000, "heat": 0.05,
                      "pull": 0.04, "price": 30.0, "match": 1.0,
                      "returned": 25, "heat_n": 25, "pull_n": 25,
                      "score": 0.017}], "excluded": []}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, *args):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), *args],
                              capture_output=True, text=True, env=env)

    def test_an_inch_mark_survives_propose(self):
        r = self.run_it("propose", "--phrase", "cosy hoodie",
                        "--title", "Pocket Folklore Deer",
                        "--product", "hoodie",
                        "--brief", 'Bold flat shapes at 3.5" wide.')
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(self.proposals.read_text())["proposals"]
        self.assertIn('3.5"', rows[0]["brief"], "the words are the agent's")
        self.assertEqual(self.run_it("merge").returncode, 0)

    def test_proposals_accumulate_across_calls(self):
        for t in ("One", "Two", "Three"):
            self.run_it("propose", "--phrase", "cosy hoodie",
                        "--title", t, "--product", "hoodie")
        self.assertEqual(len(json.loads(self.proposals.read_text())["proposals"]), 3)

    def test_propose_will_not_clobber_a_file_it_cannot_read(self):
        self.proposals.write_text("{ not json")
        r = self.run_it("propose", "--phrase", "cosy hoodie",
                        "--title", "New", "--product", "hoodie")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.proposals.read_text(), "{ not json")

    def test_the_inch_repair_recovers_a_hand_written_file(self):
        # What actually happened, kept because a model writing measurements
        # into JSON will do it again even with propose available.
        # Raw text on purpose: the unescaped inch mark IS what is being
        # tested, so it cannot go through json.dumps. The evidence block
        # rides along as text for the same reason.
        self.proposals.write_text(
            '{"proposals":[{"title":"Minimalist Mountain Badge",'
            '"product":"hoodie","evidence":{"phrase":"cosy hoodie"},'
            '"brief":"sized for a 3.5" chest print."}]}')
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("inch mark had closed a string early", r.stderr)
        log = json.loads((self.state / "ideas.json").read_text())["ideas"]
        self.assertEqual(log[0]["title"], "Minimalist Mountain Badge")

    def test_the_repair_is_not_applied_when_it_does_not_help(self):
        self.proposals.write_text('{"proposals": [')
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 1, "a guess that does not parse is still a guess")
        self.assertIn("does not parse", r.stderr)

    def test_scout_is_told_to_use_propose(self):
        header = (ROOT / "agents" / "scout" / "_scout-agents-header.md").read_text()
        # Scout is told to write drafts.txt now - it cannot run a script,
        # and told to, it hand-wrote JSON twice instead.
        self.assertIn("state/drafts.txt", header)
        self.assertIn("No JSON anywhere, from you, ever", header)


class EtsyDisclosuresAreRequiredToDraft(unittest.TestCase):
    """Etsy requires two things stated in the listing: that a production
    partner makes the item, and that AI was used. Both are deterministic text
    that depended on someone remembering, and the consequence of forgetting is
    found by Etsy rather than by us - reportedly in the same category as
    selling a prohibited item. So draft writes them in; it used to refuse
    without them, which failed Emily for a rule her header never mentioned.

    The wording lives in agents/emily/state/disclosures.json, not in code. It
    is a legal statement about a real shop and no script should freeze one on
    the owner's behalf; the check follows whatever the file says.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agents" / "emily" / "state").mkdir(parents=True)
        self.d = load("disclosures", "disclosures.py")
        self.d.ROOT = self.root
        self.d.FILE = self.root / "agents" / "emily" / "state" / "disclosures.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_listing_with_neither_is_refused(self):
        ok, why = self.d.report("A cosy hoodie with a mountain badge.")
        self.assertFalse(ok)
        self.assertIn("production_partner", why)
        self.assertIn("ai", why)

    def test_a_listing_with_both_passes(self):
        rules = self.d.load()
        desc = ("A cosy hoodie. "
                + rules["production_partner"]["text"] + " " + rules["ai"]["text"])
        ok, why = self.d.report(desc)
        self.assertTrue(ok, why)

    def test_one_present_one_missing_names_only_the_missing_one(self):
        rules = self.d.load()
        ok, why = self.d.report("A cosy hoodie. " + rules["ai"]["text"])
        self.assertFalse(ok)
        self.assertIn("production_partner", why)
        self.assertNotIn("\n  ai\n", why)

    def test_rewording_the_file_rewords_the_check(self):
        # The point of keeping the text out of code. If the owner writes their
        # own sentence, that sentence is what is required - not mine.
        mine = {"ai": {"required": True,
                       "text": "Artwork generated with AI under my direction."}}
        ok, _ = self.d.report("A hoodie. Artwork generated with AI under my direction.",
                              rules=mine)
        self.assertTrue(ok)
        ok, _ = self.d.report("A hoodie. " + self.d.DEFAULTS["ai"]["text"], rules=mine)
        self.assertFalse(ok, "the default must not satisfy a rule the owner rewrote")

    def test_an_optional_rule_is_not_enforced(self):
        rules = {"x": {"required": False, "text": "something"}}
        self.assertTrue(self.d.report("nothing here", rules=rules)[0])

    def test_whitespace_and_case_do_not_defeat_it(self):
        rules = {"ai": {"required": True, "text": "Made with AI."}}
        self.assertTrue(self.d.report("a hoodie.   MADE   WITH   ai.  ", rules=rules)[0])

    def test_ensure_adds_both_when_neither_is_present(self):
        desc, added = self.d.ensure("A cosy hoodie with a mountain badge.")
        self.assertEqual(len(added), 2)
        self.assertTrue(self.d.report(desc)[0],
                        "ensure must produce something report() accepts")

    def test_ensure_is_idempotent(self):
        once, _ = self.d.ensure("A cosy hoodie.")
        twice, added = self.d.ensure(once)
        self.assertEqual(added, [], "re-running must not duplicate the lines")
        self.assertEqual(once, twice)

    def test_ensure_leaves_a_complete_description_untouched(self):
        rules = self.d.load()
        desc = ("A cosy hoodie. " + rules["production_partner"]["text"]
                + " " + rules["ai"]["text"])
        out, added = self.d.ensure(desc)
        self.assertEqual(added, [])
        self.assertEqual(out, desc)

    def test_ensure_adds_only_what_is_missing(self):
        rules = self.d.load()
        desc, added = self.d.ensure("A cosy hoodie. " + rules["ai"]["text"])
        self.assertEqual(added, [rules["production_partner"]["text"]])
        self.assertEqual(desc.count(rules["ai"]["text"]), 1)

    def test_ensure_does_not_add_an_optional_rule(self):
        rules = {"x": {"required": False, "text": "something"}}
        desc, added = self.d.ensure("a hoodie", rules=rules)
        self.assertEqual(added, [])
        self.assertEqual(desc, "a hoodie")

    def test_ensure_of_an_empty_description_does_not_lead_with_blank_lines(self):
        desc, added = self.d.ensure("")
        self.assertEqual(len(added), 2)
        self.assertFalse(desc.startswith("\n"))
        self.assertTrue(self.d.report(desc)[0])

    def test_ensure_follows_the_owners_wording_too(self):
        # Same point as report(): the file decides, not this code.
        mine = {"ai": {"required": True, "text": "Artwork made with AI."}}
        desc, added = self.d.ensure("A hoodie.", rules=mine)
        self.assertEqual(added, ["Artwork made with AI."])
        self.assertNotIn(self.d.DEFAULTS["ai"]["text"], desc)

    def test_draft_adds_the_lines_rather_than_refusing(self):
        # The incident: two finished hoodies sat at LOCAL ONLY because draft
        # refused over a rule Emily's header never mentioned. A rule enforced
        # in code and absent from the instructions fails the agent for
        # something it was never told.
        # (draft does still refuse art knockout.py cannot cut - that is a
        # different refusal, over something Emily's header does tell her.)
        body = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1]
        self.assertIn("disclosures.ensure", body)
        self.assertNotIn("disclosures.report", body,
                         "draft must not refuse over disclosures again")

    def test_draft_writes_the_corrected_description_back_to_disk(self):
        # Otherwise what Printify holds and what listing.json says would be
        # two different descriptions, and the next reader would see the old one.
        pf = (SCRIPTS / "emily-printify.py").read_text()
        body = pf.split("def cmd_draft(", 1)[1]
        self.assertIn('listing["description"] = desc', body)
        self.assertIn('(d / "listing.json").write_text', body)

    def test_finish_does_not_still_explain_a_refusal_that_cannot_happen(self):
        # draft no longer emits it, so the branch matching on it was dead code
        # telling the owner to go and edit a file by hand.
        fin = (SCRIPTS / "emily-finish.py").read_text()
        self.assertNotIn("required disclosure", fin)


class RemovingABuildDoesNotDestroyIt(unittest.TestCase):
    """"How do I delete designs if I don't like them" had no answer: the only
    way was rm -rf on a folder the gallery reads, with a Printify product
    possibly still pointing at it.

    remove archives to builds/_removed/ instead of deleting. The artwork cost a
    model call, and "I do not like it" and "destroy it" are different
    intentions.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.builds = self.root / "agents" / "emily" / "builds"
        self.builds.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, slug, **fields):
        d = self.builds / slug
        d.mkdir()
        (d / "build.json").write_text(json.dumps(dict(status="ready_local", **fields)))
        (d / "design.png").write_bytes(b"not really a png")
        return d

    def run_build(self, *args):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "emily-build.py"), *args],
            capture_output=True, text=True, env=env, timeout=60)

    def test_remove_archives_rather_than_deletes(self):
        self.build("dislike-this")
        r = self.run_build("remove", "dislike-this")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.builds / "dislike-this").exists())
        kept = self.builds / "_removed" / "dislike-this"
        self.assertTrue((kept / "design.png").is_file(),
                        "the artwork must survive - it cost a model call")

    def test_restore_puts_it_back(self):
        self.build("dislike-this")
        self.run_build("remove", "dislike-this")
        r = self.run_build("restore", "dislike-this")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.builds / "dislike-this" / "design.png").is_file())

    def test_a_build_with_a_printify_product_is_refused(self):
        # Archiving the folder does not remove the product. Printify would
        # still hold it with nothing here pointing at it.
        self.build("has-a-product", printify_product_id="abc123")
        r = self.run_build("remove", "has-a-product")
        self.assertEqual(r.returncode, 1)
        self.assertIn("abc123", r.stderr)
        self.assertTrue((self.builds / "has-a-product").is_dir(),
                        "a refusal must not half-remove it")

    def test_force_removes_one_with_a_product(self):
        self.build("has-a-product", printify_product_id="abc123")
        r = self.run_build("remove", "has-a-product", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.builds / "_removed" / "has-a-product").is_dir())

    def test_removing_the_same_slug_twice_does_not_overwrite_the_first(self):
        self.build("twice")
        self.run_build("remove", "twice")
        self.build("twice")
        r = self.run_build("remove", "twice")
        self.assertEqual(r.returncode, 0, r.stderr)
        gone = sorted(p.name for p in (self.builds / "_removed").iterdir())
        self.assertEqual(gone, ["twice", "twice-2"],
                         "the second archive must not clobber the first")

    def test_removing_something_that_does_not_exist_is_an_error_not_a_shrug(self):
        r = self.run_build("remove", "never-existed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("never-existed", r.stderr)

    def test_list_names_the_live_builds_and_the_archived_ones(self):
        self.build("keep-this")
        self.build("drop-this")
        self.run_build("remove", "drop-this")
        r = self.run_build("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("keep-this", r.stdout)
        self.assertIn("drop-this", r.stdout)
        self.assertIn("1 build(s)", r.stdout)

    def test_the_archive_folder_is_hidden_from_the_gallery(self):
        # remove tells the owner "the gallery will stop showing it". The
        # gallery is JS in another folder and decides for itself, so this
        # checks the claim against its actual regex rather than trusting it.
        js = (ROOT / "mission-control-api" / "emily.js").read_text()
        m = re.search(r"const SLUG_RE = /(.+?)/;", js)
        self.assertIsNotNone(m, "emily.js no longer declares SLUG_RE this way")
        slug_re = re.compile(m.group(1))
        self.assertIsNone(slug_re.match("_removed"),
                          "the gallery would still list the archive folder")
        self.assertIsNotNone(slug_re.match("dislike-this"),
                             "...and it must still list real builds")

    def test_an_archived_build_no_longer_counts_as_emily_being_busy(self):
        # shutil.move keeps the mtime, so without this the Deck's freshness
        # clock would read an archived build forever.
        js = (ROOT / "mission-control-api" / "dashboard-data.js").read_text()
        scan = js.split("'builds'", 1)[1]
        self.assertIn("startsWith('_')", scan[:600])


class AnEmptySlateIsNotASkippedCycle(unittest.TestCase):
    """Ace's 03:31 scheduled run on 2026-09-18 was failed for "state/ledger.json
    has no candidates - a cycle that looked at nothing is not a pass, it is a
    cycle that did not run".

    The fetcher had offered nothing: games_shown 0, games_outside_window 0,
    candidates []. MLB finished for the day, NFL not until Sunday, and
    ace-fetch keeps only games starting within 14 hours. The ledger even
    carried the verdict line - "No picks - nothing cleared the bar on the
    no-vig line" - so Ace had done every part of its job, including the one
    added hours earlier, and was failed for not judging games that did not
    exist. It recurs every Friday in this part of the season.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = self.root / "agents" / "ace"
        for d in ("data", "state", "reports"):
            (self.agent / d).mkdir(parents=True)
        self.started = int(time.time()) - 120
        want, _ = et_time.expected_report(self.agent, "ace", started=self.started)
        (self.agent / "reports" / want).write_text("word " * 196)
        (self.agent / "MEMORY.md").write_text("- quiet slate\n")
        (self.agent / "state" / ".cycle-started").write_text(str(self.started))
        (self.agent / "state" / ".cycle-before").write_text("6")
        (self.agent / "state" / "bankroll.json").write_text(json.dumps(
            {"starting_bankroll": 10000.0, "bankroll": 10000.0, "open_bets": [],
             "settled_bets": [], "cycle_count": 7}))
        self.ledger(verdict="No picks - nothing cleared the bar on the no-vig line.")

    def tearDown(self):
        self.tmp.cleanup()

    def slate(self, n):
        rows = [{"selection": f"S{i}", "match": "A @ B", "price": -110} for i in range(n)]
        (self.agent / "data" / "candidates.json").write_text(json.dumps(
            {"day": "2026-09-18", "slot": "night", "games_shown": n,
             "games_outside_window": 0, "candidates": rows}))

    def ledger(self, rows=None, verdict=""):
        (self.agent / "state" / "ledger.json").write_text(json.dumps(
            {"day": "2026-09-17", "slot": "night", "verdict": verdict,
             "candidates": rows or []}))

    def verify(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "ace-verify.py"),
                               "40", "6", str(self.started)],
                              capture_output=True, text=True, env=env)

    def signoff(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "signoff.py"), "ace"],
                              capture_output=True, text=True, env=env)

    def test_the_run_that_was_wrongly_failed_now_passes(self):
        self.slate(0)
        r = self.verify()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no games on the slate", r.stdout)

    def test_signoff_agrees_and_does_not_send_it_hunting(self):
        self.slate(0)
        r = self.signoff()
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to judge", r.stdout)
        self.assertNotIn("pass 1", r.stdout,
                         "telling it to judge row 1 of an empty slate sends it "
                         "looking for a game that does not exist")

    def test_a_slate_that_was_offered_and_ignored_still_fails(self):
        self.slate(2)
        self.assertEqual(self.verify().returncode, 1)
        self.assertEqual(self.signoff().returncode, 1)

    def test_the_failure_names_how_many_were_offered(self):
        self.slate(2)
        self.assertIn("offered 2", self.verify().stdout)

    def test_an_empty_slate_still_owes_a_verdict_line(self):
        self.slate(0)
        self.ledger(verdict="")
        r = self.verify()
        self.assertEqual(r.returncode, 0, "a missing sentence is not a failed cycle")
        self.assertIn("even an empty slate gets a sentence", r.stdout)

    def test_an_unreadable_slate_does_not_excuse_an_empty_ledger(self):
        (self.agent / "data" / "candidates.json").write_text("{ not json")
        self.assertEqual(self.verify().returncode, 1,
                         "unknown is not the same as zero")


class OneGarmentIsOneCatalogueEntry(unittest.TestCase):
    """emily-finish on a finished hoodie stopped with "no catalogue entry for
    'sweatshirt'. Choose one once" - while the entry for that exact garment sat
    in the catalogue under "hoodie".

    The key is a word the model picked when it wrote listing.json and the
    lookup was exact. Worse than the stop was the advice: "choose one once"
    invites a second entry for a blueprint already chosen, and two catalogue
    entries for one garment is the drift this repo keeps paying for.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agents" / "emily" / "state").mkdir(parents=True)
        self.m = load("emily_printify", "emily-printify.py")
        self.m.CATALOG = self.root / "agents/emily/state/printify-catalog.json"

    def tearDown(self):
        self.tmp.cleanup()

    # The shape actually on the droplet: chosen under "hoodie", and saved
    # before blueprint_title existed, so it has none.
    DROPLET = {
        "sticker": {"blueprint_id": 564, "provider_id": 27},
        "hoodie": {"blueprint_id": 49, "provider_id": 99,
                   "variant_titles": ["Black / M", "Navy / L"]},
    }

    def test_the_exact_key_still_wins(self):
        self.assertEqual(self.m.resolve(self.DROPLET, "hoodie")[0], "hoodie")
        self.assertEqual(self.m.resolve(self.DROPLET, "sticker")[0], "sticker")

    def test_case_and_plural_do_not_miss(self):
        self.assertEqual(self.m.resolve(self.DROPLET, "Hoodie")[0], "hoodie")
        self.assertEqual(self.m.resolve(self.DROPLET, "hoodies")[0], "hoodie")

    def test_a_word_nothing_answers_to_is_still_a_miss(self):
        self.assertEqual(self.m.resolve(self.DROPLET, "mug"), (None, None))
        self.assertEqual(self.m.resolve(self.DROPLET, ""), (None, None))

    def test_the_blueprints_own_title_answers_without_a_synonym_list(self):
        # Once pick records the title, "sweatshirt" finds the hoodie because
        # the garment really is called one - nobody maintains that mapping.
        cat = {"hoodie": dict(self.DROPLET["hoodie"],
                              blueprint_title="Unisex Heavy Blend Hooded Sweatshirt")}
        for word in ("sweatshirt", "Sweatshirts", "hooded sweatshirt"):
            self.assertEqual(self.m.resolve(cat, word)[0], "hoodie", word)

    def test_a_title_word_that_is_not_the_garment_does_not_match(self):
        # Matching any word of the title made "blend" find the hoodie. A loose
        # match here is a wrong draft, not a near miss.
        title = "Unisex Heavy Blend Hooded Sweatshirt"
        for word in ("blend", "unisex", "heavy", "tee", "mug"):
            self.assertFalse(self.m.title_answers_to(title, word), word)

    def test_real_printify_titles_answer_to_their_own_garment(self):
        for title, word in (("Unisex Heavy Blend Hooded Sweatshirt", "sweatshirt"),
                            ("Unisex Jersey Short Sleeve Tee", "tee"),
                            ("Kiss-Cut Stickers", "sticker"),
                            ("Kiss-Cut Stickers", "stickers")):
            self.assertTrue(self.m.title_answers_to(title, word), (title, word))

    def test_two_entries_answering_to_one_word_is_refused_not_guessed(self):
        # A crewneck and a hoodie are both sweatshirts. Picking either would be
        # a silently wrong garment on a real order.
        two = {"hoodie": {"blueprint_title": "Unisex Heavy Blend Hooded Sweatshirt"},
               "crewneck": {"blueprint_title": "Unisex Crewneck Sweatshirt"}}
        with self.assertRaises(self.m.Ambiguous) as caught:
            self.m.resolve(two, "sweatshirt")
        self.assertEqual(caught.exception.keys, ["crewneck", "hoodie"])

    def test_the_miss_message_lists_what_is_already_there(self):
        # The whole defect: it said "choose one once" while the answer was
        # sitting in the catalogue.
        msg = self.m.no_entry(self.DROPLET, "sweatshirt")
        self.assertIn("hoodie", msg)
        self.assertIn("sticker", msg)
        self.assertIn("alias", msg)

    def test_the_miss_message_on_an_empty_catalogue_does_not_offer_an_alias(self):
        msg = self.m.no_entry({}, "sticker")
        self.assertNotIn("alias", msg)
        self.assertIn("suggest --product sticker", msg)

    def test_alias_teaches_the_existing_entry_rather_than_duplicating_it(self):
        self.m.CATALOG.write_text(json.dumps(self.DROPLET))

        class A:
            product = "hoodie"
            alias = ["sweatshirt"]
        self.m.cmd_alias(A())
        cat = self.m.read_catalog()
        self.assertEqual(sorted(cat), ["hoodie", "sticker"],
                         "aliasing must not create a second entry")
        self.assertEqual(self.m.resolve(cat, "sweatshirt")[0], "hoodie")

    def test_alias_refuses_to_point_one_word_at_two_entries(self):
        self.m.CATALOG.write_text(json.dumps(self.DROPLET))

        class A:
            product = "hoodie"
            alias = ["sweatshirt"]
        self.m.cmd_alias(A())

        class B:
            product = "sticker"
            alias = ["sweatshirt"]
        with self.assertRaises(SystemExit) as caught:
            self.m.cmd_alias(B())
        self.assertEqual(caught.exception.code, 1)

    def test_alias_is_idempotent(self):
        self.m.CATALOG.write_text(json.dumps(self.DROPLET))

        class A:
            product = "hoodie"
            alias = ["sweatshirt"]
        self.m.cmd_alias(A())
        self.m.cmd_alias(A())
        self.assertEqual(self.m.read_catalog()["hoodie"]["aliases"], ["sweatshirt"])

    def test_pick_records_the_title_so_the_next_word_resolves_itself(self):
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_pick(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"blueprint_title": blueprint_title', body,
                      "the title must go into the saved entry, not just a local")

    def test_only_one_place_says_there_is_no_catalogue_entry(self):
        # draft and prices each had their own lookup and their own message,
        # which is how the advice in one went stale while the other stayed.
        src = (SCRIPTS / "emily-printify.py").read_text()
        self.assertEqual(src.count('f"no catalogue entry for'), 1,
                         "only no_entry() may compose that message")
        for name in ("cmd_draft", "cmd_prices"):
            body = src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]
            self.assertIn("no_entry(", body, name)

    # --- the real thing: run draft far enough to hit the catalogue -----------
    #
    # It resolves the product type before it needs a shop id or the network, so
    # the lookup can be exercised for real rather than asserted about in source.

    def draft(self, product_type, catalogue):
        d = self.root / "agents" / "emily" / "builds" / "a-build"
        d.mkdir(parents=True, exist_ok=True)
        (d / "listing.json").write_text(json.dumps(
            {"title": "A Hoodie", "description": "cosy", "product_type": product_type}))
        (d / "design.png").write_bytes(b"x" * 4000)
        self.m.CATALOG.parent.mkdir(parents=True, exist_ok=True)
        self.m.CATALOG.write_text(json.dumps(catalogue))
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        env.pop("PRINTIFY_SHOP_ID", None)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "emily-printify.py"), "draft", str(d)],
            capture_output=True, text=True, env=env, timeout=60)

    def test_draft_of_a_sweatshirt_reaches_the_hoodie_entry(self):
        # The incident, end to end: Emily wrote "sweatshirt", the entry is
        # "hoodie", and draft stopped dead.
        cat = {"hoodie": dict(self.DROPLET["hoodie"],
                              blueprint_title="Unisex Heavy Blend Hooded Sweatshirt")}
        r = self.draft("sweatshirt", cat)
        out = r.stdout + r.stderr
        self.assertNotIn("no catalogue entry", out, out)
        self.assertIn("'sweatshirt' -> catalogue entry 'hoodie'", out,
                      "and it must say which entry it used")

    def test_draft_of_something_really_absent_still_stops(self):
        r = self.draft("mug", {"hoodie": self.DROPLET["hoodie"]})
        self.assertEqual(r.returncode, 2)
        self.assertIn("no catalogue entry for 'mug'", r.stderr)
        self.assertIn("hoodie", r.stderr, "it must list what IS there")

    def test_draft_does_not_announce_a_rename_that_did_not_happen(self):
        r = self.draft("hoodie", {"hoodie": self.DROPLET["hoodie"]})
        self.assertNotIn("-> catalogue entry", r.stdout + r.stderr)


class CompletingATaskIsACommandNotAParagraph(unittest.TestCase):
    """Task #6 was failed with "agent exited with code 0". The dispatcher log
    shows Emily printed the curl she had been told to write -

        Here's the command to complete the task:
        ```bash
        curl -s -X POST .../tasks/6/complete -H '...' -d '{"result":...}'
        ```
        Proceeding with the completion now.

    - and then stopped. Three deterministic things were being asked of a cheap
    model at once: substitute a number into a <placeholder>, hand-write JSON
    inside a shell quote, and remember a flag. All three are now in a script,
    and the number is not typed at all.

    The queue here is a stub serving task #6 exactly as the droplet's API
    returned it, nested payload string and all - the shape is the thing that
    breaks, so it is the thing the tests use.
    """

    REAL_TASK_6 = {
        "id": 6, "created_at": "2026-09-18 13:13:14", "created_by": "you",
        "assignee": "emily", "type": "product-build", "status": "in_progress",
        # The payload is JSON *inside a JSON string*, which is how the API
        # really returns it. Every agent that read it unwrapped it by hand.
        "payload": json.dumps({
            "idea": "Left-Chest Lantern Emblem",
            "brief": '3-inch circular emblem: a lantern with a soft halo.',
            "product": "Gildan 18500 hooded sweatshirt",
            "slug": "left-chest-lantern-emblem",
            "build_dir": "builds/left-chest-lantern-emblem",
        }),
        "result": None, "cost_estimate": 0.25, "cost_actual": 0,
        "notes": "DRAFT ONLY - Emily must not publish.",
        "kill_criteria": "Stop if assets cannot be produced.",
    }

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        cls.completed = []
        task = cls.REAL_TASK_6

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b""):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/tasks/6":
                    self._send(200, json.dumps(task).encode())
                else:
                    self._send(404, b'{"error":"not found"}')

            def do_POST(self):
                if self.path == "/tasks/6/complete":
                    n = int(self.headers.get("Content-Length") or 0)
                    cls.completed.append(json.loads(self.rfile.read(n) or b"{}"))
                    self._send(200, b'{"ok":true}')
                else:
                    self._send(404, b'{"error":"not found"}')

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        type(self).completed = []

    def run_task(self, *args, task_id="6", api=None):
        env = dict(os.environ, MISSION_CONTROL_API=api or self.base)
        if task_id is None:
            env.pop("ECOSYSTEM_TASK_ID", None)
        else:
            env["ECOSYSTEM_TASK_ID"] = task_id
        return subprocess.run([sys.executable, str(SCRIPTS / "task.py"), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_the_agent_does_not_type_the_task_number(self):
        # A <placeholder> in an instruction is a thing to get wrong.
        r = self.run_task("read")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Task #6", r.stdout)

    def test_read_unwraps_the_payload_that_is_json_inside_json(self):
        # Unwrapped means each field on its own line. Asserting only that the
        # slug appears somewhere passes on the raw JSON string too - which is
        # exactly the dump this is supposed to prevent.
        r = self.run_task("read")
        self.assertIn("\n  slug: left-chest-lantern-emblem\n", r.stdout)
        self.assertIn("\n  build_dir: builds/left-chest-lantern-emblem\n", r.stdout)
        self.assertNotIn('{"idea"', r.stdout, "the payload must be unwrapped, not dumped")

    def test_read_shows_the_limits_the_task_was_created_with(self):
        r = self.run_task("read")
        self.assertIn("must not publish", r.stdout)
        self.assertIn("kill_criteria", r.stdout)

    def test_done_sends_what_the_queue_expects(self):
        r = self.run_task("done", "Drafted the hoodie, UNPUBLISHED.")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.completed,
                         [{"result": "Drafted the hoodie, UNPUBLISHED.",
                           "cost_actual": 0.0}])

    def test_done_takes_the_line_without_quoting_json_by_hand(self):
        # The words arrive as arguments; the script owns the JSON. Emily's
        # apostrophes and quotes are hers to write, not to escape.
        r = self.run_task("done", "Emily's", '"cosy"', "hoodie: done")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.completed[0]["result"], 'Emily\'s "cosy" hoodie: done')

    def test_a_typed_number_is_accepted_rather_than_punished(self):
        r = self.run_task("done", "6", "did the thing", task_id=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.completed[0]["result"], "did the thing")

    def test_cost_is_optional_and_parsed(self):
        r = self.run_task("done", "did it", "--cost", "0.25")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.completed[0]["cost_actual"], 0.25)
        self.assertEqual(self.completed[0]["result"], "did it")

    def test_done_with_nothing_to_say_is_refused(self):
        r = self.run_task("done")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.completed, [])

    def test_a_queue_that_is_down_says_so_instead_of_a_traceback(self):
        r = self.run_task("read", api="http://127.0.0.1:1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot reach the queue", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_task_number_that_does_not_exist_says_which(self):
        r = self.run_task("read", task_id="999")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no task with that number", r.stderr)

    def test_with_no_number_anywhere_it_asks_rather_than_guessing(self):
        r = self.run_task("read", task_id=None)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no task number", r.stderr)

    # --- the instruction and the command are one fact -----------------------

    def test_the_wake_message_names_commands_that_exist(self):
        t = load("task_py", "task.py")
        msg = t.wake_message(6)
        wanted = set(re.findall(r"scripts/task\.py (\w+)", msg))
        self.assertTrue(wanted, "the wake message must name the commands")
        for cmd in wanted:
            r = self.run_task(cmd, "a line" if cmd == "done" else "")
            self.assertNotEqual(r.returncode, 2,
                                f"the wake message tells agents to run "
                                f"'{cmd}', which task.py does not accept")

    def test_the_wake_message_does_not_ask_for_a_number(self):
        t = load("task_py", "task.py")
        msg = t.wake_message(6)
        self.assertNotIn("<the number", msg)
        self.assertNotIn("curl", msg,
                         "hand-written curl is what task #6 died of")

    def test_the_dispatcher_does_not_keep_its_own_copy_of_the_wake_message(self):
        src = (SCRIPTS / "task-dispatcher.py").read_text()
        self.assertIn("task.wake_message(", src)
        self.assertNotIn("/complete -H", src)

    def test_the_dispatcher_exports_the_task_number(self):
        # Without this the script has no number and the agent is back to
        # substituting one by hand.
        src = (SCRIPTS / "task-dispatcher.py").read_text()
        body = src.split("def spawn_agent(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("task.TASK_ENV", body)
        self.assertIn('"env": env', body)

    def test_emilys_header_no_longer_tells_her_to_write_curl(self):
        h = (ROOT / "agents" / "emily" / "_emily-agents-header.md").read_text()
        self.assertNotIn("tasks/<the number", h)
        self.assertIn("scripts/task.py read", h)
        self.assertIn('scripts/task.py done', h)


class WhatACycleCosts(unittest.TestCase):
    """Pricing is arithmetic, and a model asked to do arithmetic will
    eventually claim it did. So the rates are a table, the schedule is read
    from the timers, and the multiplication is checked here against numbers
    worked by hand.
    """

    def setUp(self):
        self.m = load("cost_estimate", "cost-estimate.py")

    def test_the_arithmetic_is_the_arithmetic(self):
        # prompt 1000, 2 turns, 100 out, no tool results.
        #   transcript = 100 * 2*1/2            = 100
        #   input      = 1000*2 + 100           = 2,100
        #   output     = 100*2                  = 200
        #   opus       = (2100*5 + 200*25)/1e6  = $0.0155
        in_t, out_t, dollars, _ = self.m.cycle_cost(
            "claude-opus-5", prompt=1000, turns=2, output=100, growth=100,
            cached=False)
        self.assertEqual((in_t, out_t), (2100, 200))
        self.assertAlmostEqual(dollars, 0.0155, places=6)

    def test_the_cached_arithmetic_too(self):
        #   billed = 1000*1.25 + 1000*0.1*1 + 100 = 1,450
        #   opus   = (1450*5 + 200*25)/1e6        = $0.01225
        _, _, dollars, _why = self.m.cycle_cost(
            "claude-opus-5", prompt=1000, turns=2, output=100, growth=100,
            cached=True)
        self.assertAlmostEqual(dollars, 0.01225, places=6)

    def test_turns_cost_more_than_linearly(self):
        # The point of the whole script: every turn re-reads everything said
        # so far, so doubling the turns more than doubles the bill. "Fixing a
        # loop saves more than switching models" is this inequality.
        def cost(turns):
            return self.m.cycle_cost("claude-opus-5", 2000, turns, 400, 1000,
                                     cached=False)[2]
        self.assertGreater(cost(24), 2 * cost(12))

    def test_a_cheaper_model_on_a_longer_loop_can_cost_more(self):
        # The claim in CLAUDE.md, as a number: 68 turns of Haiku against 12 of
        # Opus. If this ever stops being true the advice needs rewriting.
        opus = self.m.cycle_cost("claude-opus-5", 2000, 12, 400, 1000, False)[2]
        haiku = self.m.cycle_cost("claude-haiku-4-5", 2000, 68, 400, 1000, False)[2]
        self.assertGreater(haiku, opus)

    def test_caching_never_costs_more_than_not_caching(self):
        for model in self.m.PRICES:
            for turns in (1, 2, 12, 68):
                plain = self.m.cycle_cost(model, 8000, turns, 400, 1000, False)[2]
                cheap = self.m.cycle_cost(model, 8000, turns, 400, 1000, True)[2]
                self.assertLessEqual(cheap, plain + 1e-12, (model, turns))

    def test_a_prompt_too_short_to_cache_is_not_quietly_discounted(self):
        # Below the model's minimum the marker is ignored and nothing is
        # saved. Reporting a discount there would be inventing money.
        rate = self.m.PRICES["claude-haiku-4-5"]
        short = rate["cache_min"] - 1
        plain = self.m.cycle_cost("claude-haiku-4-5", short, 12, 400, 1000, False)[2]
        cheap, why = self.m.cycle_cost("claude-haiku-4-5", short, 12, 400, 1000, True)[2:]
        self.assertEqual(plain, cheap)
        self.assertIn("minimum", why)

    def test_one_turn_cannot_save_anything_by_caching(self):
        # A write with no read is strictly worse, so the cached column must
        # not undercut the plain one - and must say why it did not.
        plain = self.m.cycle_cost("claude-opus-5", 8000, 1, 400, 1000, False)[2]
        cheap, why = self.m.cycle_cost("claude-opus-5", 8000, 1, 400, 1000, True)[2:]
        self.assertEqual(cheap, plain)
        self.assertIn("nothing reads", why)

    # --- the schedule is read, not restated ---------------------------------

    def test_the_schedule_comes_from_the_timers(self):
        weekly = self.m.cycles_per_week()
        # ace wakes twice daily, belfort twice on weekdays.
        self.assertEqual(weekly["ace"], 14)
        self.assertEqual(weekly["belfort"], 10)
        self.assertEqual(weekly["scout"], 7)

    def test_a_timer_that_wakes_no_model_is_not_counted(self):
        # fury-cycle runs fury-collect.py. A timer is not a bill unless
        # something behind it calls a model.
        self.assertEqual(self.m.cycles_per_week()["fury"], 0)

    def test_an_execstart_that_runs_to_several_lines_is_still_read(self):
        # timmy's unit puts its openclaw call on a continuation line. Matching
        # only lines starting with ExecStart read it as waking no model, which
        # would have under-counted any inline unit written that way.
        self.assertGreater(self.m.cycles_per_week()["timmy"], 0)

    def test_every_cycle_timer_is_accounted_for(self):
        units = {u.name[:-len("-cycle.timer")]
                 for u in (ROOT / "deploy").glob("*-cycle.timer")}
        self.assertEqual(set(self.m.cycles_per_week()), units)

    # --- what it measures ---------------------------------------------------

    def test_the_prompt_is_measured_not_assumed(self):
        tokens, source = self.m.prompt_tokens_for("emily")
        self.assertGreater(tokens, 0)
        self.assertIn("emily", source)

    def test_an_unbuilt_agents_md_says_so_rather_than_understating_silently(self):
        # AGENTS.md is generated and untracked, so a checkout without a
        # droplet measures the header - a smaller number, and saying so is the
        # difference between an estimate and a wrong estimate.
        built = ROOT / "agents" / "emily" / "AGENTS.md"
        _tokens, source = self.m.prompt_tokens_for("emily")
        if not built.is_file():
            self.assertIn("larger", source)

    def test_an_agent_that_does_not_exist_is_not_priced(self):
        self.assertEqual(self.m.prompt_tokens_for("nobody"), (None, None))

    def test_the_rates_carry_the_date_they_were_read(self):
        # A price with no date is a price nobody can check - and the date has
        # to be ON the table, not somewhere else in the file.
        src = (SCRIPTS / "cost-estimate.py").read_text()
        preamble = src.split("PRICES = {", 1)[0].splitlines()[-12:]
        self.assertRegex("\n".join(preamble), r"read (on |from\n?)?[^\n]*20\d\d-\d\d-\d\d")
        self.assertIn("OpenRouter", src,
                      "these agents do not buy from Anthropic directly")

    def test_the_printed_estimate_says_whose_prices_these_are(self):
        # Grepping the source proves a caveat exists somewhere in the file.
        # What matters is that it reaches the person reading the number: these
        # agents buy through OpenRouter, which prices separately.
        r = subprocess.run([sys.executable, str(SCRIPTS / "cost-estimate.py"),
                            "--prompt-tokens", "8000", "--turns", "12"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OpenRouter", r.stdout)
        self.assertRegex(r.stdout, r"20\d\d-\d\d-\d\d")
        self.assertIn("estimated", r.stdout, "a token estimate must say it is one")

    def test_the_prompt_is_the_built_agents_md_when_there_is_one(self):
        # AGENTS.md is what the agent actually wakes with. On this checkout
        # there is none, so without building one the measuring branch is never
        # exercised and a hardcoded size would pass unnoticed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agents" / "emily").mkdir(parents=True)
            body = "x" * 12_000
            (root / "agents" / "emily" / "AGENTS.md").write_text(body)
            self.m.ROOT = root
            tokens, source = self.m.prompt_tokens_for("emily")
        self.assertEqual(tokens, len(body) // self.m.CHARS_PER_TOKEN)
        self.assertIn("AGENTS.md", source)
        self.assertNotIn("larger", source, "it IS the real prompt here")


class OpenRouterPricesAreReadNotAssumed(unittest.TestCase):
    """cost-estimate.py's table carries a date, and a dated number goes stale.
    `--rates` asks OpenRouter what it charges today instead.

    The fixture is five real entries captured from /api/v1/models on
    2026-09-18 - the shape is the thing that breaks, so the shape is what the
    tests use. It was captured because a hand-written parser against an
    imagined payload is how every format bug in this repo happened, and it
    earned its keep twice over within the hour: gpt-4o-mini has a null
    input_cache_write, and openrouter/auto publishes its price as "-1".
    """

    def setUp(self):
        self.m = load("cost_estimate", "cost-estimate.py")
        self.models = json.loads(
            (FIXTURES / "openrouter-models.json").read_text())["data"]

    def test_the_fixture_is_really_there(self):
        # A missing fixture must fail the test, not make it vacuous - the
        # credential-scan test passed for a week on a file that did not exist.
        self.assertTrue((FIXTURES / "openrouter-models.json").is_file())
        self.assertEqual(len(self.models), 5)

    def test_prices_arrive_per_token_as_strings_and_come_back_per_million(self):
        rows = dict((r[0], r) for r in self.m.rate_rows(self.models, ""))
        _, in_r, out_r, _, _ = rows["anthropic/claude-opus-5"]
        self.assertAlmostEqual(in_r, 5.00, places=6)
        self.assertAlmostEqual(out_r, 25.00, places=6)

    def test_openrouter_charges_anthropic_list_for_opus(self):
        # The claim the estimate rests on. If OpenRouter ever adds a margin
        # this fails and the table's note needs rewriting.
        rows = dict((r[0], r) for r in self.m.rate_rows(self.models, ""))
        _, in_r, out_r, _, _ = rows["anthropic/claude-opus-5"]
        table = self.m.PRICES["claude-opus-5"]
        self.assertAlmostEqual(in_r, table["in"], places=6)
        self.assertAlmostEqual(out_r, table["out"], places=6)

    def test_the_cache_multipliers_match_the_table_too(self):
        rows = dict((r[0], r) for r in self.m.rate_rows(self.models, ""))
        _, in_r, _, c_read, c_write = rows["anthropic/claude-opus-5"]
        table = self.m.PRICES["claude-opus-5"]
        self.assertAlmostEqual(c_read / in_r, table["cache_read"], places=6)
        self.assertAlmostEqual(c_write / in_r, table["cache_write"], places=6)

    def test_a_null_price_stays_null_rather_than_becoming_zero(self):
        # gpt-4o-mini really has no input_cache_write. Zero would read as
        # "caching is free on this model", which is a discount nobody offered.
        rows = dict((r[0], r) for r in self.m.rate_rows(self.models, ""))
        _, _, _, c_read, c_write = rows["openai/gpt-4o-mini"]
        self.assertIsNone(c_write)
        self.assertIsNotNone(c_read)

    def test_the_term_filters(self):
        ids = [r[0] for r in self.m.rate_rows(self.models, "opus")]
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.m.rate_rows(self.models, "gemini"), [])

    def test_a_price_of_minus_one_is_a_sentinel_not_a_discount(self):
        # openrouter/auto is a router: it picks a model per request and
        # publishes "-1" rather than a price. Multiplied out, that printed as
        # -$1,000,000 per million tokens - and it was belfort's configured
        # model, so it was the first thing anyone would have looked up.
        rows = dict((r[0], r) for r in self.m.rate_rows(self.models, ""))
        _, in_r, out_r, _, _ = rows["openrouter/auto"]
        self.assertIsNone(in_r)
        self.assertIsNone(out_r)

    def test_a_router_is_listed_rather_than_hidden(self):
        # Skipping unpriced entries answered "nothing matches" to someone
        # asking what their own configured model costs.
        self.assertIn("openrouter/auto",
                      [r[0] for r in self.m.rate_rows(self.models, "auto")])

    def test_an_unreadable_price_is_none_and_does_not_crash(self):
        broken = [{"id": "a/b", "pricing": {"prompt": None, "completion": "1"}},
                  {"id": "c/d"},
                  {"id": "e/f", "pricing": {"prompt": "x", "completion": "1"}}]
        rows = self.m.rate_rows(broken, "")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r[1] is None for r in rows))

    def test_the_printed_output_says_a_router_has_no_price(self):
        # What matters is that the person reading it is not shown a number.
        r = subprocess.run([sys.executable, str(SCRIPTS / "cost-estimate.py"),
                            "--rates", "openrouter/auto"],
                           capture_output=True, text=True, timeout=90)
        # Skip only for the one thing this test cannot control. Treating any
        # non-zero exit as "no network" swallowed a crash - the formatter
        # raising on a None price exited non-zero and the test skipped, which
        # is the vacuous pass this suite keeps having to relearn.
        self.assertNotIn("Traceback", r.stderr, r.stderr[-800:])
        if r.returncode != 0 and "could not reach OpenRouter" in r.stderr:
            self.skipTest("OpenRouter unreachable from here")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("not published", r.stdout)
        self.assertNotIn("-1000000", r.stdout)
        self.assertNotIn("$-", r.stdout)


class CreatingATaskIsACommandToo(unittest.TestCase):
    """task.py took the hand-written curl away from Emily and left it for the
    person at the terminal: creating a task meant a POST whose payload is JSON
    nested inside JSON inside a shell quote, on a terminal that flattens
    multi-line pastes. Same trap, different victim.

    `new` takes the fields as arguments and owns the quoting, the way
    scout-ideas.py propose does - and for the same reason, which is that a
    3.5" inch mark in a brief broke a hand-written payload once already.
    """

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        cls.got = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b""):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                cls.got.append(body)
                if body.get("assignee") == "dupe":
                    return self._send(409, b'{"error":"an open task with this '
                                           b'dedupe_key already exists"}')
                if body.get("assignee") == "broke":
                    return self._send(429, b'{"error":"daily_total_spend_cap 10 '
                                           b'would be exceeded"}')
                self._send(201, json.dumps({"id": 7, **body}).encode())

            def do_GET(self):
                self._send(200, json.dumps([
                    {"id": 7, "status": "pending", "assignee": "emily",
                     "payload": json.dumps({"idea": "Left-Chest Lantern Emblem"})},
                    {"id": 6, "status": "failed", "assignee": "emily",
                     "payload": "{bad json", "result": "agent exited with code 0"},
                    {"id": 5, "status": "done", "assignee": "emily", "payload": None},
                ]).encode())

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        type(self).got = []

    def run_task(self, *args):
        env = dict(os.environ, MISSION_CONTROL_API=self.base)
        env.pop("ECOSYSTEM_TASK_ID", None)
        return subprocess.run([sys.executable, str(SCRIPTS / "task.py"), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_the_fields_go_in_as_arguments(self):
        r = self.run_task("new", "emily", "--idea", "Lantern Emblem",
                          "--brief", "a lantern", "--product", "Gildan 18500")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = self.got[0]["payload"]
        self.assertEqual(payload["idea"], "Lantern Emblem")
        self.assertEqual(payload["product"], "Gildan 18500")

    def test_an_inch_mark_in_the_brief_survives(self):
        # The exact value that broke a hand-written payload: 3.5" closes the
        # JSON string early unless something escapes it.
        brief = 'Emblem with a "soft halo", 3.5" wide'
        r = self.run_task("new", "emily", "--idea", "x", "--brief", brief)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.got[0]["payload"]["brief"], brief)

    def test_a_slug_becomes_the_build_dir_and_the_dedupe_key(self):
        # Both were derived by hand from the slug every time a task was
        # written, which is two chances to mistype one string.
        self.run_task("new", "emily", "--idea", "x", "--brief", "y",
                      "--slug", "left-chest-lantern-emblem")
        body = self.got[0]
        self.assertEqual(body["payload"]["build_dir"],
                         "builds/left-chest-lantern-emblem")
        self.assertEqual(body["dedupe_key"],
                         "emily-build-left-chest-lantern-emblem")

    def test_a_task_without_a_slug_has_no_dedupe_key(self):
        # A dedupe key of "emily-build-" would collide with every other
        # slugless task.
        self.run_task("new", "emily", "--idea", "x", "--brief", "y")
        self.assertIsNone(self.got[0].get("dedupe_key"))

    def test_the_draft_only_note_is_carried_by_default(self):
        self.run_task("new", "emily", "--idea", "x", "--brief", "y")
        self.assertIn("do not publish", self.got[0]["notes"].lower())
        self.assertTrue(self.got[0]["kill_criteria"])

    def test_an_already_open_task_says_so_and_says_where_to_look(self):
        r = self.run_task("new", "dupe", "--idea", "x", "--brief", "y")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already an open task", r.stderr)
        self.assertIn("task.py list", r.stderr)

    def test_a_broken_spend_cap_names_the_cap_and_where_it_lives(self):
        r = self.run_task("new", "broke", "--idea", "x", "--brief", "y")
        self.assertEqual(r.returncode, 1)
        self.assertIn("daily_total_spend_cap", r.stderr)
        self.assertIn("limits.json", r.stderr)

    def test_list_distinguishes_absent_unreadable_and_present(self):
        # Rendering all three as a blank line is how a broken payload looks
        # like a task with nothing in it.
        r = self.run_task("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Left-Chest Lantern Emblem", r.stdout)
        self.assertIn("will not parse", r.stdout)
        self.assertIn("no payload", r.stdout)

    def test_list_shows_why_a_failed_task_failed(self):
        r = self.run_task("list")
        self.assertIn("agent exited with code 0", r.stdout)


class TheTaskNumberHasToActuallyArrive(unittest.TestCase):
    """Task #7 failed with Emily saying, correctly, that ECOSYSTEM_TASK_ID was
    not set and she could not name her own task.

    The dispatcher was exporting it onto the openclaw CLI process. openclaw
    runs the agent from its gateway, in a process that never inherits that, so
    it reached nothing. Worse, the wake message said "both commands already
    know the task number - do not type one", which forbade the one workaround
    that would have worked: an instruction that rules out the fallback turns a
    degraded path into a dead one.

    The number now travels three ways - an argument, a file the dispatcher
    writes into the agent's own folder, and the environment variable - because
    being unable to name your own task is a failed cycle.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent_dir = self.root / "agents" / "emily"
        self.agent_dir.mkdir(parents=True)
        self.t = load("task_py", "task.py")
        self.d = load("dispatcher", "task-dispatcher.py")
        self.d.AGENTS_DIR = self.root / "agents"

    def tearDown(self):
        self.tmp.cleanup()

    def run_from_agent_dir(self, *args, env_task=None):
        env = dict(os.environ, MISSION_CONTROL_API="http://127.0.0.1:1")
        env.pop("ECOSYSTEM_TASK_ID", None)
        if env_task:
            env["ECOSYSTEM_TASK_ID"] = env_task
        return subprocess.run([sys.executable, str(SCRIPTS / "task.py"), *args],
                              capture_output=True, text=True, env=env,
                              cwd=self.agent_dir, timeout=60)

    def test_the_dispatcher_writes_the_number_where_the_agent_will_find_it(self):
        self.d.write_task_marker("emily", 7)
        self.assertEqual(
            (self.agent_dir / "state" / "current-task").read_text().strip(), "7")

    def test_an_agent_with_no_env_var_still_knows_its_task(self):
        # The whole incident: no ECOSYSTEM_TASK_ID anywhere.
        self.d.write_task_marker("emily", 7)
        r = self.run_from_agent_dir("read")
        # The queue is unreachable here, so it must get as far as TRYING task 7
        # rather than stopping at "no task number".
        self.assertEqual(r.returncode, 1)
        self.assertIn("#7", r.stderr)
        self.assertNotIn("no task number", r.stderr)

    def test_the_marker_is_cleared_so_a_later_wake_cannot_read_a_stale_number(self):
        self.d.write_task_marker("emily", 7)
        self.d.clear_task_marker("emily")
        r = self.run_from_agent_dir("read")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no task number", r.stderr)

    def test_clearing_a_marker_that_is_not_there_is_silent(self):
        # Not merely "does not raise" - FileNotFoundError is an OSError, so a
        # broader handler catches it and logs a failure that did not happen.
        # A dispatcher that complains every reap teaches you to ignore it.
        said = []
        self.d.log = lambda msg: said.append(msg)
        self.d.clear_task_marker("emily")
        self.assertEqual(said, [])

    def test_an_explicit_number_still_wins(self):
        self.d.write_task_marker("emily", 7)
        r = self.run_from_agent_dir("read", "9")
        self.assertIn("#9", r.stderr)

    def test_the_env_var_still_works_where_it_does_arrive(self):
        r = self.run_from_agent_dir("read", env_task="5")
        self.assertIn("#5", r.stderr)

    def test_with_nothing_anywhere_it_points_at_the_wake_message(self):
        r = self.run_from_agent_dir("read")
        self.assertEqual(r.returncode, 2)
        self.assertIn("wake message", r.stderr)
        self.assertIn("task.py read", r.stderr)

    # --- the message must not forbid the fallback ---------------------------

    def test_the_wake_message_carries_the_number_in_both_commands(self):
        msg = self.t.wake_message(7)
        self.assertIn("task.py read 7", msg)
        self.assertIn("task.py done 7", msg)

    def test_the_wake_message_does_not_forbid_typing_the_number(self):
        # This sentence is what turned a missing env var into a dead end.
        msg = self.t.wake_message(7).lower()
        self.assertNotIn("do not type", msg)

    def test_the_dispatcher_writes_the_marker_before_it_spawns(self):
        src = (SCRIPTS / "task-dispatcher.py").read_text()
        body = src.split("def spawn_agent(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("write_task_marker(agent, task_id)", body)
        self.assertLess(body.index("write_task_marker"), body.index("Popen"))

    def test_the_dispatcher_clears_the_marker_when_the_agent_exits(self):
        src = (SCRIPTS / "task-dispatcher.py").read_text()
        body = src.split("def reap_finished(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("clear_task_marker(agent)", body)

    def test_a_marker_that_cannot_be_written_is_logged_not_swallowed(self):
        # The number is still in the wake message, so this is degraded rather
        # than fatal - but a silent failure here surfaces as a confused agent
        # an hour later, which is exactly how this bug presented.
        said = []
        self.d.log = lambda msg: said.append(msg)
        self.d.AGENTS_DIR = Path("/proc/nonexistent-and-unwritable")
        self.d.write_task_marker("emily", 7)
        self.assertTrue(said)
        self.assertIn("wake message", " ".join(said))


class TheGarmentsOwnNameShouldNotNeedAnAlias(unittest.TestCase):
    """Emily's first clean cycle ended with the finisher stopping on "hooded
    sweatshirt" - a fourth word for a garment already aliased as hoodie and
    sweatshirt.

    Aliasing is for a word the catalogue could not have guessed. "Hooded
    Sweatshirt" is literally in "Unisex Heavy Blend Hooded Sweatshirt", so it
    should never have needed one - but that entry was chosen before pick
    started recording the blueprint title, so title matching had nothing to
    match against and every wording had to be aliased by hand, one failed
    draft at a time. `refresh` fills the titles in.
    """

    # The droplet's catalogue as the failure printed it, plus the real titles.
    CAT = {
        "hoodie": {"blueprint_id": 77, "provider_id": 99, "aliases": ["sweatshirt"]},
        "kisscut": {"blueprint_id": 400, "provider_id": 1},
        "sticker": {"blueprint_id": 564, "provider_id": 27},
    }
    TITLES = {77: "Unisex Heavy Blend Hooded Sweatshirt",
              400: "Kiss-Cut Stickers", 564: "Kiss Cut Stickers"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agents" / "emily" / "state").mkdir(parents=True)
        self.m = load("emily_printify", "emily-printify.py")
        self.m.CATALOG = self.root / "agents/emily/state/printify-catalog.json"
        self.m.CATALOG.write_text(json.dumps(self.CAT))
        self.asked = []

        def fake_call(path):
            bid = int(path.split("/")[-1].split(".")[0])
            self.asked.append(bid)
            return {"title": self.TITLES[bid]}
        self.m.call = fake_call

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self):
        class A:
            pass
        return self.m.cmd_refresh(A())

    def test_the_word_that_failed_resolves_afterwards(self):
        cat = self.m.read_catalog()
        self.assertEqual(self.m.resolve(cat, "hooded sweatshirt"), (None, None))
        self.refresh()
        self.assertEqual(
            self.m.resolve(self.m.read_catalog(), "hooded sweatshirt")[0], "hoodie")

    def test_the_title_is_written_to_disk_not_just_used(self):
        self.refresh()
        saved = self.m.read_catalog()["hoodie"]["blueprint_title"]
        self.assertEqual(saved, self.TITLES[77])

    def test_existing_aliases_and_keys_are_left_alone(self):
        self.refresh()
        entry = self.m.read_catalog()["hoodie"]
        self.assertEqual(entry["aliases"], ["sweatshirt"])
        self.assertEqual(entry["blueprint_id"], 77)
        self.assertEqual(sorted(self.m.read_catalog()), ["hoodie", "kisscut", "sticker"])

    def test_an_entry_that_already_has_a_title_is_not_re_fetched(self):
        # One read per entry, once. A refresh that re-reads everything every
        # time is a refresh nobody runs.
        self.refresh()
        self.asked.clear()
        self.refresh()
        self.assertEqual(self.asked, [])

    def test_a_word_that_became_ambiguous_is_reported_not_raised(self):
        # "cut" reaches both kisscut and sticker. Walking every word of every
        # title through resolve() raised straight out of the report - after
        # the file had been written, so it half-succeeded and printed a
        # traceback. Ambiguity is the interesting part of this report.
        rc = self.refresh()
        self.assertEqual(rc, 0)

    def test_an_entry_with_no_blueprint_id_is_named_not_skipped_silently(self):
        self.m.CATALOG.write_text(json.dumps({"mug": {"provider_id": 1}}))
        self.assertEqual(self.refresh(), 1)

    def test_a_blueprint_that_cannot_be_read_does_not_lose_the_others(self):
        def boom(path):
            if "/77." in path:
                raise RuntimeError("network")
            bid = int(path.split("/")[-1].split(".")[0])
            return {"title": self.TITLES[bid]}
        self.m.call = boom
        self.assertEqual(self.refresh(), 1)
        cat = self.m.read_catalog()
        self.assertNotIn("blueprint_title", cat["hoodie"])
        self.assertEqual(cat["kisscut"]["blueprint_title"], self.TITLES[400])

    def test_an_empty_catalogue_is_not_an_error(self):
        self.m.CATALOG.write_text("{}")
        self.assertEqual(self.refresh(), 0)


class TheDeckDoesNotDecideWhatRemovingMeans(unittest.TestCase):
    """The Command Deck grew a Remove button. What removing a build means -
    archive to _removed/ rather than delete, refuse while a Printify product
    still points at it - was already decided by scripts/emily-build.py.

    Writing that again in JavaScript is the drift this repo keeps paying for:
    the two would agree on the day they were written and not afterwards. So
    emily.js runs the script. These tests hold that line, and hold the
    contract the dashboard depends on, without needing a server running.
    """

    JS = None
    HTML = None

    @classmethod
    def setUpClass(cls):
        cls.JS = (ROOT / "mission-control-api" / "emily.js").read_text()
        cls.HTML = (ROOT / "mission-control-api" / "public" / "dashboard.html").read_text()

    def test_the_endpoint_runs_the_script_rather_than_moving_files(self):
        # Reading _removed/ to list what can be restored is fine. Moving or
        # deleting anything is not: that is the script's decision to make.
        self.assertIn("emily-build.py", self.JS)
        for forbidden in ("fs.rename", "fs.renameSync", "fs.rm(", "fs.rmSync",
                          "fs.unlink", "rmdir", "fs.cpSync", "fs.mkdir"):
            self.assertNotIn(forbidden, self.JS,
                             f"emily.js must not implement removal itself ({forbidden})")

    def test_the_script_is_run_with_an_argument_array_not_a_shell_string(self):
        # A slug is data. execFile with an array keeps it that way even if the
        # slug regex is ever loosened.
        self.assertIn("execFile('python3', [BUILD_SCRIPT", self.JS)
        self.assertNotIn("exec(", self.JS.replace("execFile(", ""))

    def test_force_is_not_passed_unless_asked_for(self):
        # The refusal exists because archiving the folder does not remove the
        # product from Printify. A UI that always forces has deleted it.
        body = self.JS.split("app.delete(", 1)[1].split("app.post(", 1)[0]
        self.assertIn("force ?", body)
        self.assertIn("'--force'", body)
        self.assertIn("req.query.force === '1'", body)

    def test_the_three_outcomes_get_three_status_codes(self):
        # One status for all of them tells the dashboard nothing it can
        # respond to differently - and it responds differently to each.
        # Assert the status call, not the numbers - they also appear in the
        # comment above it, which survives any change to the code.
        body = self.JS.split("app.delete(", 1)[1].split("app.post(", 1)[0]
        self.assertIn("res.status(blocked ? 409 : missing ? 404 : 500)", body)

    def test_the_refusal_text_is_the_scripts_own(self):
        # Rewording it here is a second copy of the explanation.
        body = self.JS.split("app.delete(", 1)[1].split("app.post(", 1)[0]
        self.assertIn("r.stderr", body)

    def test_every_write_endpoint_checks_the_slug(self):
        for route in ("app.delete('/api/emily/builds/:slug'",
                      "app.post('/api/emily/builds/:slug/restore'"):
            self.assertIn(route, self.JS)
            body = self.JS.split(route, 1)[1][:400]
            self.assertIn("SLUG_RE.test", body, route)

    def test_there_is_still_no_publish_endpoint(self):
        # The one thing this file has always refused to do.
        self.assertNotIn("/publish", self.JS)
        self.assertIn("no endpoint for it here", self.JS)

    def test_removing_is_not_a_one_way_door(self):
        self.assertIn("/api/emily/removed", self.JS)
        self.assertIn("restore", self.JS)
        self.assertIn("restoreBuild", self.HTML)
        # Defined is not enough - the archived list has to be refreshed when
        # the gallery is, or it only appears after a full page reload.
        gallery = self.HTML.split("async function loadBuilds(", 1)[1].split(
            "async function removeBuild(", 1)[0]
        self.assertIn("loadRemoved()", gallery)

    def test_the_dashboard_asks_before_removing(self):
        body = self.HTML.split("async function removeBuild(", 1)[1].split(
            "async function restoreBuild(", 1)[0]
        # The guard, not just the word: `if (false && !confirm(...))` still
        # contains "confirm(" and asks nobody anything.
        self.assertIn("if (!force && !confirm(", body)
        # And asks a second time before overriding the Printify refusal,
        # rather than offering force as the first button.
        self.assertIn("d.blocked", body)
        self.assertIn("if (confirm(", body)
        self.assertEqual(body.count("confirm("), 2)

    def test_the_dashboard_shows_the_scripts_reason_not_its_own(self):
        body = self.HTML.split("async function removeBuild(", 1)[1].split(
            "async function restoreBuild(", 1)[0]
        self.assertIn("d.error", body)

    def test_the_file_says_it_writes_now(self):
        # It was documented "Read-only, unlike scout.js". A comment that is no
        # longer true is how the next reader gets a wrong idea for free.
        self.assertNotIn("Read-only, unlike scout.js", self.JS)


class TheImageModelKnobIsWiredToSomething(unittest.TestCase):
    """The art was disappointing and the obvious lever - draw it with a
    different model - was documented and dead.

    EMILY_IMAGE_MODEL was read into a module constant at import. Credentials
    are loaded inside main(), which runs after, so setting it in
    credentials.env - the only place this ecosystem keeps that kind of setting
    - was read before the file that sets it existed in the environment, and
    silently did nothing. A documented knob wired to nothing is worse than no
    knob, because it gets believed.
    """

    def setUp(self):
        self.m = load("emily_assets", "emily-assets.py")
        self.saved = os.environ.pop("EMILY_IMAGE_MODEL", None)

    def tearDown(self):
        os.environ.pop("EMILY_IMAGE_MODEL", None)
        if self.saved is not None:
            os.environ["EMILY_IMAGE_MODEL"] = self.saved

    def test_the_env_var_is_read_when_asked_not_when_imported(self):
        # Set AFTER import, the way load_credentials() does it.
        os.environ["EMILY_IMAGE_MODEL"] = "openai/gpt-5-image"
        self.assertEqual(self.m.image_model(), "openai/gpt-5-image")

    def test_an_explicit_model_beats_the_environment(self):
        os.environ["EMILY_IMAGE_MODEL"] = "openai/gpt-5-image"
        self.assertEqual(self.m.image_model("google/gemini-3-pro-image"),
                         "google/gemini-3-pro-image")

    def test_unset_and_blank_both_fall_back(self):
        self.assertEqual(self.m.image_model(), self.m.DEFAULT_IMAGE_MODEL)
        os.environ["EMILY_IMAGE_MODEL"] = "   "
        self.assertEqual(self.m.image_model(), self.m.DEFAULT_IMAGE_MODEL)

    def test_the_model_is_not_frozen_at_import(self):
        # The bug itself: a module-level constant cannot see a later change.
        src = (SCRIPTS / "emily-assets.py").read_text()
        head = src.split("def image_model(", 1)[0]
        self.assertNotIn('os.environ.get("EMILY_IMAGE_MODEL"', head)

    def test_generate_sends_the_chosen_model(self):
        src = (SCRIPTS / "emily-assets.py").read_text()
        body = src.split("def generate(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"model": model or image_model()', body)


class ComparingModelsIsLookingAtThem(unittest.TestCase):
    """"Would ChatGPT's images be better" has no answer in the abstract - it
    depends on the prompt, the garment, and the taste of whoever is selling
    them. So compare draws one prompt with several models and writes them into
    a build folder, because the Deck's gallery already renders a folder of
    images with a lightbox. The filename is the model, so what you are looking
    at is never a guess.
    """

    def setUp(self):
        self.m = load("emily_assets", "emily-assets.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "builds" / "bakeoff-fall"
        self.drawn = []

        self.costs = {}

        def fake_generate(path, prompt, key, model=None):
            self.drawn.append((model, prompt))
            if "refuses" in (model or ""):
                raise RuntimeError("provider returned 429")
            Path(path).write_bytes(b"\x89PNG" + b"x" * 900)
            usage = {"cost": self.costs[model]} if model in self.costs else {}
            return Path(path).stat().st_size, usage
        self.m.generate = fake_generate

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_model_gets_the_same_prompt(self):
        self.m.compare("a fall emblem", "a/one,b/two", "k", self.dir)
        self.assertEqual([p for _m, p in self.drawn], ["a fall emblem"] * 2)

    def test_the_filename_names_the_model(self):
        self.m.compare("x", "openai/gpt-5-image,google/gemini-2.5-flash-image",
                       "k", self.dir)
        names = sorted(p.name for p in self.dir.glob("*.png"))
        self.assertEqual(names, ["google-gemini-2.5-flash-image.png",
                                 "openai-gpt-5-image.png"])

    def test_one_model_failing_does_not_cost_you_the_others(self):
        rc = self.m.compare("x", "a/refuses,b/two,c/three", "k", self.dir)
        self.assertEqual(rc, 0, "two of three still drew something")
        self.assertEqual(len(list(self.dir.glob("*.png"))), 2)

    def test_a_failure_is_recorded_rather_than_only_printed(self):
        self.m.compare("x", "a/refuses,b/two", "k", self.dir)
        results = json.loads((self.dir / "comparison.json").read_text())["results"]
        failed = [r for r in results if not r["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("429", failed[0]["error"])

    def test_every_model_failing_is_a_failure(self):
        self.assertEqual(self.m.compare("x", "a/refuses", "k", self.dir), 1)

    def test_it_is_not_a_product(self):
        # No price, no printify id, and a status the gallery will not read as
        # something sellable.
        self.m.compare("x", "a/one", "k", self.dir)
        build = json.loads((self.dir / "build.json").read_text())
        listing = json.loads((self.dir / "listing.json").read_text())
        self.assertEqual(build["status"], "comparison")
        self.assertNotIn("printify_product_id", build)
        self.assertNotIn("price_suggestion", listing)

    def test_comparing_without_a_key_says_so_instead_of_drawing_placeholders(self):
        # A folder of identical geometric placeholders answers nothing, and
        # looks like the models all agreed.
        rc = self.m.compare("x", "a/one", "", self.dir)
        self.assertEqual(rc, 2)
        self.assertFalse(self.dir.exists())

    def test_no_models_named_is_refused(self):
        self.assertEqual(self.m.compare("x", "  ,  ", "k", self.dir), 2)

    def test_what_each_model_actually_cost_is_recorded(self):
        # An image's price cannot be worked out from the published rate:
        # image_output is dollars per output TOKEN, and how many tokens an
        # image is depends on the model, the size and the quality. So the run
        # reports what it cost rather than anyone estimating it.
        self.costs = {"a/one": 0.042, "b/two": 0.0081}
        self.m.compare("x", "a/one,b/two", "k", self.dir)
        results = json.loads((self.dir / "comparison.json").read_text())["results"]
        got = {r["model"]: r["cost_usd"] for r in results}
        self.assertEqual(got, {"a/one": 0.042, "b/two": 0.0081})

    def test_a_provider_that_reports_no_cost_is_none_not_zero(self):
        # Zero would read as "this model is free", which is the kind of number
        # that gets repeated.
        self.costs = {"a/one": 0.042}
        self.m.compare("x", "a/one,b/silent", "k", self.dir)
        results = json.loads((self.dir / "comparison.json").read_text())["results"]
        silent = next(r for r in results if r["model"] == "b/silent")
        self.assertIsNone(silent["cost_usd"])

    def test_cost_of_reads_what_the_provider_sent(self):
        self.assertEqual(self.m.cost_of({"cost": 0.039}), 0.039)
        self.assertEqual(self.m.cost_of({"total_cost": 0.039}), 0.039)
        self.assertIsNone(self.m.cost_of({}))
        self.assertIsNone(self.m.cost_of(None))
        self.assertIsNone(self.m.cost_of({"cost": "0.039"}),
                          "a string is not a number the arithmetic can trust")

    def test_the_request_asks_for_the_cost_back(self):
        src = (SCRIPTS / "emily-assets.py").read_text()
        body = src.split("def generate(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"usage": {"include": True}', body)


class ASettingYouCannotSeeChangesBackQuietly(unittest.TestCase):
    """Choosing an image model after comparing four of them writes one line
    into credentials.env and leaves no trace anywhere a person looks. If that
    file is ever rebuilt the setting reverts to the default silently, and the
    art quietly gets worse with nothing to notice.

    preflight already prints what model each agent thinks with, costs nothing
    and is run constantly. It prints what draws the art too now.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adir = Path(self.tmp.name) / "agents" / "emily"
        (self.adir / "state").mkdir(parents=True)
        self.pf = load("preflight", "preflight.py")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        (self.adir / "state" / "credentials.env").write_text(text)

    def test_the_chosen_model_is_shown(self):
        self.write("PRINTIFY_API_TOKEN=secret\nEMILY_IMAGE_MODEL=google/gemini-3-pro-image\n")
        self.assertEqual(self.pf.image_model_for("emily", self.adir),
                         "google/gemini-3-pro-image")

    def test_quotes_and_spacing_do_not_defeat_it(self):
        self.write('EMILY_IMAGE_MODEL = "google/gemini-3-pro-image" \n')
        self.assertEqual(self.pf.image_model_for("emily", self.adir),
                         "google/gemini-3-pro-image")

    def test_nothing_set_is_nothing_shown_not_a_guess(self):
        # Printing the default here would state as fact something the file
        # does not say - and the default is exactly what this is meant to
        # catch reverting to.
        self.write("PRINTIFY_API_TOKEN=secret\n")
        self.assertIsNone(self.pf.image_model_for("emily", self.adir))

    def test_a_blank_value_is_not_a_model(self):
        self.write("EMILY_IMAGE_MODEL=\n")
        self.assertIsNone(self.pf.image_model_for("emily", self.adir))

    def test_no_credentials_file_at_all(self):
        self.assertIsNone(self.pf.image_model_for("fury", self.adir.parent / "fury"))

    def test_it_reads_only_the_model_line(self):
        # This function touches a mode-600 file full of API tokens. It must
        # return the model and nothing else, ever.
        self.write("PRINTIFY_API_TOKEN=tok_do_not_print_me\n"
                   "OPENROUTER_API_KEY=sk-also-not-this\n"
                   "EMILY_IMAGE_MODEL=google/gemini-3-pro-image\n")
        got = self.pf.image_model_for("emily", self.adir)
        self.assertEqual(got, "google/gemini-3-pro-image")
        self.assertNotIn("tok_", got)
        self.assertNotIn("sk-", got)

    def test_a_token_that_merely_mentions_the_name_is_not_returned(self):
        self.write("SOMETHING_EMILY_IMAGE_MODEL_KEY=sk-not-a-model\n")
        self.assertIsNone(self.pf.image_model_for("emily", self.adir))


class OneParserForCredentialsEnv(unittest.TestCase):
    """emily-assets.py owns the credentials.env format. preflight grew a
    second parser to show which model draws the art, and the two disagreed on
    the first real line tried: `KEY = value` with spaces around the equals,
    which emily-assets accepts and the new one did not. A setting that worked
    perfectly would have been reported as absent.

    Tested here at its own level rather than only through its callers - both
    of those filter a blank value downstream, so a parser that returned one
    would have looked fine through either.
    """

    def setUp(self):
        self.m = load("emily_assets", "emily-assets.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "credentials.env"

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, text):
        self.path.write_text(text)
        return self.m.read_env_file(self.path)

    def test_plain(self):
        self.assertEqual(self.read("A=1\nB=two\n"), {"A": "1", "B": "two"})

    def test_spaces_around_the_equals(self):
        self.assertEqual(self.read("A = 1\n"), {"A": "1"})

    def test_quotes_are_stripped(self):
        self.assertEqual(self.read('A="one"\nB=\'two\'\n'), {"A": "one", "B": "two"})

    def test_comments_and_blank_lines_are_skipped(self):
        self.assertEqual(self.read("# a note\n\nA=1\n"), {"A": "1"})

    def test_a_blank_value_is_not_a_setting(self):
        # An empty value is someone who meant to set something and did not.
        # Returning "" makes the key look present to anything checking `in`.
        self.assertEqual(self.read("A=\nB=  \nC=1\n"), {"C": "1"})

    def test_a_value_containing_an_equals_survives_whole(self):
        # Tokens contain =, and splitting on every one would truncate them.
        self.assertEqual(self.read("TOKEN=abc=def==\n"), {"TOKEN": "abc=def=="})

    def test_a_line_with_no_equals_is_skipped_not_crashed_on(self):
        self.assertEqual(self.read("nonsense\nA=1\n"), {"A": "1"})

    def test_a_file_that_is_not_there_is_empty_not_an_error(self):
        self.assertEqual(self.m.read_env_file(self.path.parent / "nope.env"), {})

    def test_preflight_uses_this_one(self):
        src = (SCRIPTS / "preflight.py").read_text()
        self.assertIn("ea.read_env_file", src)
        body = src.split("def image_model_for(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("splitlines", body, "that is a second parser")
        self.assertNotIn('split("=", 1)', body, "that is a second parser")


class CollegeFootballIsTheSameBetOnABiggerBoard(unittest.TestCase):
    """CFB moneylines are the bet Ace already makes - same endpoint, same
    pickcenter block, same no-vig maths - so they went in the SPORTS table
    rather than into a second agent that would have needed its own fetch,
    judge, verify and sign-off.

    What they needed was a price filter. Sampled on a real board, CFB carried
    a moneyline on 9 of 14 games and the ones it did included -1350/+800 and
    -8000/+2200. Every value below is one that actually appeared.
    """

    def setUp(self):
        self.m = load("ace_fetch", "ace-fetch.py")

    def odds(self, home, away):
        return {"odds": {"moneyline_home": home, "moneyline_away": away}}

    def test_college_football_is_in_the_table(self):
        self.assertIn("cfb", self.m.SPORTS)
        self.assertEqual(self.m.SPORTS["cfb"]["path"], "football/college-football")

    def test_it_is_in_season_in_the_autumn_and_not_in_june(self):
        months = self.m.SPORTS["cfb"]["months"]
        self.assertTrue({9, 10, 11}.issubset(months))
        self.assertNotIn(6, months)

    def test_a_real_playable_game_is_playable(self):
        # LSU @ MISS +124/-148 and WVU/UVA -410/+320, both off today's board.
        self.assertEqual(self.m.unplayable(self.odds(124, -148)), "")
        self.assertEqual(self.m.unplayable(self.odds(-410, 320)), "")
        self.assertEqual(self.m.unplayable(self.odds(-111, -109)), "")

    def test_a_real_blowout_is_held_back(self):
        for home, away in ((-8000, 2200), (-5000, 1800), (-2800, 1300),
                           (-2100, 1100), (650, -1000)):
            self.assertIn("lopsided", self.m.unplayable(self.odds(home, away)),
                          f"{home}/{away}")

    def test_lopsidedness_is_the_favourite_not_the_smaller_number(self):
        # The defect this test exists for: taking min(abs(home), abs(away))
        # let -410/+320 through a band documented as "favourite no shorter
        # than -400", because the underdog's 320 was the smaller magnitude.
        # The rule is the favourite's price, so the band moves with it.
        band = self.m.MAX_FAVOURITE
        self.assertEqual(self.m.unplayable(self.odds(-band, band - 100)), "")
        self.assertIn("lopsided",
                      self.m.unplayable(self.odds(-(band + 1), band - 100)))
        # ...and it does not matter which side of the fixture the favourite is.
        self.assertIn("lopsided",
                      self.m.unplayable(self.odds(band - 100, -(band + 1))))

    def test_a_game_with_no_line_is_named_as_such_not_called_lopsided(self):
        # "No line was posted" and "the line is -5000" are different facts and
        # the slate summary says which. Five of fourteen sampled CFB games had
        # no moneyline at all.
        self.assertEqual(self.m.unplayable(self.odds(None, -148)),
                         "no moneyline posted")
        self.assertEqual(self.m.unplayable(self.odds(-148, None)),
                         "no moneyline posted")
        self.assertEqual(self.m.unplayable({}), "no moneyline posted")

    def test_a_price_that_is_not_a_number_does_not_crash_the_slate(self):
        self.assertEqual(self.m.unplayable(self.odds("x", 1)),
                         "moneyline is not a number")

    def test_a_string_price_that_is_a_number_still_works(self):
        # ESPN has handed back numbers as strings before.
        self.assertEqual(self.m.unplayable(self.odds("-150", "130")), "")


class TheWindowIsSharedBetweenSports(unittest.TestCase):
    """Adding college football to the table was not enough to get it judged.

    The slate is the playable games starting soonest, capped at MAX_GAMES.
    Baseball plays fourteen games a night and starts earlier, so on the first
    real Saturday board every one of the eight slots went to MLB - college
    football was fetched, priced, filtered and then crowded out before Ace saw
    any of it. Configured and never used is the same as not configured.

    Each sport now takes its next game in turn, soonest first within a sport.
    """

    def setUp(self):
        self.m = load("ace_fetch", "ace-fetch.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def game(self, sport, name, hours_out, home=-150, away=130):
        start = datetime.now(timezone.utc) + timedelta(hours=hours_out)
        (self.ctx / f"{sport}-{name}.json").write_text(json.dumps({
            "sport": sport, "short": name, "match": name,
            "start_utc": start.strftime("%Y-%m-%dT%H:%MZ"),
            "status": "STATUS_SCHEDULED",
            "home": {"abbr": "HOM"}, "away": {"abbr": "AWY"},
            "odds": {"moneyline_home": home, "moneyline_away": away,
                     "novig_home_pct": 58.0, "novig_away_pct": 42.0},
        }))

    def slate(self):
        out = self.m.build_ledger("2026-09-19", self.ctx, "afternoon")
        seen, games = set(), []
        for r in out["candidates"]:
            key = (r["sport"], r["match"])
            if key not in seen:
                seen.add(key)
                games.append(key)
        return out, games

    def test_baseball_no_longer_takes_every_slot(self):
        # The board that found this: MLB starting first, football later.
        for i in range(14):
            self.game("mlb", f"mlb{i}", 1 + i * 0.1)
        for i in range(6):
            self.game("cfb", f"cfb{i}", 5 + i * 0.1)
        _out, games = self.slate()
        sports = {s for s, _ in games}
        self.assertIn("cfb", sports, "college football must reach the slate")
        self.assertLessEqual(len([s for s, _ in games if s == "mlb"]),
                             self.m.MAX_GAMES - 1)

    def test_one_sport_alone_still_fills_the_window(self):
        # Sharing must not mean holding slots empty for a sport with no games.
        for i in range(12):
            self.game("mlb", f"mlb{i}", 1 + i * 0.1)
        _out, games = self.slate()
        self.assertEqual(len(games), self.m.MAX_GAMES)

    def test_the_slate_is_still_in_kick_off_order(self):
        for i in range(4):
            self.game("mlb", f"mlb{i}", 6 - i)
            self.game("cfb", f"cfb{i}", 6.5 - i)
        out, _games = self.slate()
        starts = [r["starts_utc"] for r in out["candidates"]]
        self.assertEqual(starts, sorted(starts))

    def test_lopsided_games_are_filtered_before_the_window_is_cut(self):
        # Otherwise blowouts take slots and the playable games behind them are
        # reported as "outside the window", which is a different claim.
        for i in range(8):
            self.game("cfb", f"blowout{i}", 1 + i * 0.1, home=-5000, away=1800)
        self.game("nfl", "playable", 9)
        out, games = self.slate()
        self.assertIn(("nfl", "playable"), games)
        self.assertEqual(out["games_filtered"], 8)

    def test_what_was_filtered_is_reported_with_its_reason(self):
        # A filter nobody can see silently decides the slate, and on a CFB
        # Saturday it decides most of it.
        self.game("cfb", "blowout", 1, home=-5000, away=1800)
        self.game("cfb", "noline", 2, home=None, away=None)
        self.game("nfl", "fine", 3)
        out, _games = self.slate()
        why = {f["match"]: f["why"] for f in out["filtered"]}
        self.assertIn("lopsided", why["blowout"])
        self.assertEqual(why["noline"], "no moneyline posted")
        self.assertNotIn("fine", why)


class TheWholeBoardNotAThirdOfIt(unittest.TestCase):
    """"There are dozens playing" - and there were. ESPN's college scoreboard
    returns 22 events by default where the real board has 75, so Ace was
    judging a third of a Saturday and never saw the rest. `limit` alone does
    nothing; it is `groups=80` that opens it.

    That left a second question: the per-game context fetch is capped at
    DEEP_CAP, and on a 43-game board WHICH twelve is the whole thing. It was
    the first twelve in ESPN's order - neither soonest nor closest. The
    scoreboard carries DraftKings' spread for every game at no extra call, so
    it decides: a 45.5-point spread is the same fact as a -8000 moneyline, and
    a fetch spent on it buys a row that can only ever be passed.
    """

    def setUp(self):
        self.m = load("ace_fetch", "ace-fetch.py")

    def event(self, name, hours_out, spread=None, when=None):
        start = (when or datetime.now(timezone.utc)) + timedelta(hours=hours_out)
        odds = [{"provider": {"name": "DraftKings"}, "spread": spread}] if spread is not None else []
        return {"shortName": name, "date": start.strftime("%Y-%m-%dT%H:%MZ"),
                "competitions": [{"odds": odds}]}

    def test_the_college_board_is_opened(self):
        q = self.m.SPORTS["cfb"].get("query", "")
        self.assertIn("groups=80", q)
        self.assertIn("limit=", q)

    def test_the_other_sports_are_left_alone(self):
        # NFL's 16 and MLB's 15 are already whole slates. A query string that
        # is not needed is a thing that can break.
        for sport in ("nfl", "mlb", "nba"):
            self.assertEqual(self.m.SPORTS[sport].get("query", ""), "")

    def test_the_query_is_actually_used_in_the_call(self):
        src = (SCRIPTS / "ace-fetch.py").read_text()
        self.assertIn("SPORTS[sport].get('query', '')", src)

    def test_the_ranking_is_actually_used_when_fetching(self):
        # Ranking correctly and then fetching in ESPN's order anyway is a
        # function that passes its own tests and changes nothing.
        src = (SCRIPTS / "ace-fetch.py").read_text()
        self.assertIn("pick_for_context(scheduled)[:DEEP_CAP]", src)
        self.assertNotIn("for e in scheduled[:DEEP_CAP]", src)

    # --- the spread off the scoreboard ---------------------------------------

    def test_the_spread_is_read_and_made_positive(self):
        # Real values: 'IU -45.5' and 'SC -3'. The sign says who is favoured,
        # which this does not care about - only how lopsided it is.
        self.assertEqual(self.m.board_spread(self.event("WKU @ IU", 3, -45.5)), 45.5)
        self.assertEqual(self.m.board_spread(self.event("MSST @ SC", 3, -3.0)), 3.0)
        self.assertEqual(self.m.board_spread(self.event("X @ Y", 3, 7.5)), 7.5)

    def test_a_game_with_no_spread_is_none_not_zero(self):
        # Zero would rank an unpriced game as the most competitive on the
        # board and spend the first fetch on it.
        self.assertIsNone(self.m.board_spread(self.event("X @ Y", 3)))
        self.assertIsNone(self.m.board_spread({"competitions": [{}]}))
        self.assertIsNone(self.m.board_spread({}))

    def test_a_spread_that_is_not_a_number_is_none(self):
        e = self.event("X @ Y", 3)
        e["competitions"][0]["odds"] = [{"spread": "EVEN"}]
        self.assertIsNone(self.m.board_spread(e))

    # --- which games get a fetch ---------------------------------------------

    def test_the_closest_game_is_fetched_before_the_blowout(self):
        board = [self.event("BLOWOUT", 3, -45.5),
                 self.event("CLOSE", 4, -3.0),
                 self.event("MIDDLING", 5, -14.0)]
        order = [e["shortName"] for e in self.m.pick_for_context(board)]
        self.assertEqual(order, ["CLOSE", "MIDDLING", "BLOWOUT"])

    def test_games_outside_the_window_go_last_however_close_they_are(self):
        # A pick-em three days out cannot be bet on information that does not
        # exist yet, and a fetch spent on it is one a playable game did not get.
        board = [self.event("TOMORROW_PICKEM", 40, -1.0),
                 self.event("TONIGHT_BLOWOUT", 2, -38.0)]
        order = [e["shortName"] for e in self.m.pick_for_context(board)]
        self.assertEqual(order, ["TONIGHT_BLOWOUT", "TOMORROW_PICKEM"])

    def test_a_game_already_started_is_not_picked_first(self):
        board = [self.event("STARTED", -2, -2.0), self.event("UPCOMING", 3, -20.0)]
        order = [e["shortName"] for e in self.m.pick_for_context(board)]
        self.assertEqual(order[0], "UPCOMING")

    def test_an_unpriced_game_is_kept_but_last_within_the_window(self):
        # Unknown is not the same as lopsided, so it is not dropped - it just
        # does not outrank a game whose price we can see.
        board = [self.event("NOPRICE", 3), self.event("PRICED", 4, -30.0)]
        order = [e["shortName"] for e in self.m.pick_for_context(board)]
        self.assertEqual(order, ["PRICED", "NOPRICE"])

    def test_nothing_is_lost_only_reordered(self):
        # The cap drops games; this function must not drop any itself, or the
        # two reductions become impossible to tell apart.
        board = [self.event(f"g{i}", i, -float(i)) for i in range(1, 12)]
        board.append(self.event("nospread", 2))
        self.assertEqual(len(self.m.pick_for_context(board)), len(board))


class ACapIsNotACapUntilSomethingChecksIt(unittest.TestCase):
    """"Max 2 open bets" lived in Ace's header and nowhere else - a sentence
    the agent was asked to keep, with nothing able to tell whether it had. The
    number is a constant now, `bet` refuses past it, the verifier reports a
    bankroll edited past it by hand, and this fails the build if the header
    stops quoting the same figure.

    Same shape as MIN_REPORT_WORDS: a header is prose and cannot import a
    constant, so the test is the only thing holding them together.
    """

    def setUp(self):
        self.m = load("ace_judge", "ace-judge.py")
        self.header = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()

    def test_the_header_quotes_the_real_cap(self):
        m = re.search(r"Max (\d+) open bets", self.header)
        self.assertIsNotNone(m, "Ace is capped but never told the number")
        self.assertEqual(int(m.group(1)), self.m.MAX_OPEN_BETS)

    def test_the_verifier_imports_the_cap_rather_than_restating_it(self):
        src = (SCRIPTS / "ace-verify.py").read_text()
        self.assertIn("ace_judge.MAX_OPEN_BETS", src)
        self.assertNotRegex(src, r"open_n > \d",
                            "ace-verify has its own copy of the cap again")

    def test_counting_open_bets_survives_a_missing_or_broken_file(self):
        # It is read at the moment a bet is recorded, and a crash there would
        # cost a cycle over a file that simply is not there yet.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agents" / "ace" / "state").mkdir(parents=True)
            self.m.AGENT = root / "agents" / "ace"
            self.assertEqual(self.m.open_bet_count(), 0)
            (root / "agents/ace/state/bankroll.json").write_text("{ not json")
            self.assertEqual(self.m.open_bet_count(), 0)
            (root / "agents/ace/state/bankroll.json").write_text(
                json.dumps({"open_bets": [{"stake": 150}] * 3}))
            self.assertEqual(self.m.open_bet_count(), 3)

    def test_bet_refuses_once_the_cap_is_reached(self):
        src = (SCRIPTS / "ace-judge.py").read_text()
        body = src.split("def cmd_bet(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("open_bet_count()", body)
        self.assertIn("MAX_OPEN_BETS", body)


class AWeekOfPassesShouldSayHowClose(unittest.TestCase):
    """Ace has not placed a bet in a week, and "is the 8-point bar too hard"
    could not be answered - because nothing was recorded. --my-pct was
    optional, a cycle swept all sixteen rows with one shared reason, and every
    passed row carried edge_pts: null.

    A pass naming one or two games is a game Ace studied, so it must say what
    it estimated. A sweep of the whole board is exempt: one number cannot be an
    estimate for sixteen different games, and pretending it is would be worse
    than recording nothing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agents/ace/state").mkdir(parents=True)
        (self.root / "agents/ace/data").mkdir(parents=True)
        self.m = load("ace_judge", "ace-judge.py")
        self.m.ROOT = self.root
        self.m.AGENT = self.root / "agents" / "ace"
        self.m.CANDIDATES = self.m.AGENT / "data" / "candidates.json"
        self.m.LEDGER = self.m.AGENT / "state" / "ledger.json"
        self.m.CANDIDATES.write_text(json.dumps({
            "day": "2026-09-19", "slot": "afternoon",
            "candidates": [{"selection": f"T{i} ML", "match": f"A@B{i}",
                            "price": -150, "novig_pct": 57.0, "sport": "cfb"}
                           for i in range(16)]}))

    def tearDown(self):
        self.tmp.cleanup()

    def judge(self, number, why="no edge", my_pct=None, status="passed"):
        class A:
            pass
        a = A()
        a.number, a.why, a.my_pct, a.stake = number, why, my_pct, None
        return self.m.record(a, status)

    def test_passing_one_studied_game_without_an_estimate_is_refused(self):
        self.assertEqual(self.judge("1"), 1)
        self.assertFalse(self.m.LEDGER.exists(), "nothing should have been written")

    def test_passing_it_with_an_estimate_records_the_gap(self):
        self.assertEqual(self.judge("1", my_pct=54.5), 0)
        row = json.loads(self.m.LEDGER.read_text())["candidates"][0]
        self.assertEqual(row["my_pct"], 54.5)
        self.assertEqual(row["edge_pts"], -2.5)

    def test_a_sweep_of_the_whole_board_needs_no_estimate(self):
        # One number cannot be an estimate for sixteen games.
        self.assertEqual(self.judge("1-16", why="nothing cleared 8 points"), 0)
        self.assertEqual(len(json.loads(self.m.LEDGER.read_text())["candidates"]), 16)

    def test_the_boundary_is_where_the_constant_says(self):
        upto = self.m.ESTIMATE_REQUIRED_UPTO
        self.assertEqual(self.judge(",".join(str(n) for n in range(1, upto + 1))), 1)
        self.assertEqual(self.judge(",".join(str(n) for n in range(1, upto + 2))), 0)

    def test_a_bet_needs_an_estimate_of_its_own(self):
        # The bar IS the gap between the estimate and the no-vig line, so a
        # bet without one claims 8+ points with nothing on record to check.
        class A:
            pass
        a = A()
        a.number, a.why, a.my_pct, a.stake = "1", "SP scratched", None, 150.0
        self.assertEqual(self.m.record(a, "bet"), 1)
        a.my_pct = 71.0
        self.assertEqual(self.m.record(a, "bet"), 0)
        row = json.loads(self.m.LEDGER.read_text())["candidates"][0]
        self.assertEqual(row["edge_pts"], 14.0)

    def test_a_bet_is_refused_for_bet_reasons_not_pass_reasons(self):
        # Guarding the pass rule on status is what keeps the advice correct.
        # Told to "pass it with --my-pct", an agent holding a real edge would
        # do the one thing it should not - and being given the wrong next step
        # is a failure this repo has already paid for twice today.
        import contextlib, io
        class A:
            pass
        a = A()
        a.number, a.why, a.my_pct, a.stake = "1", "SP scratched", None, 150.0
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.m.record(a, "bet")
        message = err.getvalue()
        self.assertIn("--my-pct", message)
        self.assertNotIn("ace-judge.py pass", message,
                         "a bet must not be told to pass the game instead")

    def test_the_header_says_to_give_an_estimate(self):
        # Refusing for a rule the instructions never state is the failure this
        # repo keeps repeating.
        header = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()
        self.assertIn("--my-pct", header)
        # Asserted on intent rather than a sentence: the header must tell Ace
        # that estimates are owed and that it is failed without them. Pinning
        # the exact wording broke this test the first time the paragraph was
        # rewritten, which says nothing about whether Ace was told.
        self.assertIn("studied", header)
        self.assertRegex(header, r"verifier fails a cycle")


class RuleTwoNeedsSomethingToPointAt(unittest.TestCase):
    """Ace's own bar asks for "real information the market has not priced yet -
    a just-announced injury, a scratched starter". It could not be satisfied:
    injuries lived inside the per-game context files, Ace is told to open three
    or four of them, so it could not scan a board for news and could not tell a
    report filed an hour ago from one filed eleven days ago.

    On a real NFL board the ages ran 0h, 2h, 3h and 4h alongside entries 264h
    and 449h old, so the distinction is there to be made. Every timestamp in
    these tests is one that actually appeared.
    """

    def setUp(self):
        self.m = load("ace_fetch", "ace-fetch.py")
        self.now = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)

    def ctx(self, *injuries):
        return {"injuries": [dict(i) for i in injuries]}

    def inj(self, player, hours_ago, team="PIT", status="Out", position="CB"):
        when = self.now - timedelta(hours=hours_ago)
        return {"team": team, "player": player, "position": position,
                "status": status, "date": when.strftime("%Y-%m-%dT%H:%M") + "Z"}

    def test_a_report_from_three_hours_ago_is_fresh(self):
        # Joey Porter Jr., Out, 3h - exactly the case rule 2 describes.
        got = self.m.fresh_injuries(self.ctx(self.inj("Joey Porter Jr.", 3)), self.now)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["player"], "Joey Porter Jr.")
        self.assertAlmostEqual(got[0]["hours_old"], 3.0, places=1)

    def test_a_report_from_eleven_days_ago_is_not(self):
        # Kyler Gordon, Out, 261h. Real, and not news.
        self.assertEqual(
            self.m.fresh_injuries(self.ctx(self.inj("Kyler Gordon", 261)), self.now), [])

    def test_the_window_boundary_is_where_the_constant_says(self):
        w = self.m.FRESH_INJURY_HOURS
        self.assertEqual(len(self.m.fresh_injuries(
            self.ctx(self.inj("inside", w - 0.1)), self.now)), 1)
        self.assertEqual(self.m.fresh_injuries(
            self.ctx(self.inj("outside", w + 0.1)), self.now), [])

    def test_an_undated_injury_is_not_treated_as_fresh(self):
        # Unknown is not recent. Guessing the other way puts an entry of
        # unknown age at the top of the list Ace is told to trust.
        bad = self.inj("no date", 1)
        bad["date"] = None
        self.assertEqual(self.m.fresh_injuries(self.ctx(bad), self.now), [])
        del bad["date"]
        self.assertEqual(self.m.fresh_injuries(self.ctx(bad), self.now), [])

    def test_a_date_that_will_not_parse_is_not_fresh_either(self):
        bad = self.inj("garbage", 1)
        bad["date"] = "last Tuesday"
        self.assertEqual(self.m.fresh_injuries(self.ctx(bad), self.now), [])

    def test_a_report_dated_in_the_future_is_a_clock_problem_not_news(self):
        ahead = self.inj("tomorrow", -4)
        self.assertEqual(self.m.fresh_injuries(self.ctx(ahead), self.now), [])

    def test_the_newest_comes_first(self):
        got = self.m.fresh_injuries(self.ctx(
            self.inj("four", 4), self.inj("half", 0.5), self.inj("two", 2)), self.now)
        self.assertEqual([i["player"] for i in got], ["half", "two", "four"])

    def test_both_teams_news_is_carried(self):
        # A side is priced against its opponent, so the opponent losing a
        # starter is as much a reason to look as your own team losing one.
        got = self.m.fresh_injuries(self.ctx(
            self.inj("theirs", 1, team="NE"), self.inj("ours", 2, team="PIT")), self.now)
        self.assertEqual({i["team"] for i in got}, {"NE", "PIT"})

    def test_the_list_is_capped(self):
        many = [self.inj(f"p{i}", i * 0.1) for i in range(20)]
        got = self.m.fresh_injuries(self.ctx(*many), self.now)
        self.assertEqual(len(got), self.m.MAX_FRESH_INJURIES)

    def test_no_injuries_at_all_is_an_empty_list_not_a_crash(self):
        self.assertEqual(self.m.fresh_injuries({}, self.now), [])
        self.assertEqual(self.m.fresh_injuries(None, self.now), [])
        self.assertEqual(self.m.fresh_injuries({"injuries": None}, self.now), [])


class EveryCandidateSaysWhetherThereIsNews(unittest.TestCase):
    """The list is only useful if it reaches the file Ace actually reads.
    `candidates.json` carries it per row, and an empty list is a real answer -
    no news on this game, so nothing here can be the unpriced information the
    bar asks for.
    """

    def setUp(self):
        self.m = load("ace_fetch", "ace-fetch.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def game(self, name, hours_out, injuries=()):
        start = datetime.now(timezone.utc) + timedelta(hours=hours_out)
        (self.ctx / f"nfl-{name}.json").write_text(json.dumps({
            "sport": "nfl", "short": name, "match": name,
            "start_utc": start.strftime("%Y-%m-%dT%H:%MZ"),
            "status": "STATUS_SCHEDULED",
            "home": {"abbr": "HOM"}, "away": {"abbr": "AWY"},
            "odds": {"moneyline_home": -150, "moneyline_away": 130,
                     "novig_home_pct": 58.0, "novig_away_pct": 42.0},
            "injuries": list(injuries),
        }))

    def slate(self):
        return self.m.build_ledger("2026-09-19", self.ctx, "afternoon")

    def recent(self, player, hours_ago):
        when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return {"team": "HOM", "player": player, "position": "QB",
                "status": "Out", "date": when.strftime("%Y-%m-%dT%H:%MZ")}

    def test_a_game_with_news_carries_it_on_both_of_its_rows(self):
        self.game("NEWS", 4, [self.recent("Joey Porter Jr.", 3)])
        rows = [r for r in self.slate()["candidates"] if r["match"] == "NEWS"]
        self.assertEqual(len(rows), 2, "home and away")
        for r in rows:
            self.assertEqual(len(r["fresh_injuries"]), 1)
            self.assertEqual(r["fresh_injuries"][0]["player"], "Joey Porter Jr.")

    def test_a_quiet_game_says_so_rather_than_leaving_the_field_out(self):
        # Missing and empty read differently. Ace is told an empty list means
        # there is nothing here, which only works if the key is always present.
        self.game("QUIET", 4)
        for r in self.slate()["candidates"]:
            self.assertIn("fresh_injuries", r)
            self.assertEqual(r["fresh_injuries"], [])

    def test_the_slate_counts_how_many_games_have_news(self):
        # 2 of 8 on the board this was built against.
        self.game("NEWS", 4, [self.recent("a", 1)])
        self.game("ALSO", 5, [self.recent("b", 2)])
        self.game("QUIET", 6)
        out = self.slate()
        self.assertEqual(out["games_with_news"], 2)
        self.assertEqual(out["games_shown"], 3)

    def test_stale_news_does_not_count_as_news(self):
        self.game("OLD", 4, [self.recent("Kyler Gordon", 261)])
        out = self.slate()
        self.assertEqual(out["games_with_news"], 0)
        self.assertEqual(out["candidates"][0]["fresh_injuries"], [])

    def test_the_window_is_published_so_the_number_can_be_checked(self):
        self.game("X", 4)
        self.assertEqual(self.slate()["fresh_injury_hours"], self.m.FRESH_INJURY_HOURS)


class WhatAPropsFeedWouldCost(unittest.TestCase):
    """Player props are the one thing ESPN's free endpoint does not carry, so
    they mean a paid feed. The Odds API sells credits, not requests, and props
    must be fetched one event at a time - one credit per market returned per
    region, per game, every time you look.

    That multiplies out fast enough that the plan you need depends entirely on
    how often you poll and how many markets you carry, which is a calculation,
    not a price. The plans below were read from the-odds-api.com on
    2026-09-19.
    """

    def setUp(self):
        self.m = load("cost_estimate", "cost-estimate.py")

    def test_the_arithmetic_is_the_arithmetic(self):
        #   8 games x 5 markets x 1 region          = 40 per fetch
        #   x 29 fetches                            = 1,160 a day
        #   x 3 days a week x 4.33 weeks            = 15,068 a month
        use = self.m.feed_credits(8, 5, 1, 29, 3)
        self.assertEqual(use["per_fetch"], 40)
        self.assertEqual(use["per_day"], 1160)
        self.assertEqual(use["per_month"], 15068)

    def test_polling_twice_as_often_costs_twice_as_much(self):
        # Unlike a model cycle, this one really is linear - there is no
        # transcript growing underneath it.
        half = self.m.feed_credits(16, 5, 1, 14, 3)["per_month"]
        full = self.m.feed_credits(16, 5, 1, 28, 3)["per_month"]
        self.assertEqual(full, half * 2)

    def test_the_cheapest_plan_that_fits_is_the_one_returned(self):
        name, price, quota = self.m.plan_for(15_068)
        self.assertEqual(name, "20K")
        self.assertEqual(price, 30.00)
        self.assertGreaterEqual(quota, 15_068)

    def test_a_plan_exactly_at_quota_still_counts_as_fitting(self):
        self.assertEqual(self.m.plan_for(20_000)[0], "20K")
        self.assertEqual(self.m.plan_for(20_001)[0], "100K")

    def test_nothing_covering_it_is_said_rather_than_rounded_up(self):
        # Quoting the biggest plan for a load it does not cover would be
        # inventing a price, and the honest answer is "look less often".
        biggest = max(q for _n, _p, q in self.m.ODDS_API_PLANS)
        self.assertIsNone(self.m.plan_for(biggest + 1))

    def test_the_free_tier_is_not_offered_for_a_real_workload(self):
        # 500 credits is twelve fetches of one game. It exists to try the API.
        self.assertEqual(self.m.plan_for(1)[0], "free")
        self.assertEqual(self.m.plan_for(501)[0], "20K")

    def test_the_plans_are_in_ascending_order(self):
        # plan_for returns the first that fits, so an unsorted table would
        # quote the wrong price without failing anything.
        quotas = [q for _n, _p, q in self.m.ODDS_API_PLANS]
        prices = [p for _n, p, _q in self.m.ODDS_API_PLANS]
        self.assertEqual(quotas, sorted(quotas))
        self.assertEqual(prices, sorted(prices))

    def test_the_plans_carry_the_date_they_were_read(self):
        src = (SCRIPTS / "cost-estimate.py").read_text()
        preamble = src.split("ODDS_API_PLANS = [", 1)[0].splitlines()[-12:]
        self.assertRegex("\n".join(preamble), r"read 20\d\d-\d\d-\d\d")

    def test_the_printed_answer_says_props_are_per_event(self):
        # The whole reason the number is large. A reader who misses it will
        # assume one call covers the slate.
        r = subprocess.run([sys.executable, str(SCRIPTS / "cost-estimate.py"),
                            "--feed", "--games", "8"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("per-event", r.stdout)
        self.assertIn("2026-09-19", r.stdout)
        self.assertIn("cheapest that fits", r.stdout)


class ACycleOwesEstimates(unittest.TestCase):
    """Requiring an estimate on a pass that names one or two games was not
    enough: a cycle can sweep all sixteen rows with `rest` and one shared
    reason, record nothing, and pass. That is exactly what happened - "passed
    1-16, no edge on the no-vig line" - and it is what a week of unanswerable
    "no bets" is made of.

    Nothing can see which context files were opened, so "estimate the ones you
    studied" is unenforceable as written. A count is not: a cycle owes
    MIN_ESTIMATES, Ace chooses which, and the choosing is what makes them the
    studied ones.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agents/ace/state").mkdir(parents=True)
        (self.root / "agents/ace/data").mkdir(parents=True)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.av = load("ace_verify", "ace-verify.py")
        self.cands = [{"selection": f"T{i} ML", "match": f"A@B{i}", "price": -150,
                       "novig_pct": 57.0, "sport": "cfb"} for i in range(16)]
        self.slate(self.cands)

    def tearDown(self):
        os.environ.pop("ECOSYSTEM_ROOT", None)
        self.tmp.cleanup()

    def slate(self, rows):
        (self.root / "agents/ace/data/candidates.json").write_text(json.dumps(
            {"day": "2026-09-19", "slot": "afternoon", "candidates": rows}))

    def judge(self, n_rows, n_estimates):
        rows = [dict(c, status="passed", why_not=["no edge"])
                for c in self.cands[:n_rows]]
        for i in range(n_estimates):
            rows[i]["my_pct"] = 54.0 + i
        (self.root / "agents/ace/state/ledger.json").write_text(json.dumps(
            {"day": "2026-09-19", "slot": "afternoon", "verdict": "No picks.",
             "candidates": rows}))
        problems, notes = [], []
        self.av.check_ledger(problems, notes, None)
        return [p for p in problems if "my-pct" in p]

    def test_the_sweep_that_started_this_now_fails(self):
        # 16 rows judged, 0 estimates - today's cycle, verbatim.
        self.assertTrue(self.judge(16, 0))

    def test_one_short_is_still_short(self):
        want = load("ace_judge", "ace-judge.py").MIN_ESTIMATES
        self.assertTrue(self.judge(16, want - 1))

    def test_meeting_the_count_passes(self):
        want = load("ace_judge", "ace-judge.py").MIN_ESTIMATES
        self.assertEqual(self.judge(16, want), [])

    def test_more_than_asked_for_is_fine(self):
        self.assertEqual(self.judge(16, 9), [])

    def test_a_slate_smaller_than_the_minimum_asks_only_for_what_is_there(self):
        # Failing a cycle for not estimating games that were never offered is
        # the empty-slate mistake this repo already made once.
        self.slate(self.cands[:2])
        self.assertEqual(self.judge(2, 2), [])

    def test_a_one_game_slate_still_owes_that_one(self):
        self.slate(self.cands[:1])
        self.assertTrue(self.judge(1, 0))
        self.assertEqual(self.judge(1, 1), [])

    def test_it_is_a_problem_not_a_note(self):
        # A rule that only warns is the rule that was there before.
        rows = [dict(c, status="passed", why_not=["no edge"]) for c in self.cands]
        (self.root / "agents/ace/state/ledger.json").write_text(json.dumps(
            {"day": "2026-09-19", "slot": "afternoon", "verdict": "x",
             "candidates": rows}))
        problems, notes = [], []
        self.av.check_ledger(problems, notes, None)
        self.assertTrue([p for p in problems if "my-pct" in p])
        self.assertFalse([n for n in notes if "my-pct" in n])

    def test_a_string_estimate_does_not_count(self):
        # Only a number can be subtracted from the no-vig line.
        rows = [dict(c, status="passed", why_not=["no edge"]) for c in self.cands]
        for i in range(5):
            rows[i]["my_pct"] = "54.0"
        (self.root / "agents/ace/state/ledger.json").write_text(json.dumps(
            {"day": "2026-09-19", "slot": "afternoon", "verdict": "x",
             "candidates": rows}))
        problems, notes = [], []
        self.av.check_ledger(problems, notes, None)
        self.assertTrue([p for p in problems if "my-pct" in p])

    def test_the_header_states_the_number_the_verifier_enforces(self):
        # The failure this repo keeps repeating: a rule in the verifier and
        # not in the instructions.
        header = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()
        m = re.search(r"`--my-pct` for at least (\d+) games", header)
        self.assertIsNotNone(m, "Ace is failed for this but never told the count")
        self.assertEqual(int(m.group(1)),
                         load("ace_judge", "ace-judge.py").MIN_ESTIMATES)

    def test_the_verifier_does_not_keep_its_own_copy_of_the_number(self):
        src = (SCRIPTS / "ace-verify.py").read_text()
        self.assertIn("ace_judge.MIN_ESTIMATES", src)


class StrayMarksBesideTheArt(unittest.TestCase):
    """A compass emblem reached a hoodie with two pen-stroke scratches beside
    it. knockout floods the background inward from the border, so it removes
    what is background-coloured AND connected to the edge - a dark mark the
    model drew in the middle of the canvas is neither, and it survived all the
    way onto the garment.

    Removing it is a second pass over what the flood left, and it obeys the
    same rule as the rest of this script: refuse rather than guess. A design
    that is genuinely several parts has no speck to find, and deleting its
    smaller half would be worse than the scratch.
    """

    def setUp(self):
        self.k = load("knockout", "knockout.py")

    def canvas(self, blocks, size=120):
        px = bytearray()
        for _ in range(size * size):
            px += bytes((245, 245, 240, 255))
        for (x0, y0, x1, y1) in blocks:
            for y in range(y0, y1):
                for x in range(x0, x1):
                    i = (y * size + x) * 4
                    px[i:i + 3] = bytes((30, 60, 45))
        self.k.knockout(size, size, px)
        return size, size, px

    def sizes(self, w, h, px):
        return [s for s, _ in self.k.components(w, h, px)]

    def test_a_speck_beside_the_emblem_is_removed(self):
        w, h, px = self.canvas([(40, 40, 90, 90), (12, 12, 16, 16)])
        self.assertEqual(self.sizes(w, h, px), [2500, 16])
        wiped, parts, why = self.k.despeckle(w, h, px)
        self.assertEqual(parts, 1)
        self.assertEqual(wiped, 16)
        self.assertEqual(self.sizes(w, h, px), [2500])
        self.assertIn("stray mark", why)

    def test_a_two_part_design_is_left_alone(self):
        # An icon over a word. Neither half is a speck, and picking one to
        # delete would be vandalism dressed as a fix.
        w, h, px = self.canvas([(30, 20, 90, 60), (28, 70, 92, 88)])
        before = self.sizes(w, h, px)
        wiped, parts, why = self.k.despeckle(w, h, px)
        self.assertEqual((wiped, parts), (0, 0))
        self.assertEqual(self.sizes(w, h, px), before)
        self.assertIn("multi-part", why)

    def test_a_single_piece_says_so_rather_than_claiming_a_clean_up(self):
        # "Nothing removed" has two causes and the caller prints which.
        w, h, px = self.canvas([(40, 40, 90, 90)])
        wiped, parts, why = self.k.despeckle(w, h, px)
        self.assertEqual((wiped, parts), (0, 0))
        self.assertIn("one connected piece", why)

    def test_the_threshold_is_where_the_constant_says(self):
        # A piece just under the limit goes, one just over stays.
        w, h, px = self.canvas([(20, 20, 100, 100)])          # 6400 px
        big = self.sizes(w, h, px)[0]
        small = int(big * self.k.SPECK_MAX_PCT / 100.0) - 20
        side = max(2, int(small ** 0.5))
        w, h, px = self.canvas([(20, 20, 100, 100), (5, 5, 5 + side, 5 + side)])
        wiped, parts, _why = self.k.despeckle(w, h, px)
        self.assertEqual(parts, 1, "a piece under the threshold is a speck")

    def test_a_piece_over_the_threshold_survives(self):
        w, h, px = self.canvas([(20, 20, 100, 100), (2, 2, 30, 30)])
        wiped, parts, _why = self.k.despeckle(w, h, px)
        self.assertEqual((wiped, parts), (0, 0))

    def test_several_specks_all_go(self):
        w, h, px = self.canvas([(40, 40, 95, 95), (5, 5, 8, 8),
                                (110, 5, 113, 8), (5, 110, 8, 113)])
        wiped, parts, _why = self.k.despeckle(w, h, px)
        self.assertEqual(parts, 3)
        self.assertEqual(len(self.sizes(w, h, px)), 1)

    def test_pieces_touching_only_at_a_corner_are_two_pieces(self):
        # Four-connected, same as the flood. Treating a diagonal touch as a
        # join would merge a speck into the art it sits beside and hide it.
        w, h, px = self.canvas([(20, 20, 40, 40), (40, 40, 44, 44)])
        self.assertEqual(len(self.sizes(w, h, px)), 2)

    def test_an_empty_image_does_not_crash(self):
        px = bytearray(bytes((245, 245, 240, 255)) * (20 * 20))
        self.k.knockout(20, 20, px)
        self.assertEqual(self.k.despeckle(20, 20, px)[:2], (0, 0))

    def test_the_script_reports_what_it_did(self):
        # Silent cleanup is how you stop noticing it is happening.
        src = (SCRIPTS / "knockout.py").read_text()
        self.assertIn("despeckle:", src)

    def test_keep_specks_really_keeps_them(self):
        # Asserting the flag's name appears in the file proves only that the
        # usage line mentions it. This runs the script both ways.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            size = 120
            px = bytearray()
            for _ in range(size * size):
                px += bytes((245, 245, 240, 255))
            for (x0, y0, x1, y1) in ((40, 40, 95, 95), (8, 8, 12, 12)):
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        i = (y * size + x) * 4
                        px[i:i + 3] = bytes((30, 60, 45))
            self.k.encode(src, size, size, px)

            def run(*flags):
                out = Path(tmp) / f"out{len(flags)}.png"
                r = subprocess.run(
                    [sys.executable, str(SCRIPTS / "knockout.py"), str(src),
                     str(out), *flags],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr)
                w, h, got = self.k.decode(out)
                return r.stdout, len(self.k.components(w, h, got))

            cleaned_out, cleaned_parts = run()
            kept_out, kept_parts = run("--keep-specks")

        self.assertEqual(cleaned_parts, 1, "the speck should be gone by default")
        self.assertIn("stray mark", cleaned_out)
        self.assertEqual(kept_parts, 2, "--keep-specks must leave it alone")
        self.assertNotIn("despeckle:", kept_out)

    def test_despeckling_happens_only_after_the_refusals(self):
        # Cleaning a file that will not be written is work for nothing, and
        # measuring specks against art whose background was never found is
        # measuring against noise.
        #
        # This used to assert that "MAX_REMOVED_PCT" appeared before
        # "despeckle(" inside main()'s source. The refusals then moved into
        # verdict() so knockout could be asked without writing, and the test
        # broke on a rename rather than on a behaviour - it was never really
        # watching the ordering. Now it runs the thing: on a file that gets
        # refused, no despeckle line is printed at all.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src, dst = Path(tmp.name) / "gradient.png", Path(tmp.name) / "out.png"
        w, h = 80, 60
        px = bytearray()
        for y in range(h):
            for x in range(w):
                px += bytes((x * 3 % 256, y * 4 % 256, (x + y) % 256)) + b"\xff"
        self.k.encode(src, w, h, px)
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(src), str(dst)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, "a gradient has no background to remove")
        self.assertNotIn("despeckle:", r.stdout)
        self.assertFalse(dst.exists(), "nothing should have been written")


class ForecastsAreGradedOrTheyAreDecoration(unittest.TestCase):
    """A player-prop forecaster is easy to build and easy to fool yourself
    with: ten thousand draws of a weak distribution is still weak, and the
    precision is cosmetic. Two different running backs came back at 27.5% and
    27.4% in the tool this was modelled on, both flagged "limited role data" -
    a generic prior wearing a specific number.

    So the grading was built at the same time as the forecasting, not after.
    Every value below comes from a real ESPN game log.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.m.STORE = Path(self.tmp.name) / "forecasts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_forecast_stays_anchored_to_what_he_actually_did(self):
        # Five games, two of them over 60. Smoothing fills the gaps between
        # his games; it must not drag the answer away from them.
        probs = self.m.smoothed_probs([10.0, 20.0, 65.0, 80.0, 30.0], (60,))
        self.assertAlmostEqual(probs[60], 40.0, delta=12.0)

    def test_the_same_games_give_the_same_forecast(self):
        # A number nobody can reproduce cannot be audited after the game. It
        # used to need a seed for that; the percentage is exact arithmetic
        # now, so it is reproducible without one.
        sample = [12.0, 45.0, 77.0, 31.0, 66.0, 9.0, 52.0, 88.0]
        bars = self.m.MARKETS["receiving_yards"]["thresholds"]
        self.assertEqual(self.m.smoothed_probs(sample, bars),
                         self.m.smoothed_probs(sample, bars))

    def test_the_interval_is_reproducible_from_its_seed(self):
        sample = [12.0, 45.0, 77.0, 31.0, 66.0, 9.0, 52.0, 88.0]
        bars = self.m.MARKETS["receiving_yards"]["thresholds"]
        self.assertEqual(self.m.interval(sample, bars, seed="g1-p2"),
                         self.m.interval(sample, bars, seed="g1-p2"))
        self.assertNotEqual(self.m.interval(sample, bars, seed="g1-p2"),
                            self.m.interval(sample, bars, seed="g1-p9"))

    def test_an_empty_sample_forecasts_nothing(self):
        self.assertEqual(self.m.smoothed_probs([], (40, 50)), {})
        self.assertEqual(self.m.interval([], (40, 50)), {})

    def test_a_thin_sample_is_refused_by_the_minimum(self):
        # Week 3 gives two games. A confident number off two games is the
        # failure being copied, not the feature.
        self.assertGreaterEqual(self.m.MIN_GAMES, 8)

    def test_probabilities_fall_as_the_bar_rises(self):
        sample = [5.0, 18.0, 33.0, 44.0, 55.0, 61.0, 72.0, 90.0, 101.0]
        probs = self.m.smoothed_probs(
            sample, self.m.MARKETS["receiving_yards"]["thresholds"])
        ordered = [probs[t] for t in sorted(probs)]
        self.assertEqual(ordered, sorted(ordered, reverse=True))

    # --- grading -----------------------------------------------------------

    def forecast_row(self, probs, actual=None):
        return {"event_id": "1", "athlete_id": "2", "player": "A Receiver",
                "probabilities": probs, "actual": actual,
                "market": "receiving_yards", "graded_utc": None}

    def test_calibration_compares_what_was_said_to_what_happened(self):
        # Ten calls at ~70%, seven of which landed. That is a forecast telling
        # the truth, and the only test of one that matters.
        rows = []
        for i in range(10):
            rows.append(self.forecast_row({"60": 70.0}, actual=80.0 if i < 7 else 10.0))
        b = self.m.buckets(rows)
        self.assertEqual(b[70]["n"], 10)
        self.assertEqual(b[70]["observed"], 70.0)
        self.assertEqual(b[70]["gap"], 0.0)

    def test_an_overconfident_forecaster_shows_a_negative_gap(self):
        # Said 90%, happened 30%. This is the direction that costs money.
        rows = [self.forecast_row({"60": 90.0}, actual=80.0 if i < 3 else 10.0)
                for i in range(10)]
        b = self.m.buckets(rows)
        self.assertLess(b[90]["gap"], 0)

    def test_ungraded_forecasts_are_not_counted_as_anything(self):
        # Counting a pending forecast as a miss would make every new week look
        # like a failure.
        rows = [self.forecast_row({"60": 70.0}, actual=None) for _ in range(5)]
        self.assertEqual(self.m.buckets(rows), {})

    def test_every_threshold_of_a_graded_forecast_is_scored(self):
        rows = [self.forecast_row({"40": 80.0, "70": 30.0}, actual=55.0)]
        b = self.m.buckets(rows)
        self.assertEqual(b[80]["hits"], 1, "55 clears 40")
        self.assertEqual(b[30]["hits"], 0, "55 does not clear 70")

    def test_a_certainty_does_not_open_a_bucket_above_one_hundred(self):
        # A real run printed a "100-109%" row, which is not a thing a
        # probability can be. 100% belongs in the top bucket with 90-99.
        rows = [self.forecast_row({"3": 100.0}, actual=5.0)]
        self.assertEqual(list(self.m.buckets(rows)), [90])

    def test_it_says_when_there_is_not_enough_to_conclude(self):
        # A coin lands 7 of 10 often enough that it means nothing, and a
        # calibration table printed without that warning invites a conclusion.
        src = (SCRIPTS / "props-forecast.py").read_text()
        self.assertIn("not enough to conclude", src)

    # --- what it refuses to claim ------------------------------------------

    def test_it_holds_no_odds_and_says_it_is_not_a_bet(self):
        # The trap written into Ace's own header: comparing a number you
        # computed to a line you did not is not an edge. There is no price in
        # this file to compare against.
        src = (SCRIPTS / "props-forecast.py").read_text()
        self.assertIn("NOT A BET", self.m.NOT_A_BET)
        self.assertIn("print(NOT_A_BET)", src,
                      "the disclaimer has to be printed, not just documented")
        for word in ("moneyline", "novig", "stake", "bankroll"):
            self.assertNotIn(word, src.lower().replace("no odds", ""),
                             f"{word} does not belong in a forecaster")

    def test_identical_thresholds_are_reported_as_coarse(self):
        # Tutu Atwell came back 17.7% at every threshold off a real game log,
        # because he has no game between 40 and 70 yards.
        bars = (40, 50, 60, 70)
        self.assertTrue(self.m.is_coarse({40: 17.7, 50: 17.7, 60: 17.7, 70: 17.7}, bars))
        self.assertTrue(self.m.is_coarse({40: 66.4, 50: 66.4, 60: 44.4, 70: 32.7}, bars))

    def test_a_forecast_with_resolution_is_not_flagged(self):
        self.assertFalse(self.m.is_coarse({40: 86.0, 50: 69.9, 60: 47.1, 70: 21.0},
                                          (40, 50, 60, 70)))

    def test_nothing_forecast_is_not_coarse(self):
        self.assertFalse(self.m.is_coarse({}, (40, 50, 60, 70)))

    def test_a_row_carries_the_coarse_flag_its_own_numbers_earn(self):
        # Asserted on the row, not on the source line that sets it: the
        # earlier version of this checked for the text "coarse = is_coarse("
        # and would have passed against a flag nothing ever read.
        flat = [20.0] * 12          # every game identical: nothing to spread
        spread = [5.0, 25.0, 45.0, 55.0, 65.0, 75.0, 85.0, 95.0, 105.0, 115.0]
        rows = self.m.rows_for_player(
            "1", "A @ B", None, "2", "A Receiver", "WR", "AAA",
            {"receiving_yards": spread, "receptions": flat}, ["2026"], None)
        by = {r["market"]: r for r in rows}
        self.assertFalse(by["receiving_yards"]["coarse"])
        self.assertTrue(by["receptions"]["coarse"])


class TheForecastCardShowsNoPrice(unittest.TestCase):
    """The village renders forecasts as cards, shaped after a tool the owner
    was shown. The one thing they must never grow is a price beside the
    probability: "mine says 70, the line says 60" is the reasoning Ace's
    instructions open by forbidding, and a card putting the two side by side
    would be an invitation to it in the interface rather than the prompt.
    """

    def setUp(self):
        self.js = (ROOT / "mission-control-api" / "ace.js").read_text()
        self.html = (ROOT / "mission-control-api" / "public" / "village.html").read_text()

    def test_the_endpoint_serves_the_forecasts(self):
        self.assertIn("/api/ace/forecasts", self.js)
        self.assertIn("forecasts.json", self.js)

    def test_a_missing_file_is_a_reason_not_a_crash(self):
        # Nothing has been forecast yet is the normal state on a fresh
        # droplet, and it must not read as a broken endpoint.
        body = self.js.split("/api/ace/forecasts", 1)[1].split("app.get(", 1)[0]
        self.assertIn("available: false", body)
        self.assertIn("reason", body)

    def test_neither_the_endpoint_nor_the_card_carries_odds(self):
        card = self.html.split("function forecastCard(", 1)[1].split("\n/* -", 1)[0]
        feed = self.js.split("/api/ace/forecasts", 1)[1].split("app.get(", 1)[0]
        for word in ("price", "odds", "novig", "moneyline", "stake", "edge"):
            self.assertNotIn(word, card.lower(), f"a forecast card must not show {word}")
            self.assertNotIn(word, feed.lower().replace("no odds", ""),
                             f"the forecast feed must not carry {word}")

    def test_the_card_says_it_is_not_a_bet(self):
        card = self.html.split("function forecastCard(", 1)[1].split("\n/* -", 1)[0]
        self.assertIn("NOT A BET", card)

    def test_a_flat_forecast_is_marked_in_the_interface_too(self):
        # It is flagged in the script's output; a card that dropped the flag
        # would present the weakest rows as if they were the strongest. The
        # flag is FLAT now rather than COARSE - it used to fire on any two
        # bars with no game between them, which smoothing fixed, and it now
        # fires only on a player whose games are all the identical value.
        card = self.html.split("function forecastCard(", 1)[1].split("\n/* -", 1)[0]
        self.assertIn("f.coarse", card)
        self.assertIn("FLAT", card)

    def test_the_card_shows_how_firm_each_percentage_is(self):
        # Stored and not drawn is the same as not computed.
        card = self.html.split("function fcTile(", 1)[1].split("\nfunction forecastCard", 1)[0]
        self.assertIn("band[0]", card)
        self.assertIn("band[1]", card)
        body = self.html.split("function forecastCard(", 1)[1].split("\n/* -", 1)[0]
        self.assertIn("f.interval", body)

    def test_the_sample_is_shown_beside_the_number(self):
        # A probability off 11 games is not the same claim as one off 21, and
        # printing them identically is the failure being designed against.
        card = self.html.split("function forecastCard(", 1)[1].split("\n/* -", 1)[0]
        for field in ("sample_games", "sample_mean", "p25", "p75"):
            self.assertIn(field, card, f"the card must show {field}")

    def test_an_ungraded_forecast_is_not_drawn_as_a_result(self):
        # Showing a pending forecast as a miss would make every new slate look
        # like a failure before a ball was thrown.
        card = self.html.split("function forecastCard(", 1)[1].split("\n/* -", 1)[0]
        self.assertIn("awaiting the final score", card)
        self.assertIn("f.actual !== null", card)

    def test_the_panel_is_loaded_when_the_hall_is_entered(self):
        # Assert the CALL, not the name: `function openPropsPanel(){` contains
        # "openPropsPanel()" as a substring, so a version of this asserting
        # the bare name passed on the definition of a function nothing
        # invoked. It used to hang off Ace's door; it hangs off the hall's.
        self.assertIn('id="propsBody"', self.html)
        self.assertRegex(self.html,
                         r"name === PROPS_DOOR\) return openPropsPanel\(\);")
        self.assertRegex(self.html, r"loadProps\(\);\n\}")

    def test_the_label_does_not_wrap_mid_phrase(self):
        # It rendered as "MODEL FORECAST · NOT / A BET", which reads as two
        # separate claims. Found by screenshotting it rather than reading it.
        self.assertRegex(self.html, r"\.fclabel\{[^}]*white-space:nowrap")


class AStatIsFoundByNameNotByColumn(unittest.TestCase):
    """The forecaster read a game log by column number and got the wrong stat
    for most of the league.

    It took labels.index("YDS") - the first YDS column - with a comment saying
    that was the receiving one. It is, for a tight end. ESPN orders a game
    log's columns by what the player mostly does, so the arrays below (both
    captured from the live API) put receivingYards at index 2 for Kelce and at
    index 7 for Gibbs. Every running back in the file was being forecast on
    his rushing yards under a heading that said receiving, and nothing in the
    output could have shown it: the numbers looked entirely plausible.

    The box score then orders the same stats a third way - TGTS is second in
    the game log and last in the box score - so a fix that hard-coded the
    other order would have broken grading instead.

    Both payloads name their columns. That is what is read now, and this is
    the test that the names are the ones ESPN actually uses.
    """

    # Captured 2026-09-21 from site.web.api.espn.com, verbatim.
    TE_NAMES = ['receptions', 'receivingTargets', 'receivingYards',
                'yardsPerReception', 'receivingTouchdowns', 'longReception',
                'rushingAttempts', 'rushingYards', 'yardsPerRushAttempt',
                'longRushing', 'rushingTouchdowns', 'fumbles', 'fumblesLost',
                'fumblesForced', 'kicksBlocked']
    TE_LABELS = ['REC', 'TGTS', 'YDS', 'AVG', 'TD', 'LNG', 'CAR', 'YDS', 'AVG',
                 'LNG', 'TD', 'FUM', 'LST', 'FF', 'KB']
    RB_NAMES = ['rushingAttempts', 'rushingYards', 'yardsPerRushAttempt',
                'rushingTouchdowns', 'longRushing', 'receptions',
                'receivingTargets', 'receivingYards', 'yardsPerReception',
                'receivingTouchdowns', 'longReception', 'fumbles',
                'fumblesLost', 'fumblesForced', 'kicksBlocked']
    RB_LABELS = ['CAR', 'YDS', 'AVG', 'TD', 'LNG', 'REC', 'TGTS', 'YDS', 'AVG',
                 'TD', 'LNG', 'FUM', 'LST', 'FF', 'KB']
    QB_NAMES = ['completions', 'passingAttempts', 'passingYards',
                'completionPct', 'yardsPerPassAttempt', 'passingTouchdowns',
                'interceptions', 'longPassing', 'sacks', 'QBRating', 'adjQBR',
                'rushingAttempts', 'rushingYards', 'yardsPerRushAttempt',
                'rushingTouchdowns', 'longRushing']
    QB_LABELS = ['CMP', 'ATT', 'YDS', 'CMP%', 'AVG', 'TD', 'INT', 'LNG',
                 'SACK', 'RTG', 'QBR', 'CAR', 'YDS', 'AVG', 'TD', 'LNG']
    # Box score categories from the same game, keyed the same way.
    BOX_KEYS = ['receptions', 'receivingYards', 'yardsPerReception',
                'receivingTouchdowns', 'longReception', 'receivingTargets']
    BOX_RUSHING = ['rushingAttempts', 'rushingYards', 'yardsPerRushAttempt',
                   'rushingTouchdowns', 'longRushing']
    BOX_PASSING = ['completions/passingAttempts', 'passingYards',
                   'yardsPerPassAttempt', 'passingTouchdowns', 'interceptions',
                   'sacks-sackYardsLost', 'adjQBR', 'QBRating']

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")

    def log(self, names, stats):
        return {"names": names, "seasonTypes": [
            {"displayName": "2026 Regular Season", "categories": [
                {"events": [{"stats": stats}]}]}]}

    def test_the_first_yards_column_is_not_the_same_stat_twice(self):
        # The premise of the bug, asserted so the fixtures cannot rot into
        # agreeing with each other.
        self.assertEqual(self.TE_LABELS.index("YDS"), 2)
        self.assertEqual(self.RB_LABELS.index("YDS"), 1)
        self.assertEqual(self.TE_NAMES[2], "receivingYards")
        self.assertEqual(self.RB_NAMES[1], "rushingYards")

    def test_a_tight_ends_receiving_yards_are_his_receiving_yards(self):
        stats = ["7", "9", "82", "11.7", "1", "24",
                 "0", "0", "0.0", "0", "0", "0", "0", "0", "0"]
        out = self.m.values_from_log(self.log(self.TE_NAMES, stats))
        self.assertEqual([v for _s, v in out["receiving_yards"]], [82.0])
        self.assertEqual([v for _s, v in out["receptions"]], [7.0])
        self.assertEqual([v for _s, v in out["receiving_targets"]], [9.0])

    def test_a_running_backs_receiving_yards_are_not_his_rushing_yards(self):
        # 118 rushing, 24 receiving. By column this read 118 as a receiving
        # forecast, which is how a third-down back gets quoted over 70 yards
        # receiving every week.
        stats = ["21", "118", "5.6", "2", "37",
                 "3", "4", "24", "8.0", "0", "12", "0", "0", "0", "0"]
        out = self.m.values_from_log(self.log(self.RB_NAMES, stats))
        self.assertEqual([v for _s, v in out["receiving_yards"]], [24.0])
        self.assertEqual([v for _s, v in out["rushing_yards"]], [118.0])
        self.assertEqual([v for _s, v in out["rushing_attempts"]], [21.0])

    def test_the_box_score_orders_them_differently_and_is_read_the_same_way(self):
        # TGTS is index 1 in the game log and index 5 here. One resolver
        # handles both because both name their columns.
        self.assertEqual(self.m.column_of(self.BOX_KEYS, "receivingYards"), 1)
        self.assertEqual(self.m.column_of(self.BOX_KEYS, "receivingTargets"), 5)
        self.assertEqual(self.m.column_of(self.TE_NAMES, "receivingTargets"), 1)

    def test_one_stat_sits_at_three_different_columns(self):
        # rushingYards: index 1 for a back, 7 for a tight end, 12 for a
        # quarterback. Any hard-coded column is wrong for two thirds of the
        # league, and the wrong number looks perfectly reasonable.
        self.assertEqual(self.RB_NAMES.index("rushingYards"), 1)
        self.assertEqual(self.TE_NAMES.index("rushingYards"), 7)
        self.assertEqual(self.QB_NAMES.index("rushingYards"), 12)

    def test_a_quarterbacks_first_yards_column_is_passing(self):
        # And his rushing yards are eleven columns further along. Read by
        # label, a quarterback's rushing prop would have been his passing
        # yards - a 280 on a bar of 30.
        stats = ["24", "35", "287", "68.6", "8.2", "3", "1", "44", "2",
                 "112.4", "78.1", "6", "41", "6.8", "1", "19"]
        out = self.m.values_from_log(self.log(self.QB_NAMES, stats))
        self.assertEqual([v for _s, v in out["passing_yards"]], [287.0])
        self.assertEqual([v for _s, v in out["passing_tds"]], [3.0])
        self.assertEqual([v for _s, v in out["rushing_yards"]], [41.0])
        self.assertEqual([v for _s, v in out["rushing_attempts"]], [6.0])
        self.assertEqual(self.QB_LABELS.index("YDS"), 2)

    def test_a_quarterback_has_no_receiving_columns_at_all(self):
        # Not zeroes - absent. Reading a missing column as 0 would forecast
        # every quarterback UNDER on receptions, forever.
        stats = ["24", "35", "287", "68.6", "8.2", "3", "1", "44", "2",
                 "112.4", "78.1", "6", "41", "6.8", "1", "19"]
        out = self.m.values_from_log(self.log(self.QB_NAMES, stats))
        self.assertEqual(out["receptions"], [])
        self.assertEqual(out["receiving_yards"], [])

    def test_every_market_can_also_be_graded_off_a_box_score(self):
        # A market that cannot be found in a box score is forecast every week
        # and scored never - it just sits pending, which reads like the game
        # has not finished. passingAttempts is the trap: the box score carries
        # it only inside the combined "completions/passingAttempts" column, so
        # a completions or attempts market would be ungradeable.
        box = set(self.BOX_KEYS) | set(self.BOX_RUSHING) | set(self.BOX_PASSING)
        for market, spec in self.m.MARKETS.items():
            self.assertIn(spec["stat"], box,
                          f"{market} could be forecast but never graded")
        self.assertNotIn("passingAttempts", box)

    def test_a_stat_that_is_not_in_the_payload_is_missing_not_zero(self):
        # A quarterback's log has no receiving column at all. Reading that as
        # index 0 would forecast his completions as receptions.
        self.assertIsNone(self.m.column_of(
            ['passingYards', 'passingTouchdowns'], "receivingYards"))

    def test_every_market_names_a_stat_espn_actually_publishes(self):
        # A typo in MARKETS costs nothing at import and everything at 1pm on
        # Sunday: the column simply is not found and the market silently
        # disappears from the file. Checked against captured names.
        known = (set(self.TE_NAMES) | set(self.RB_NAMES) | set(self.QB_NAMES)
                 | set(self.BOX_KEYS))
        for market, spec in self.m.MARKETS.items():
            self.assertIn(spec["stat"], known,
                          f"{market} reads a stat no captured payload has")

    def test_no_column_number_is_written_down_anywhere(self):
        src = (SCRIPTS / "props-forecast.py").read_text()
        self.assertNotIn('labels.index(', src)
        self.assertNotIn('.index("YDS")', src)


class WhichMarketsAreHisAtAll(unittest.TestCase):
    """A wide receiver has a rushingYards column and it reads zero every week.

    The first rule was "cleared the lowest bar at least twice", and against
    real logs it let Davis Allen through on receptions - 2 games over 3 in 21,
    printing 14.4%, 0.0%, 0.0%, 0.0%. Four numbers, no information, and they
    crowd out the rows that mean something. Twice is nothing in twenty-one
    games and a lot in nine, so the rule is a rate: a quarter of his games
    have to clear the lowest bar.

    It is read off percentile(), which the same row prints as P25-P75, so the
    filter and the spread beside it cannot come to different conclusions.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")

    def test_a_market_he_almost_never_reaches_is_not_his(self):
        # Davis Allen's receptions, 21 real games.
        allen = [0, 1, 0, 2, 1, 0, 3, 1, 0, 0, 2, 1, 1, 0, 3, 0, 1, 2, 1, 0, 1]
        self.assertFalse(self.m.is_relevant(
            [float(v) for v in allen],
            self.m.MARKETS["receptions"]["thresholds"]))

    def test_the_same_two_clearances_in_nine_games_is_a_role(self):
        # 2 of 21 is 10% and not his; 3 of 9 is a third of his games. The
        # first draft of this test used 2 of 9 - 22%, just under the bar - and
        # failed, which is the rule being a rate rather than a count.
        nine = [0.0, 1.0, 2.0, 3.0, 4.0, 1.0, 0.0, 3.0, 5.0]
        self.assertTrue(self.m.is_relevant(
            nine, self.m.MARKETS["receptions"]["thresholds"]))
        self.assertFalse(self.m.is_relevant(
            [0.0, 1.0, 2.0, 3.0, 4.0, 1.0, 0.0, 2.0, 1.0],
            self.m.MARKETS["receptions"]["thresholds"]))

    def test_a_receiver_is_not_forecast_on_carries_he_never_takes(self):
        self.assertFalse(self.m.is_relevant(
            [0.0] * 17, self.m.MARKETS["rushing_attempts"]["thresholds"]))

    def test_a_feature_back_keeps_his_market(self):
        kyren = [17.0, 19.0, 12.0, 23.0, 14.0, 16.0, 11.0, 20.0, 18.0]
        self.assertTrue(self.m.is_relevant(
            kyren, self.m.MARKETS["rushing_attempts"]["thresholds"]))

    def test_an_empty_sample_is_nobody_s_market(self):
        self.assertFalse(self.m.is_relevant([], (3, 4, 5, 6)))

    def test_the_rule_and_the_printed_spread_come_off_one_function(self):
        # If these could drift, a row would show P75 = 2.0 next to a market it
        # was admitted to for reaching 3.
        sample = [0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 9.0]
        p75 = self.m.percentile(sample, self.m.RELEVANT_PERCENTILE)
        self.assertEqual(self.m.is_relevant(sample, (3,)), p75 >= 3)
        self.assertEqual(self.m.is_relevant(sample, (4,)), p75 >= 4)

    def test_percentiles_are_exact_not_resampled(self):
        # Taken off the sample, not off the draws: two runs of a forecast
        # that did not change must not print a different P25.
        sample = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(self.m.percentile(sample, 50), 25.0)
        self.assertEqual(self.m.percentile(sample, 0), 10.0)
        self.assertEqual(self.m.percentile(sample, 100), 40.0)
        self.assertIsNone(self.m.percentile([], 50))


class TheSixPlayersWorthForecasting(unittest.TestCase):
    """ESPN returns a roster in alphabetical order.

    Taking the first six skill players off it gave Adams, Allen, Atwell,
    Corum, Daniels for the Rams - and no Puka Nacua, no Kyren Williams. The
    output looked complete; it was a list of surnames beginning with A.

    Rank by volume instead: targets plus carries, this season, falling back to
    last season for a side that has not played yet - which in week one is
    every side, and is when a silent fallback to alphabetical would return.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")
        self.m.LOG_CACHE.clear()
        self.pools = copy.deepcopy(self.m.POOLS)

    def tearDown(self):
        self.m.LOG_CACHE.clear()
        self.m.POOLS[:] = self.pools

    def only(self, per_team, name="skill"):
        """Shrink one pool's cap so a three-player fixture can fill it."""
        for pool in self.m.POOLS:
            if pool["name"] == name:
                pool["per_team"] = per_team

    def cache(self, aid, season, targets=(), carries=(), passing=()):
        self.m.LOG_CACHE[(str(aid), season)] = {
            "receiving_targets": [("s", float(t)) for t in targets],
            "rushing_attempts": [("s", float(c)) for c in carries],
            "passing_yards": [("s", float(y)) for y in passing],
        }

    def cand(self, aid, name, pos="WR"):
        return (str(aid), name, pos, "LAR", "14")

    def test_the_alphabet_does_not_decide_who_is_forecast(self):
        self.only(2)
        self.cache(1, None, [2, 1], [0, 0])      # Adams, first alphabetically
        self.cache(2, None, [12, 11], [0, 0])    # Nacua
        self.cache(3, None, [0, 0], [19, 21])    # Williams
        picked = self.m.top_by_usage(
            [self.cand(1, "A Adams"), self.cand(2, "P Nacua"),
             self.cand(3, "K Williams")],
            now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual([p[1] for p in picked], ["K Williams", "P Nacua"])

    def test_carries_count_as_much_as_targets(self):
        # A feature back takes no targets and must not rank below a decoy.
        skill = ("receiving_targets", "rushing_attempts")
        self.assertEqual(self.m.usage_of(
            {"receiving_targets": [], "rushing_attempts": [("s", 20.0)]},
            skill), 20.0)
        self.assertEqual(self.m.usage_of(
            {"receiving_targets": [("s", 9.0)], "rushing_attempts": []},
            skill), 9.0)

    def test_a_quarterback_does_not_compete_with_his_own_receivers(self):
        # He throws thirty-five times a game and they are targeted eight. One
        # list ranked by volume gives a side its quarterback and nobody else,
        # so the pools are ranked separately and both get their slots.
        self.only(1)
        self.cache(1, None, passing=[280, 310])          # the starter
        self.cache(2, None, targets=[12, 11])            # the WR1
        picked = self.m.top_by_usage(
            [self.cand(1, "A Passer", "QB"), self.cand(2, "Z Receiver")],
            now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual(sorted(p[1] for p in picked),
                         ["A Passer", "Z Receiver"])

    def test_the_backup_quarterback_is_not_the_one_forecast(self):
        # He is on the roster and he has thrown nothing. Alphabetically he
        # comes first, which is how the old selection would have chosen him.
        self.cache(1, None, passing=[0])                 # the backup
        self.cache(2, None, passing=[280, 310, 260])     # the starter
        picked = self.m.top_by_usage(
            [self.cand(1, "A Backup", "QB"), self.cand(2, "Z Starter", "QB")],
            now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual([p[1] for p in picked], ["Z Starter"])

    def test_every_position_forecast_belongs_to_a_pool(self):
        # POSITIONS is derived from POOLS rather than restated beside it. If
        # it were a second list, a position could be taken off the roster and
        # then ranked by nothing at all.
        for pos in self.m.POSITIONS:
            self.assertIsNotNone(self.m.pool_of(pos), f"{pos} has no pool")
        self.assertIsNone(self.m.pool_of("K"))

    def test_week_one_falls_back_to_last_season_not_to_the_alphabet(self):
        # Nobody has played. Without the fallback every score is zero and the
        # sort returns the roster order it was built to replace.
        now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        self.cache(1, None); self.cache(1, 2025, [30], [0])
        self.cache(2, None); self.cache(2, 2025, [180], [0])
        self.only(1)
        picked = self.m.top_by_usage(
            [self.cand(1, "A Adams"), self.cand(2, "Z Nacua")], now=now)
        self.assertEqual([p[1] for p in picked], ["Z Nacua"])

    def test_the_cap_is_per_side_not_per_slate(self):
        # Six from each team, or one lopsided offence takes every slot and the
        # other side of the game is not forecast at all.
        self.only(1)
        for i in (1, 2, 3, 4):
            self.cache(i, None, [i * 5], [0])
        cands = [(str(i), f"P{i}", "WR", "LAR" if i < 3 else "NYG",
                  "14" if i < 3 else "19") for i in (1, 2, 3, 4)]
        picked = self.m.top_by_usage(
            cands, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual(sorted(p[3] for p in picked), ["LAR", "NYG"])

    def test_a_log_is_fetched_once_and_read_twice(self):
        # It is read to rank him and again to forecast him. Two fetches of one
        # URL are two chances to disagree about the same player, and 40 extra
        # requests a game.
        calls = []
        real_get = self.m.get
        self.m.get = lambda url: (calls.append(url), {"names": [], "seasonTypes": []})[1]
        try:
            self.m.game_values("99")
            self.m.game_values("99")
        finally:
            self.m.get = real_get
        self.assertEqual(len(calls), 1)


class TheStrongestClaimIsNotTheHighestNumber(unittest.TestCase):
    """Asked for "the best prop per player - the highest percentage".

    Ranking by percentage picks the lowest bar every time: 3+ receptions at
    96% for everyone, which is a sentence about the bar and not about the
    player. Without a price there is no way to prefer one probability to
    another, so what is ranked is distance from a coin flip - the same
    question a book answers with its longest and shortest odds - and an UNDER
    counts as much as an OVER.

    COARSE rows are excluded: a claim whose resolution is an artifact has no
    business being the single line shown for a player.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")

    def row(self, player, market, probs, coarse=False, games=20, width=4.0):
        # A tight interval unless a test asks for a wide one.
        band = {t: [max(0.0, p - width), min(100.0, p + width)]
                for t, p in probs.items()}
        return {"athlete_id": player, "player": player, "market": market,
                "unit": "rec", "probabilities": probs, "interval": band,
                "coarse": coarse, "sample_games": games, "sample_mean": 4.0,
                "p25": 2.0, "p75": 6.0, "availability": None}

    def test_a_shrug_does_not_outrank_a_claim(self):
        # 95% off nine games, bracket (54-99), against 90% off twenty-one,
        # bracket (86-94). Ranked on the estimate the nine-game sample wins
        # for having less idea; ranked on the near edge of its bracket it
        # does not.
        picks = self.m.strongest([
            self.row("Shrug", "receptions", {"3": 95.0}, games=9, width=22.0),
            self.row("Claim", "receptions", {"3": 90.0}, games=21, width=4.0)])
        self.assertEqual([p["row"]["player"] for p in picks], ["Claim", "Shrug"])

    def test_the_floor_is_the_near_edge_whichever_side_it_is(self):
        over = self.m.strongest([self.row("A", "receptions", {"3": 80.0})])[0]
        under = self.m.strongest([self.row("B", "receptions", {"6": 20.0})])[0]
        self.assertEqual(over["side"], "OVER")
        self.assertEqual(over["confidence"], 76.0)       # the LOW end
        self.assertEqual(under["side"], "UNDER")
        self.assertEqual(under["confidence"], 76.0)      # 100 - the HIGH end

    def test_a_row_with_no_interval_still_ranks(self):
        # Rows written before intervals existed must not vanish from the list.
        bare = {"athlete_id": "A", "player": "A", "market": "receptions",
                "unit": "rec", "probabilities": {"3": 88.0}, "coarse": False,
                "sample_games": 20, "sample_mean": 4.0, "p25": 2.0, "p75": 6.0,
                "availability": None}
        picks = self.m.strongest([bare])
        self.assertEqual(picks[0]["confidence"], 88.0)

    def test_a_confident_under_beats_a_middling_over(self):
        picks = self.m.strongest([
            self.row("A", "receptions", {"3": 61.0, "6": 8.0})])
        self.assertEqual(picks[0]["side"], "UNDER")
        self.assertEqual(picks[0]["threshold"], 6.0)
        self.assertEqual(picks[0]["probability"], 8.0)

    def test_a_confident_over_is_picked_as_an_over(self):
        picks = self.m.strongest([
            self.row("A", "receptions", {"3": 96.0, "6": 44.0})])
        self.assertEqual(picks[0]["side"], "OVER")
        self.assertEqual(picks[0]["threshold"], 3.0)

    def test_one_line_per_player_across_every_market(self):
        picks = self.m.strongest([
            self.row("A", "receptions", {"3": 70.0}),
            self.row("A", "rushing_yards", {"30": 97.0}),
            self.row("B", "receptions", {"3": 55.0})])
        self.assertEqual(len(picks), 2)
        best = {p["row"]["player"]: p for p in picks}
        self.assertEqual(best["A"]["row"]["market"], "rushing_yards")

    def test_the_list_is_ordered_by_confidence(self):
        picks = self.m.strongest([
            self.row("A", "receptions", {"3": 60.0}),
            self.row("B", "receptions", {"3": 99.0}),
            self.row("C", "receptions", {"3": 80.0})])
        self.assertEqual([p["row"]["player"] for p in picks], ["B", "C", "A"])

    def test_a_coarse_row_is_never_the_one_shown(self):
        picks = self.m.strongest([
            self.row("A", "receptions", {"3": 99.0, "6": 99.0}, coarse=True),
            self.row("A", "rushing_yards", {"30": 71.0})])
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["row"]["market"], "rushing_yards")

    def test_a_player_with_only_coarse_rows_is_left_out_entirely(self):
        # Better absent than represented by a number whose resolution is
        # invented.
        self.assertEqual(self.m.strongest([
            self.row("A", "receptions", {"3": 99.0, "6": 99.0}, coarse=True)]), [])

    def test_it_says_out_loud_that_the_winner_is_flattered(self):
        # A maximum over many noisy estimates is biased upward. Printing the
        # ranking without that line invites reading 100.0% as a certainty.
        src = (SCRIPTS / "props-forecast.py").read_text()
        body = src.split("def cmd_best(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("noisy", body)
        self.assertIn("FLOOR", body)
        self.assertIn("print(NOT_A_BET)", body)


class WhatHeDoesWhenHePlaysIsADifferentQuestion(unittest.TestCase):
    """A bootstrap over a player's game log conditions on him having played.

    Every game in the sample is one he was active for, so the number is
    P(clears the bar | he plays). A book prices P(plays) x that. Where the two
    differ the gap looks like an enormous edge and is an artifact of the
    sample - the single most likely way this file produces a confident wrong
    answer.

    It is reported and never applied: multiplying by a three-game attendance
    record would invent precision, and a player back from injury would be
    marked down for the weeks he missed.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")

    def test_a_player_who_has_missed_games_is_flagged(self):
        a = self.m.availability(10, 6)
        self.assertEqual(a["rate"], 0.6)
        self.assertTrue(a["thin"])

    def test_an_ever_present_player_is_not_flagged(self):
        self.assertFalse(self.m.availability(10, 10)["thin"])

    def test_it_never_exceeds_one(self):
        # ESPN's team record and its game logs update on different clocks.
        self.assertEqual(self.m.availability(1, 2)["rate"], 1.0)

    def test_an_unknown_team_count_is_no_claim_at_all(self):
        self.assertIsNone(self.m.availability(None, 4))
        self.assertIsNone(self.m.availability(0, 0))

    def test_availability_does_not_change_the_probability(self):
        # The correction is a label, not a multiplier. If this ever starts
        # scaling the number, the row stops being reproducible from its seed.
        sample = [55.0, 62.0, 71.0, 48.0, 90.0, 33.0, 66.0, 77.0, 41.0, 58.0]
        args = ("1", "A @ B", None, "2", "A Receiver", "WR", "AAA",
                {"receiving_yards": sample}, ["2026"])
        full = self.m.rows_for_player(*args, {"team_games": 10, "played": 10,
                                              "rate": 1.0, "thin": False})
        part = self.m.rows_for_player(*args, {"team_games": 10, "played": 4,
                                              "rate": 0.4, "thin": True})
        self.assertEqual(full[0]["probabilities"], part[0]["probabilities"])
        self.assertTrue(part[0]["availability"]["thin"])

    def test_the_flagged_rows_are_counted_in_the_summary(self):
        src = (SCRIPTS / "props-forecast.py").read_text()
        body = src.split("def cmd_forecast(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('PLAYED', body)
        self.assertIn("flagged += 1", body)


class EveryMarketIsGradedAgainstItsOwnStat(unittest.TestCase):
    """Grading read the receiving category and called the answer yards.

    With five markets that is wrong four ways: a receptions forecast scored
    against yards would be marked HIT on every row. The stat name travels on
    the forecast, and the box score category that carries it is found by that
    name.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.m.STORE = Path(self.tmp.name) / "forecasts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_market_carries_the_stat_it_will_be_graded_on(self):
        rows = self.m.rows_for_player(
            "1", "A @ B", None, "2", "A Back", "RB", "AAA",
            {"rushing_yards": [40.0, 55.0, 71.0, 33.0, 88.0, 62.0, 29.0, 90.0,
                               47.0, 51.0],
             "receptions": [3.0, 4.0, 2.0, 5.0, 3.0, 6.0, 4.0, 2.0, 5.0, 3.0]},
            ["2026"], None)
        by = {r["market"]: r for r in rows}
        self.assertEqual(by["rushing_yards"]["stat"], "rushingYards")
        self.assertEqual(by["receptions"]["stat"], "receptions")
        self.assertEqual(by["rushing_yards"]["unit"], "yds")
        self.assertEqual(by["receptions"]["unit"], "rec")

    def test_a_receptions_forecast_is_scored_in_receptions(self):
        # 82 yards on 7 catches: graded against yards, "6+ receptions" is a
        # hit; graded against receptions it is also a hit but for the right
        # reason, and "8+ receptions" correctly is not.
        box = {"header": {"competitions": [{"status": {"type": {
                   "name": "STATUS_FINAL"}}}]},
               "boxscore": {"players": [{"statistics": [
                   {"name": "receiving",
                    "keys": ["receptions", "receivingYards",
                             "yardsPerReception", "receivingTouchdowns",
                             "longReception", "receivingTargets"],
                    "athletes": [{"athlete": {"id": "2"},
                                  "stats": ["7", "82", "11.7", "1", "24", "9"]}]}]}]}}
        real_get = self.m.get
        self.m.get = lambda url: box
        try:
            self.assertEqual(self.m.actual_value("1", "2", "receptions")[0], 7.0)
            self.assertEqual(self.m.actual_value("1", "2", "receivingYards")[0], 82.0)
            self.assertEqual(self.m.actual_value("1", "2", "receivingTargets")[0], 9.0)
        finally:
            self.m.get = real_get

    def test_a_game_still_being_played_is_not_graded(self):
        live = {"header": {"competitions": [{"status": {"type": {
            "name": "STATUS_IN_PROGRESS"}}}]}, "boxscore": {"players": []}}
        real_get = self.m.get
        self.m.get = lambda url: live
        try:
            value, why = self.m.actual_value("1", "2", "receptions")
        finally:
            self.m.get = real_get
        self.assertIsNone(value)
        self.assertIn("not final", why)

    def test_a_grade_run_that_dies_halfway_keeps_what_it_had(self):
        # `grade | head -18` broke the pipe, killed the process before the
        # single save at the end, and threw away every grade it had just
        # collected - silently, because the rows simply stayed pending. A
        # dropped connection on the nineteenth player does the same thing.
        rows = [{"event_id": "1", "athlete_id": str(i), "player": f"P{i}",
                 "market": "receptions", "stat": "receptions", "unit": "rec",
                 "probabilities": {"3": 50.0}, "actual": None,
                 "graded_utc": None} for i in range(3)]
        self.m.save({"forecasts": rows})

        seen = []

        def flaky(event_id, athlete_id, stat):
            seen.append(athlete_id)
            if len(seen) > 1:
                raise KeyboardInterrupt("the pipe went away")
            return 7.0, "final"

        real = self.m.actual_value
        self.m.actual_value = flaky
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.m.cmd_grade()
        finally:
            self.m.actual_value = real

        kept = self.m.load()["forecasts"]
        self.assertEqual(kept[0]["actual"], 7.0, "the finished grade survived")
        self.assertIsNone(kept[2]["actual"], "the unreached one is still pending")

    def test_calibration_is_reported_market_by_market(self):
        # Receptions are integers off a four-value list and yards are not.
        # Pooled, one market can be badly off while the average looks fine.
        rows = [{"market": "receptions", "probabilities": {"3": 90.0}, "actual": 1.0},
                {"market": "receiving_yards", "probabilities": {"40": 60.0}, "actual": 70.0}]
        split = self.m.by_market(rows)
        self.assertEqual(sorted(split), ["receiving_yards", "receptions"])
        self.assertLess(self.m.buckets(split["receptions"])[90]["gap"], 0)


class TwoBarsWithNoGameBetweenThem(unittest.TestCase):
    """Stafford came back 76.2% for 200+ AND 76.2% for 225+.

    A plain bootstrap draws one of the player's own games, so it can only ever
    produce a value he has already produced. Twenty-one games and a bar every
    twenty-five yards means bars with no game between them are literally the
    same question, and the answer is identical - correctly. The number was
    real; the resolution it appeared to have was not, and three of four
    quarterback rows carried the flag.

    The fix is to let each past game stand for a small neighbourhood instead
    of a single point. The width of that neighbourhood is the whole argument,
    so it is Silverman's rule computed off his own games - a streaky player
    gets a wide one, a metronome a narrow one, and nobody chose either.

    Two things this must not do: move the answer away from what he actually
    did, and invent a spread for a player who has none.
    """

    # Matthew Stafford's real passing yards, 21 games, captured 2026-09-21
    # from the live game log - the exact sample that produced the bug. Note
    # the gap between 196 and 243: no game lands between the 200 and 225 bars,
    # which is the whole of it. An invented fixture with a game at 211 in it
    # passed the "it is fixed" tests and failed this one, which is why the
    # numbers here are pulled and not written.
    STAFFORD = [130.0, 155.0, 181.0, 182.0, 196.0, 243.0, 245.0, 258.0, 259.0,
                269.0, 273.0, 280.0, 281.0, 281.0, 298.0, 304.0, 368.0, 374.0,
                375.0, 389.0, 457.0]

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")
        self.bars = self.m.MARKETS["passing_yards"]["thresholds"]

    def unsmoothed(self, sample, thresholds, counts=False):
        """What the old bootstrap answered, exactly: h = 0 is a step."""
        return self.m.smoothed_probs(sample, thresholds, counts, h=0.0)

    def test_the_bug_is_real_and_the_old_answer_still_shows_it(self):
        # The premise, asserted rather than remembered: with no smoothing two
        # of these bars are the same number.
        old = self.unsmoothed(self.STAFFORD, self.bars)
        self.assertEqual(old[200], 76.2)
        self.assertEqual(old[225], 76.2)
        self.assertTrue(self.m.is_coarse(old, self.bars))

    def test_smoothing_tells_the_two_bars_apart(self):
        new = self.m.smoothed_probs(self.STAFFORD, self.bars)
        self.assertNotEqual(new[200], new[225])
        self.assertEqual(len(set(new.values())), len(self.bars))
        self.assertFalse(self.m.is_coarse(new, self.bars))

    def test_it_does_not_drag_the_answer_off_his_own_games(self):
        # Filling the gaps between his afternoons is the point; moving the
        # estimate somewhere else would be fitting a curve to him, which is
        # the thing this file refuses to do.
        old = self.unsmoothed(self.STAFFORD, self.bars)
        new = self.m.smoothed_probs(self.STAFFORD, self.bars)
        for t in self.bars:
            self.assertLess(abs(new[t] - old[t]), 12.0,
                            f"{t}+ moved from {old[t]} to {new[t]}")

    def test_the_bars_still_fall_as_they_rise(self):
        new = self.m.smoothed_probs(self.STAFFORD, self.bars)
        ordered = [new[t] for t in sorted(new)]
        self.assertEqual(ordered, sorted(ordered, reverse=True))

    def test_a_player_with_no_spread_gets_none_invented(self):
        # Every game identical. There is genuinely nothing to smooth, and a
        # bandwidth conjured for him would be the one piece of fiction here.
        self.assertEqual(self.m.bandwidth([20.0] * 12), 0.0)
        flat = self.m.smoothed_probs([20.0] * 12, (10, 15, 18))
        self.assertTrue(self.m.is_coarse(flat, (10, 15, 18)))
        # A bar he has never been on either side of is 0 or 100, not a
        # fraction: with no spread there is no room for doubt to live in.
        self.assertEqual(set(flat.values()), {100.0})
        self.assertEqual(self.m.smoothed_probs([20.0] * 12, (25,))[25], 0.0)

    def test_the_bandwidth_comes_off_the_sample_not_off_a_constant(self):
        # A streaky player gets a wide neighbourhood and a metronome a narrow
        # one. If this were a hand-picked number the two would match.
        steady = [250.0, 252.0, 248.0, 251.0, 249.0, 250.0, 253.0, 247.0,
                  250.0, 251.0]
        streaky = [120.0, 380.0, 160.0, 410.0, 200.0, 340.0, 95.0, 430.0,
                   180.0, 360.0]
        self.assertLess(self.m.bandwidth(steady), self.m.bandwidth(streaky))
        self.assertGreater(self.m.bandwidth(streaky), 20.0)

    def test_the_bandwidth_shrinks_as_the_games_pile_up(self):
        # n^(-1/5): more games, less need to borrow from the neighbours.
        # The same games three times over - identical spread, triple the
        # sample - so only the count can move the answer. Comparing two
        # DIFFERENT slices instead let a version with the n term deleted pass,
        # because the slices had different spreads.
        once = self.STAFFORD
        thrice = self.STAFFORD * 3
        self.assertGreater(self.m.bandwidth(once), 0.0)
        self.assertLess(self.m.bandwidth(thrice), self.m.bandwidth(once) * 0.85)

    def test_no_bandwidth_is_written_down_in_the_file(self):
        # The one number that was not invented. A literal here would be the
        # whole answer smuggled in as a constant.
        src = (SCRIPTS / "props-forecast.py").read_text()
        body = src.split("def bandwidth(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("0.9", body, "Silverman's rule, and only it")
        self.assertIn("1.34", body)
        self.assertIn("-0.2", body)


class ACatchIsAWholeNumber(unittest.TestCase):
    """Smoothing receptions the way yards are smoothed is wrong.

    3.4 receptions is not a thing. A book settles 4+ on whether the count is
    four or more, so a counting market has to be asked at the bar minus a
    half - the same question asked of a number that is going to be rounded.
    Asked at the bar itself, a smoothed count loses half the mass sitting
    exactly on it, and every counting market reads low.
    """

    NACUA = [5.0, 8.0, 11.0, 6.0, 9.0, 7.0, 12.0, 5.0, 10.0, 8.0,
             6.0, 9.0, 7.0, 11.0, 8.0, 5.0, 10.0, 6.0, 9.0, 7.0]

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")

    def test_a_count_is_asked_at_the_half(self):
        # Eight catches, bar of 8. As a count that is a clear hit, so it is
        # asked at 7.5 and lands well above a coin flip. Asked at 8.0 the
        # smear splits in half and it reads 50%.
        h = 1.0
        self.assertAlmostEqual(self.m.point_prob(8.0, 8, h, True), 0.69, delta=0.02)
        self.assertAlmostEqual(self.m.point_prob(8.0, 8, h, False), 0.50, delta=0.001)

    def test_every_counting_market_is_declared_one(self):
        counts = {m for m, spec in self.m.MARKETS.items() if spec["counts"]}
        self.assertEqual(counts, {"receptions", "receiving_targets",
                                  "rushing_attempts", "passing_tds"})

    def test_yards_are_not_treated_as_counts(self):
        for market in ("passing_yards", "receiving_yards", "rushing_yards"):
            self.assertFalse(self.m.MARKETS[market]["counts"],
                             f"{market} is a distance, not a tally")

    def test_a_receiver_who_never_caught_four_still_gets_a_number_for_it(self):
        # He has 5s and 6s and no 4s at all, so a plain bootstrap said 100%
        # for 3+, 4+ and 5+ alike. Smoothed as a count, they separate.
        probs = self.m.smoothed_probs(
            self.NACUA, self.m.MARKETS["receptions"]["thresholds"], counts=True)
        self.assertEqual(len(set(probs.values())), 4)
        self.assertGreater(probs[3], probs[6])

    def test_a_count_market_does_not_smear_below_zero_into_a_hit(self):
        # Nobody catches minus one pass. The bar is what moves by a half, not
        # the floor.
        never = [0.0] * 8 + [1.0, 0.0]
        probs = self.m.smoothed_probs(never, (3, 4), counts=True)
        self.assertLess(probs[3], 5.0)


class HowFirmIsThatPercentage(unittest.TestCase):
    """COARSE was a flag firing on a symptom of a small sample.

    The underlying question it could not answer is how much a percentage
    would move if the player had happened to play a different twenty-one
    games. That has an answer - resample his games and look - and it is worth
    more than a flag, because it is a number and it appears on every row
    rather than on the ones that tripped a test.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")

    def test_fewer_games_means_a_wider_interval(self):
        # The whole point: "76.2% off fifteen games" and "76.2% off a hundred"
        # stop being printed identically.
        long_career = [40.0, 55.0, 70.0, 35.0, 90.0, 60.0, 45.0, 75.0] * 12
        short = long_career[:8]
        wide = self.m.interval(short, (60,), seed="s")[60]
        tight = self.m.interval(long_career, (60,), seed="s")[60]
        self.assertGreater(wide[1] - wide[0], (tight[1] - tight[0]) * 2)

    def test_the_interval_brackets_the_estimate(self):
        sample = [40.0, 55.0, 70.0, 35.0, 90.0, 60.0, 45.0, 75.0, 50.0, 65.0]
        probs = self.m.smoothed_probs(sample, (60,))
        lo, hi = self.m.interval(sample, (60,), seed="s")[60]
        self.assertLessEqual(lo, probs[60])
        self.assertGreaterEqual(hi, probs[60])

    def test_one_resampled_set_of_games_answers_every_bar(self):
        # Drawing a fresh set per bar would let 250+ come back above 225+ in
        # the interval even though it cannot in the estimate.
        sample = [40.0, 55.0, 70.0, 35.0, 90.0, 60.0, 45.0, 75.0, 50.0, 65.0]
        band = self.m.interval(sample, (40, 60, 80), seed="s")
        self.assertGreaterEqual(band[40][0], band[60][0])
        self.assertGreaterEqual(band[60][0], band[80][0])

    def test_a_bars_interval_does_not_depend_on_which_others_were_asked(self):
        # The sharp version of the same property. One resampled set per
        # iteration is shared by every bar, so asking about 60 alone and
        # asking about 40 and 60 together must give 60 the identical answer.
        # A fresh resample per bar hands 60 a different draw depending on how
        # many bars precede it - which the monotonicity test above does not
        # notice, because far-apart bars stay in order by luck.
        sample = [40.0, 55.0, 70.0, 35.0, 90.0, 60.0, 45.0, 75.0, 50.0, 65.0]
        alone = self.m.interval(sample, (60,), seed="s")
        together = self.m.interval(sample, (40, 60, 80), seed="s")
        self.assertEqual(alone[60], together[60])

    def test_every_row_carries_one(self):
        sample = [40.0, 55.0, 70.0, 35.0, 90.0, 60.0, 45.0, 75.0, 50.0, 65.0]
        rows = self.m.rows_for_player(
            "1", "A @ B", None, "2", "A Receiver", "WR", "AAA",
            {"receiving_yards": sample}, ["2026"], None)
        row = rows[0]
        self.assertEqual(sorted(row["interval"]), sorted(row["probabilities"]))
        self.assertIn("bandwidth", row)

    def test_the_interval_is_printed_beside_the_percentage(self):
        # Stored and not shown would be the same as not computed. Asserted on
        # the f-string that builds the line, not on the word "interval"
        # appearing somewhere in the file.
        src = (SCRIPTS / "props-forecast.py").read_text()
        body = src.split("def cmd_forecast(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('band[str(t)][0]', body)
        self.assertIn('band[str(t)][1]', body)


class ThePropsHallIsItsOwnBuilding(unittest.TestCase):
    """The forecasts used to live inside Ace's house.

    They are not his work and were never his to keep: he places bets against
    posted odds, and this thing holds no odds at all and is forbidden to. A
    door of its own says that in the one place the owner actually looks.
    """

    def setUp(self):
        self.html = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        self.js = (ROOT / "mission-control-api" / "ace.js").read_text()

    def test_the_hall_has_a_door_of_its_own(self):
        self.assertRegex(self.html, r"doors\.push\(\{ name: PROPS_DOOR")

    def test_a_door_with_no_agent_behind_it_still_opens(self):
        # openPanel looks the name up in DATA.agents and returns if it finds
        # nothing, so the hall had to be handled BEFORE that lookup or the key
        # press would do nothing at all and look like a broken door.
        body = self.html.split("function openPanel(", 1)[1].split("\nlet ideaBusy", 1)[0]
        before = body.split("DATA.agents", 1)[0]
        self.assertIn("PROPS_DOOR", before,
                      "the hall must be handled before the agent lookup")
        self.assertIn("openPropsPanel()", before)

    def test_ace_no_longer_carries_the_forecasts(self):
        self.assertNotIn("aceForecasts", self.html)
        self.assertNotIn("loadAceForecasts", self.html)
        body = self.html.split("function openPanel(", 1)[1].split("\nlet ideaBusy", 1)[0]
        self.assertIn("loadAceLedger()", body, "the ledger is still his")

    def test_the_hall_is_not_standing_on_a_house_plot(self):
        # PLOTS is indexed by agent, so an agent added later takes the next
        # one. If the hall were sitting on it they would be built on top of
        # each other, which is only visible once that agent exists.
        plots = [(float(a), float(b)) for a, b in
                 re.findall(r"\{ x:\s*([\d.]+),\s*y:\s*([\d.]+),\s*flip", self.html)]
        self.assertEqual(len(plots), 8)
        hx, hy = self.props_plot()
        for (px, py) in plots:
            apart = (hx + 5 <= px or px + 4 <= hx or hy + 4.4 <= py or py + 4.3 <= hy)
            self.assertTrue(apart, f"the hall overlaps the plot at {px},{py}")

    def props_plot(self):
        m = re.search(r"PROPS_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html)
        return float(m.group(1)), float(m.group(2))

    def test_the_hall_gets_a_path_out_to_the_lane(self):
        # Without one it stands on grass with no way in, which is what made
        # the houses read as scenery before they were given theirs.
        self.assertRegex(self.html,
                         r"PROPS_PLOT\.x \+ 2\.5[\s\S]{0,400}PAVED\[y\]\[dx\] = true")


class NothingGrowsThroughAWall(unittest.TestCase):
    """Three trees grew through the props hall.

    The comment above the tree list already said "nudged off the house plots -
    four of these used to grow through a wall", and the nudge was done by
    hand against PLOTS. The hall is not in PLOTS, so it was invisible to that
    nudge and the same bug happened again immediately - including a tree
    standing in front of the name plate, which then read "P    PROPS".

    Nudging is something a person remembers to do. This is the version that
    does not need remembering: every scenery coordinate in the file, against
    every building's wall and name plate, with depth taken into account so a
    prop drawn BEHIND a building does not count.

    It checks all eight plots, not just the occupied ones. A sixth agent
    takes the next plot, and the tree that has been sitting on it harmlessly
    for months is through its wall the moment it is built.
    """

    T = 32

    def setUp(self):
        self.html = (ROOT / "mission-control-api" / "public" / "village.html").read_text()

    def scenery(self):
        """[(kind, [(x, y)], half width, height, depth pad)] straight out of the file."""
        out = []
        sizes = {"tree": (0.65, 2.4, 40), "bush": (0.50, 0.80, 8),
                 "rock": (0.40, 0.65, 6)}
        for m in re.finditer(r"\.forEach\(\(\[tx\s*,\s*ty\][^\n]*place\(`?'?([a-z]+)",
                             self.html):
            kind = m.group(1)
            if kind not in sizes:
                continue
            before = self.html[:m.start()]
            end = before.rfind("]]") + 2
            start = before.rfind("[[", 0, end)
            pts = [tuple(float(v) for v in p.split(","))
                   for p in re.findall(r"\[\s*([-\d.]+\s*,\s*[-\d.]+)\s*\]",
                                       before[start:end])]
            out.append((kind, pts) + sizes[kind])
        return out

    def buildings(self):
        """(name, x, y, w, h, depth, plate width) in tiles, for all eight plots."""
        T = self.T
        for i, (x, y) in enumerate(
                (float(a), float(b)) for a, b in
                re.findall(r"\{ x:\s*([\d.]+),\s*y:\s*([\d.]+),\s*flip", self.html)):
            yield (f"house plot {i}", x, y, 128 / T, 116 / T, y * T + 116,
                   max(70, 8 * 10 + 22) / T)
        m = re.search(r"PROPS_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html)
        x, y = float(m.group(1)), float(m.group(2))
        yield ("the props hall", x, y, 160 / T, 124 / T, y * T + 124,
               max(96, len("Player Props") * 10 + 22) / T)

    def collisions(self):
        bad = []
        blds = list(self.buildings())
        for kind, pts, hw, hh, pad in self.scenery():
            for (sx, sy) in pts:
                for (name, bx, by, bw, bh, bdepth, plate) in blds:
                    if sy * self.T + pad < bdepth:
                        continue                       # drawn behind it
                    for (rx0, rx1, ry0, ry1, what) in (
                            (bx, bx + bw, by, by + bh, "wall"),
                            (bx + bw / 2 - plate / 2, bx + bw / 2 + plate / 2,
                             by + bh, by + bh + 0.9, "name plate")):
                        if (sx + hw > rx0 and sx - hw < rx1
                                and sy > ry0 and sy - hh < ry1):
                            bad.append(f"{kind} at ({sx:g},{sy:g}) covers "
                                       f"{name}'s {what}")
        return bad

    def test_the_file_really_does_list_scenery_to_check(self):
        # A parser that silently finds nothing would make the test below pass
        # for the wrong reason - the failure mode this repo keeps hitting.
        found = self.scenery()
        self.assertEqual(sorted(k for k, *_ in found), ["bush", "rock", "tree"])
        for kind, pts, *_ in found:
            self.assertGreater(len(pts), 5, f"only {len(pts)} {kind} found")

    def test_the_check_can_actually_fail(self):
        # Stand a tree on the hall's doorstep and confirm it is reported.
        m = re.search(r"PROPS_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html)
        hx, hy = float(m.group(1)), float(m.group(2))
        planted = self.html.replace("[2,16],[8,17]",
                                    f"[{hx + 2:g},{hy + 3:g}],[8,17]", 1)
        real, self.html = self.html, planted
        try:
            self.assertTrue(any("props hall" in b for b in self.collisions()))
        finally:
            self.html = real

    def test_nothing_covers_a_wall_or_a_name_plate(self):
        self.assertEqual(self.collisions(), [])


class OneScreenPerMarket(unittest.TestCase):
    """Seven markets on one scroll read as one long answer.

    A quarterback's passing yards and a tight end's targets are different
    questions and were stacked in the same list. A tab each.

    The tabs come from the catalogue props-forecast.py writes into
    forecasts.json, never from a list kept in the page: MARKETS is where a
    market is defined, and a second list in JavaScript would quietly leave a
    tab missing the next time one is added there.
    """

    def setUp(self):
        self.m = load("props_forecast", "props-forecast.py")
        self.html = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        self.js = (ROOT / "mission-control-api" / "ace.js").read_text()
        self.tmp = tempfile.TemporaryDirectory()
        self.m.STORE = Path(self.tmp.name) / "forecasts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_catalogue_is_every_market_and_nothing_else(self):
        cat = self.m.catalogue()
        self.assertEqual(list(cat), list(self.m.MARKETS))
        for name, spec in self.m.MARKETS.items():
            self.assertEqual(cat[name]["unit"], spec["unit"])
            self.assertEqual(cat[name]["thresholds"], list(spec["thresholds"]))
            self.assertEqual(cat[name]["counts"], spec["counts"])

    def test_it_is_written_every_time_the_file_is_saved(self):
        # Not only when forecasting: a grade run rewrites the file too, and a
        # save that dropped the catalogue would empty the tab bar.
        self.m.save({"forecasts": []})
        on_disk = json.loads(self.m.STORE.read_text())
        self.assertEqual(list(on_disk["markets"]), list(self.m.MARKETS))

    def test_the_order_survives_the_round_trip(self):
        # The tab order is the catalogue's order, so passing markets stay
        # first. A dict that came back sorted would put attempts before yards.
        self.m.save({"forecasts": []})
        self.assertEqual(list(json.loads(self.m.STORE.read_text())["markets"])[0],
                         list(self.m.MARKETS)[0])

    def test_the_api_passes_the_catalogue_through(self):
        feed = self.js.split("/api/ace/forecasts", 1)[1].split("app.get(", 1)[0]
        self.assertIn("markets:", feed)
        self.assertIn("data.markets", feed)

    def test_the_page_holds_no_market_list_of_its_own(self):
        # The whole point. If any market name is spelled out in the page, the
        # two lists have already started to drift.
        panel = self.html.split("function propsTabs(", 1)[1].split(
            "\nasync function loadAceLedger", 1)[0]
        for market in self.m.MARKETS:
            self.assertNotIn(market, panel,
                             f"{market} is named in the page; it belongs to MARKETS")

    def test_the_tabs_are_built_from_the_catalogue(self):
        panel = self.html.split("function propsTabs(", 1)[1].split(
            "\nasync function loadAceLedger", 1)[0]
        self.assertIn("PROPS.markets", panel)
        self.assertIn("propsPick(", panel)

    def test_a_market_with_no_forecasts_keeps_its_tab(self):
        # "nobody qualified for this one today" and "this market does not
        # exist" are different, and a missing tab says the second.
        panel = self.html.split("function drawProps(", 1)[1].split("\n}", 1)[0]
        self.assertIn("rows.length", panel)
        self.assertIn("quarter of his games", panel)

    def test_each_screen_shows_only_its_own_market(self):
        panel = self.html.split("function drawProps(", 1)[1].split("\n}", 1)[0]
        self.assertIn("f.market === propsTab", panel)


class ADeadEndSaysWhyItIsADeadEnd(unittest.TestCase):
    """The gallery said LOCAL ONLY and stopped there.

    A build sat like that for two days. emily-finish.py knew exactly why -
    listing.json would not parse, and it said so on two separate attempts -
    but it said so into the dispatcher log, which nobody reads. The card
    showed a badge and no reason, and the only way to find out was to open a
    terminal and re-run the drafter by hand.

    Worse, the API was throwing the same fact away a second time: readJson
    mapped "no such file" and "this file is malformed" onto the same null, so
    a broken listing.json produced a card with no title, no product type and
    no price and nothing to say why all three were blank.

    The reason is recorded on the build now, in the drafter's own words,
    cleared the moment a draft succeeds.
    """

    def setUp(self):
        self.m = load("emily_finish", "emily-finish.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "maple-leaf-pocket-sticker"
        self.d.mkdir()
        (self.d / "build.json").write_text(
            '{"status": "ready_local", "idea": "Maple Leaf Pocket Sticker"}')

    def tearDown(self):
        self.tmp.cleanup()

    def res(self, stderr="", stdout="", code=1):
        return types.SimpleNamespace(stderr=stderr, stdout=stdout, returncode=code)

    def build(self):
        return json.loads((self.d / "build.json").read_text())

    # --- what the reason is ------------------------------------------------

    def test_the_refusal_is_the_drafter_s_own_words(self):
        # The real one, from the real incident.
        said = ("cannot read /root/ecosystem/agents/emily/builds/"
                "maple-leaf-pocket-sticker/listing.json: Expecting ',' "
                "delimiter: line 2 column 63 (char 64)")
        self.assertEqual(self.m.reason_from(self.res(stderr=said)), said)

    def test_stdout_is_the_fallback_when_it_never_reached_stderr(self):
        self.assertEqual(self.m.reason_from(self.res(stdout="ran out of disk")),
                         "ran out of disk")

    def test_a_crash_is_reduced_to_the_line_that_says_something(self):
        # Truncating a traceback from the front puts "Traceback (most recent
        # call last):" on the card and throws the exception away, which is the
        # wrong half of it.
        crash = ('Traceback (most recent call last):\n'
                 '  File "emily-printify.py", line 43, in <module>\n'
                 '    import disclosures\n'
                 "ModuleNotFoundError: No module named 'disclosures'\n")
        got = self.m.reason_from(self.res(stderr=crash))
        self.assertIn("ModuleNotFoundError: No module named 'disclosures'", got)
        self.assertNotIn("Traceback (most recent", got)

    def test_a_silent_failure_still_says_something(self):
        # "" on a card is indistinguishable from not looking.
        got = self.m.reason_from(self.res(code=7))
        self.assertIn("7", got)
        self.assertTrue(got.strip())

    def test_a_long_reason_is_cut_down_to_a_card(self):
        got = self.m.reason_from(self.res(stderr="x" * 5000))
        self.assertLessEqual(len(got), self.m.REASON_CHARS + 8)
        self.assertTrue(got.endswith("..."))

    # --- where it goes -----------------------------------------------------

    def test_the_reason_is_written_onto_the_build(self):
        self.m.note_draft(self.d, {"reason": "listing.json will not parse",
                                   "exit": 1, "when_utc": "2026-09-21 16:41:02"})
        blocked = self.build()["draft_blocked"]
        self.assertEqual(blocked["reason"], "listing.json will not parse")
        self.assertEqual(blocked["exit"], 1)
        self.assertEqual(blocked["when_utc"], "2026-09-21 16:41:02")

    def test_emily_s_own_account_of_the_build_is_not_disturbed(self):
        self.m.note_draft(self.d, {"reason": "x", "exit": 1, "when_utc": "now"})
        self.assertEqual(self.build()["status"], "ready_local")
        self.assertEqual(self.build()["idea"], "Maple Leaf Pocket Sticker")

    def test_a_success_clears_it(self):
        # A stale reason on a build that has since drafted is worse than no
        # reason: it describes a problem that is over.
        self.m.note_draft(self.d, {"reason": "x", "exit": 1, "when_utc": "now"})
        self.m.note_draft(self.d, None)
        self.assertNotIn("draft_blocked", self.build())

    def test_clearing_a_build_that_was_never_blocked_rewrites_nothing(self):
        before = (self.d / "build.json").read_text()
        self.m.note_draft(self.d, None)
        self.assertEqual((self.d / "build.json").read_text(), before)

    def test_a_build_json_that_will_not_parse_is_left_exactly_as_it_is(self):
        # Overwriting Emily's own account of what she made, in order to
        # explain that a different file would not parse, would destroy the
        # more valuable of the two.
        broken = '{"status": "ready_local",,}'
        (self.d / "build.json").write_text(broken)
        self.m.note_draft(self.d, {"reason": "x", "exit": 1, "when_utc": "now"})
        self.assertEqual((self.d / "build.json").read_text(), broken)

    def test_a_build_json_holding_something_other_than_an_object_is_too(self):
        (self.d / "build.json").write_text('["not", "an", "object"]')
        self.m.note_draft(self.d, {"reason": "x", "exit": 1, "when_utc": "now"})
        self.assertEqual(json.loads((self.d / "build.json").read_text()),
                         ["not", "an", "object"])

    # --- end to end --------------------------------------------------------

    def run_finish(self, stub_body, exit_code, already_blocked=False):
        """emily-finish against a stub drafter, in a throwaway ecosystem."""
        root = Path(self.tmp.name) / "root"
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        build = root / "agents" / "emily" / "builds" / "a-build"
        build.mkdir(parents=True, exist_ok=True)
        start = {"status": "ready_local"}
        if already_blocked:
            start["draft_blocked"] = {"reason": "an earlier attempt failed",
                                      "exit": 1, "when_utc": "2026-09-20 09:00:00"}
        (build / "build.json").write_text(json.dumps(start))
        (root / "scripts" / "emily-printify.py").write_text(
            "import sys\n"
            f"sys.stderr.write({stub_body!r})\n"
            f"sys.exit({exit_code})\n")
        env = dict(os.environ, ECOSYSTEM_ROOT=str(root))
        r = subprocess.run([sys.executable, str(SCRIPTS / "emily-finish.py"),
                            "a-build"], capture_output=True, text=True, env=env)
        return r, json.loads((build / "build.json").read_text())

    def test_a_refused_draft_leaves_the_reason_behind(self):
        r, build = self.run_finish("no such catalogue entry: 'sticker'\n", 2)
        self.assertEqual(r.returncode, 0, "a local build is still a real build")
        self.assertEqual(build["draft_blocked"]["reason"],
                         "no such catalogue entry: 'sticker'")
        self.assertEqual(build["draft_blocked"]["exit"], 2)
        self.assertEqual(build["status"], "ready_local")

    def test_a_drafted_build_leaves_none(self):
        r, build = self.run_finish("", 0)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("draft_blocked", build)

    def test_a_build_that_finally_drafts_stops_showing_why_it_did_not(self):
        # The version of the test above started from a build with nothing to
        # clear, so deleting the clear entirely still passed it. This one
        # starts from a build already carrying yesterday's refusal.
        r, build = self.run_finish("", 0, already_blocked=True)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("draft_blocked", build,
                         "a reason that describes a solved problem is worse "
                         "than none")

    def test_a_second_refusal_replaces_the_first(self):
        _, build = self.run_finish("the catalogue has no such entry\n", 2,
                                   already_blocked=True)
        self.assertEqual(build["draft_blocked"]["reason"],
                         "the catalogue has no such entry")
        self.assertEqual(build["draft_blocked"]["exit"], 2)

    def test_the_finisher_never_fails_the_task_over_the_last_mile(self):
        # The whole premise: the artwork is good work and throwing it away
        # over a missing draft would be the expensive mistake.
        r, _ = self.run_finish("everything is on fire\n", 9)
        self.assertEqual(r.returncode, 0)
        self.assertIn("ready_local", r.stdout)


class TheApiSaysWhichFileIsBroken(unittest.TestCase):
    """readJson turned two different problems into the same null.

    "there is no listing.json" and "listing.json is malformed" produce
    identical cards - no title, no product type, no price - and the gallery
    had no way to tell the owner which one it was looking at.

    The JavaScript is exercised through node where node is available, and the
    test skips where it is not, so the suite still runs anywhere with no
    dependencies. Asserting on the source text instead would pass against a
    function nothing called.
    """

    NODE = shutil.which("node")
    API = ROOT / "mission-control-api" / "emily.js"
    DECK = ROOT / "mission-control-api" / "public" / "dashboard.html"

    def node_eval(self, body):
        r = subprocess.run([self.NODE, "-e", body], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def source_of(self, path, start, end):
        src = path.read_text()
        a = src.index(start)
        return src[a:src.index(end, a)]

    @unittest.skipUnless(NODE, "node is not installed")
    def test_a_missing_file_and_a_broken_one_are_told_apart(self):
        fn = self.source_of(self.API, "function readJsonOrWhy(", "\n// Belt and braces")
        out = self.node_eval(
            "const fs = require('fs'), os = require('os'), path = require('path');\n"
            + fn +
            "const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ej'));\n"
            "const gone = path.join(dir, 'gone.json');\n"
            "const bad = path.join(dir, 'bad.json');\n"
            "const good = path.join(dir, 'good.json');\n"
            "fs.writeFileSync(bad, '{\"title\": \"x\" \"product_type\": \"sticker\"}');\n"
            "fs.writeFileSync(good, '{\"title\": \"x\"}');\n"
            "const g = readJsonOrWhy(gone), b = readJsonOrWhy(bad), k = readJsonOrWhy(good);\n"
            "console.log(JSON.stringify({gone: [g.data, g.error === null],"
            " bad: [b.data, typeof b.error], good: [k.data.title, k.error === null]}));")
        got = json.loads(out)
        self.assertEqual(got["gone"], [None, True], "absent is not an error")
        self.assertEqual(got["bad"], [None, "string"], "malformed says so")
        self.assertEqual(got["good"], ["x", True])

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_card_prefers_the_drafter_s_reason_and_drops_a_stale_one(self):
        # Three rules at once: the drafter's own refusal wins over the API's
        # guess, the API's read failure is the fallback, and a build that HAS
        # a Printify draft shows nothing at all - a reason left over from an
        # earlier failed attempt describes a problem that is over.
        fn = self.source_of(self.DECK, "function blockedReason(", "\nasync function loadBuilds")
        out = self.node_eval(
            fn +
            "const drafter = {draft_blocked: {reason: 'the drafter said so'},"
            " listing_error: 'the api noticed'};\n"
            "const apionly = {listing_error: 'Unexpected token'};\n"
            "const stale = {printify: 'abc123', draft_blocked: {reason: 'old news'}};\n"
            "const sold = {published: true, draft_blocked: {reason: 'old news'}};\n"
            "const fine = {};\n"
            "console.log(JSON.stringify([drafter, apionly, stale, sold, fine]"
            ".map(blockedReason)));")
        got = json.loads(out)
        self.assertEqual(got[0], "the drafter said so")
        self.assertIn("Unexpected token", got[1])
        self.assertEqual(got[2], "", "a drafted build shows no stale reason")
        self.assertEqual(got[3], "", "nor does one that is on sale")
        self.assertEqual(got[4], "")

    def test_both_galleries_draw_it(self):
        # Recorded and not shown is the same as not recorded. Asserted on the
        # call inside the card template, not on the function's definition.
        for page in ("dashboard.html", "village.html"):
            html = (ROOT / "mission-control-api" / "public" / page).read_text()
            self.assertIn("blockedReason(b) ? `<div class=\"bwhy\">", html, page)
            self.assertIn(".bwhy{", html.replace(".build .bwhy{", ".bwhy{"), page)

    def test_the_unreadable_listing_gets_its_own_pill(self):
        # The title, the product type and the price all come from that file,
        # so when it will not parse the card goes blank in three places at
        # once and the pill is what explains all three.
        for page in ("dashboard.html", "village.html"):
            html = (ROOT / "mission-control-api" / "public" / page).read_text()
            pills = html.split("function buildPills(", 1)[1].split("\n}", 1)[0]
            self.assertIn("b.listing_error", pills, page)
            self.assertIn("listing unreadable", pills, page)

    def test_the_api_serves_both_fields(self):
        js = self.API.read_text()
        body = js.split("return {\n    slug,", 1)[1].split("\n}", 1)[0]
        self.assertIn("draft_blocked:", body)
        self.assertIn("listing_error:", body)


class AModelAskedForJsonWillEventuallyEmitNearlyJson(unittest.TestCase):
    """One missing comma cost a finished build.

    listing.json read `{"title": "Maple Leaf Pocket Sticker" "product_type":
    "sticker", ...}`. The verifier rejected it, the drafter never got past
    reading it, the gallery showed LOCAL ONLY with no reason, and Emily wrote
    the same broken file again on the retry. The artwork was fine and the copy
    was fine.

    So she stops being asked. She passes strings on a command line and
    json.dumps does the quoting, the escaping and the commas - none of which
    needed judgement, all of which she was being asked to get exactly right by
    hand every cycle forever. The same argument as the indicators, the ledger
    rows and the cycle arithmetic.
    """

    def setUp(self):
        self.m = load("emily_listing", "emily-listing.py")
        self.ev = load("emily_verify2", "emily-verify.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.d = self.root / "agents" / "emily" / "builds" / "a-build"
        self.d.mkdir(parents=True)
        self.m.ROOT = self.root

    def tearDown(self):
        self.tmp.cleanup()

    def run_listing(self, *extra, title="A Sticker", desc="Some copy.",
                    tags=("fall",)):
        argv = ["listing", "a-build", "--title", title, "--description", desc]
        for t in tags:
            argv += ["--tag", t]
        argv += list(extra)
        return self.invoke(argv)

    def invoke(self, argv):
        real = sys.argv
        sys.argv = ["emily-listing.py"] + argv
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.m.main()
        finally:
            sys.argv = real
        return code, out.getvalue() + err.getvalue()

    def listing(self):
        return json.loads((self.d / "listing.json").read_text())

    # --- the thing that broke ----------------------------------------------

    def test_the_copy_that_broke_the_build_round_trips(self):
        # Quotes, an apostrophe, an em dash, a newline and a backslash - every
        # character a hand-written JSON file gets wrong.
        title = 'Maple Leaf "Pocket" Sticker — Emily\'s Fall Drop'
        desc = 'Line one.\nLine two with a \\ backslash and "quotes".'
        code, _ = self.run_listing(title=title, desc=desc)
        self.assertEqual(code, 0)
        self.assertEqual(self.listing()["title"], title)
        self.assertEqual(self.listing()["description"], desc)

    def test_the_file_it_writes_always_parses(self):
        # The whole claim, made against the characters most likely to break it.
        for nasty in ('a "quoted" thing', "an apostrophe's", "a\nnewline",
                      "a\\backslash", "a\ttab", "emoji 🍁", 'trailing comma,',
                      '}{[]"'):
            with self.subTest(nasty=nasty):
                self.run_listing(title=nasty, desc=nasty, tags=(nasty[:20],))
                json.loads((self.d / "listing.json").read_text())

    # --- what it refuses ---------------------------------------------------

    def test_it_refuses_copy_the_verifier_would_reject(self):
        code, out = self.run_listing(tags=tuple(f"t{i}" for i in range(14)))
        self.assertEqual(code, 2)
        self.assertIn("13", out)
        self.assertFalse((self.d / "listing.json").exists(),
                         "a file that exists and fails looks like finished work")

    def test_a_title_too_long_for_etsy_is_refused(self):
        code, out = self.run_listing(title="x" * 200)
        self.assertEqual(code, 2)
        self.assertIn("140", out)

    def test_a_refusal_never_leaves_a_half_written_file(self):
        self.run_listing()                       # a good one first
        good = (self.d / "listing.json").read_text()
        self.run_listing(title="x" * 200)        # then a bad one
        self.assertEqual((self.d / "listing.json").read_text(), good)

    def test_the_rules_are_the_verifier_s_rules(self):
        # Not a second copy. Two spellings of "13 tags" drift, and the
        # direction they drift in is a writer that cheerfully produces what
        # the verifier then fails.
        src = (SCRIPTS / "emily-listing.py").read_text()
        self.assertIn("ev.listing_problems(", src)
        for number in ("13", "140", "20"):
            self.assertNotIn(f"= {number}", src,
                             f"{number} belongs to emily-verify.py")

    def test_a_listing_it_wrote_passes_the_verifier(self):
        # The round trip that matters: writer to verifier, no hands.
        self.run_listing(title="Maple Leaf Pocket Sticker",
                         desc="A small maple leaf.", tags=("fall", "autumn"))
        self.assertEqual(self.ev.listing_problems(self.listing()), [])

    # --- build.json --------------------------------------------------------

    def test_the_byte_counts_are_measured_not_reported(self):
        # She was asked for "every file produced and its real byte count",
        # which is an invitation to state a number nobody checked.
        (self.d / "design.png").write_bytes(b"x" * 4096)
        (self.d / "cover.png").write_bytes(b"y" * 1024)
        code, _ = self.invoke(["build", "a-build", "--art", "generated"])
        self.assertEqual(code, 0)
        build = json.loads((self.d / "build.json").read_text())
        self.assertEqual({f["name"]: f["bytes"] for f in build["files"]},
                         {"design.png": 4096, "cover.png": 1024})
        self.assertEqual(build["bytes"], 5120)

    def test_she_cannot_claim_a_printify_draft_exists(self):
        # ready_for_review means a draft exists and only emily-finish.py knows
        # whether one does. She set it by hand once on a build with no product.
        code, out = self.invoke(["build", "a-build", "--art", "generated",
                                 "--status", "ready_for_review"])
        self.assertEqual(code, 2)
        self.assertIn("emily-finish.py", out)
        self.assertFalse((self.d / "build.json").exists())

    def test_rewriting_the_build_keeps_what_code_recorded(self):
        # The finisher's reason, the Printify id and the prices read back from
        # Printify are not hers, and a rewrite must not lose them.
        (self.d / "build.json").write_text(json.dumps({
            "printify_product_id": "abc123", "published": True,
            "price_low": 6.99, "draft_blocked": {"reason": "old"},
            "status": "ready_local", "files": [{"name": "stale", "bytes": 1}]}))
        (self.d / "design.png").write_bytes(b"x" * 4096)
        self.invoke(["build", "a-build", "--art", "generated"])
        build = json.loads((self.d / "build.json").read_text())
        self.assertEqual(build["printify_product_id"], "abc123")
        self.assertEqual(build["published"], True)
        self.assertEqual(build["price_low"], 6.99)
        self.assertEqual(build["draft_blocked"], {"reason": "old"})
        self.assertEqual([f["name"] for f in build["files"]], ["design.png"],
                         "her own fields are replaced, not merged")

    def test_a_build_json_that_will_not_parse_is_simply_replaced(self):
        # The opposite of note_draft's rule, on purpose: this command is how
        # she fixes a broken file, so it must not refuse to write over one.
        (self.d / "build.json").write_text('{"status": "ready_local",,}')
        (self.d / "design.png").write_bytes(b"x" * 4096)
        code, _ = self.invoke(["build", "a-build", "--art", "placeholder"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.d / "build.json").read_text())["art_mode"],
                         "placeholder")

    def test_a_placeholder_build_is_called_unsellable_where_she_will_see_it(self):
        (self.d / "design.png").write_bytes(b"x" * 4096)
        _, out = self.invoke(["build", "a-build", "--art", "placeholder"])
        self.assertIn("NOT SELLABLE", out)

    def test_art_mode_cannot_be_invented(self):
        # It is whatever emily-assets.py reported, not a word she chooses.
        with self.assertRaises(SystemExit):
            self.invoke(["build", "a-build", "--art", "beautiful"])

    # --- being told about it -----------------------------------------------

    def test_a_missing_folder_lists_the_ones_that_exist(self):
        code, out = self.invoke(["build", "nope", "--art", "generated"])
        self.assertEqual(code, 1)
        self.assertIn("a-build", out)

    def test_her_instructions_tell_her_to_use_it(self):
        # A rule enforced in code and absent from the instructions is the
        # failure this repo keeps repeating. This is the mirror: a command
        # nothing tells her to run is a command she will not run.
        header = (ROOT / "agents" / "emily" /
                  "_emily-agents-header.md").read_text()
        self.assertIn("emily-listing.py listing builds/<slug>", header)
        self.assertIn("emily-listing.py build builds/<slug>", header)
        self.assertNotRegex(header, r"\*\*Write `builds/<slug>/build\.json`\*\*")
        self.assertIn("never by hand", header)


class TheListingRulesLiveInOnePlace(unittest.TestCase):
    """The title limit was documented for months and enforced nowhere.

    Emily's instructions said "title (≤140 chars)" from the beginning; nothing
    checked it. That is the same failure as a rule enforced in code and absent
    from the instructions, pointing the other way - and both end with the
    agent and the checker believing different things.
    """

    def setUp(self):
        self.m = load("emily_verify3", "emily-verify.py")

    def test_an_empty_listing_names_everything_it_needs(self):
        self.assertEqual(self.m.listing_problems({}),
                         ["listing.json is missing title, description, tags"])

    def test_a_good_listing_has_no_problems(self):
        self.assertEqual(self.m.listing_problems(
            {"title": "A Sticker", "description": "copy", "tags": ["fall"]}), [])

    def test_the_documented_title_limit_is_now_enforced(self):
        long = {"title": "x" * (self.m.MAX_TITLE + 1), "description": "d",
                "tags": ["t"]}
        self.assertTrue(any("title" in p for p in self.m.listing_problems(long)))
        ok = dict(long, title="x" * self.m.MAX_TITLE)
        self.assertEqual(self.m.listing_problems(ok), [])

    def test_etsy_s_tag_limits(self):
        many = {"title": "t", "description": "d",
                "tags": ["t"] * (self.m.MAX_TAGS + 1)}
        self.assertTrue(any(str(self.m.MAX_TAGS) in p
                            for p in self.m.listing_problems(many)))
        longtag = {"title": "t", "description": "d",
                   "tags": ["x" * (self.m.MAX_TAG_CHARS + 1)]}
        self.assertTrue(any("characters" in p
                            for p in self.m.listing_problems(longtag)))

    def test_tags_that_are_not_a_list_do_not_crash_the_verifier(self):
        # len("notalist") is 8, which used to read as eight tags.
        self.assertEqual(self.m.listing_problems(
            {"title": "t", "description": "d", "tags": "notalist"}),
            ["tags is not a list"])

    def test_something_that_is_not_an_object_at_all(self):
        self.assertTrue(self.m.listing_problems(["a", "list"]))


class AnEmptyTimerListIsNotABrokenInstall(unittest.TestCase):
    """`scripts/deploy.sh emily` ended with "0 timers listed."

    Emily has no timer and never has - she runs when the task dispatcher
    hands her a task - so `systemctl list-timers emily-*` correctly prints
    nothing. Underneath it the script printed "Deploy finished. Nothing above
    needs your attention." Both lines were true and the pair read as a broken
    install, which is why it got screenshotted and asked about.

    Absent and broken are different things. This is the same bug task.py's
    payload reader had to unlearn: rendering both as an empty line is how one
    gets mistaken for the other.

    And for an agent with no clock, the dispatcher IS its next wake. A dead
    task-dispatcher.service means it never runs again, and that was being
    reported as nothing at all - the one case where the empty list really
    would have meant something.
    """

    DEPLOY = SCRIPTS / "deploy.sh"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def wake_report(self, agents, dispatcher_up=True):
        """Run deploy.sh's wake_report against the real deploy/ folder.

        A stub systemctl rather than the machine's: the test must fail for the
        reason it names, not because the runner happens to have no timers.
        """
        (self.bin / "systemctl").write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            '  "list-timers --no-pager") echo "UNIT $3"; echo "1 timers listed." ;;\n'
            f'  "is-active --quiet") exit {0 if dispatcher_up else 1} ;;\n'
            "esac\n")
        (self.bin / "systemctl").chmod(0o755)
        script = (
            'ROOT="$1"; shift\n'
            'AGENTS=("$@")\n'
            "FAILED=0\n"
            "ok()   { printf '   -- %s\\n' \"$*\"; }\n"
            "warn() { printf '   !! %s\\n' \"$*\"; FAILED=1; }\n"
            # has_timers is a one-liner, so one range covers both functions.
            "source <(sed -n '/^has_timers()/,/^}/p' \"$ROOT/scripts/deploy.sh\")\n"
            "wake_report\n"
            'echo "FAILED=$FAILED"\n')
        runner = Path(self.tmp.name) / "run.sh"
        runner.write_text(script)
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        r = subprocess.run(["bash", str(runner), str(ROOT)] + list(agents),
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_emily_really_does_have_no_timer(self):
        # The premise, asserted rather than remembered. If she is ever given
        # one, the wording below becomes a lie and this says so first.
        self.assertEqual(list((ROOT / "deploy").glob("emily-*.timer")), [])
        self.assertTrue(list((ROOT / "deploy").glob("ace-*.timer")))

    def test_an_agent_without_a_timer_is_named_and_explained(self):
        out = self.wake_report(["emily"])
        self.assertIn("emily has no timer", out)
        self.assertIn("task dispatcher", out)

    def test_an_agent_with_a_timer_still_gets_its_timer_list(self):
        out = self.wake_report(["ace"])
        self.assertIn("1 timers listed", out)
        self.assertNotIn("has no timer", out)

    def test_a_dead_dispatcher_is_the_missing_next_wake(self):
        # This is the case the empty list really did mean something, and the
        # deploy said "nothing needs your attention" over the top of it.
        out = self.wake_report(["emily"], dispatcher_up=False)
        self.assertIn("!! task-dispatcher.service is NOT running", out)
        self.assertIn("FAILED=1", out, "a deploy that cannot wake an agent "
                                       "has finished WITH PROBLEMS")

    def test_a_live_dispatcher_is_said_out_loud_too(self):
        out = self.wake_report(["emily"])
        self.assertIn("task-dispatcher.service is running", out)
        self.assertIn("FAILED=0", out)

    def test_the_dispatcher_is_not_checked_for_agents_that_have_clocks(self):
        # Ace does not care whether the dispatcher is up, and a warning about
        # it in an ace-only deploy would be noise that trains you to ignore
        # the line that matters.
        out = self.wake_report(["ace"], dispatcher_up=False)
        self.assertNotIn("task-dispatcher", out)
        self.assertIn("FAILED=0", out)

    def test_a_mixed_deploy_reports_both(self):
        out = self.wake_report(["emily", "ace"])
        self.assertIn("emily has no timer", out)
        self.assertIn("1 timers listed", out)

    def test_installing_nothing_says_so(self):
        # The other silent section: "== emily: installing units" printed a
        # heading and no lines at all.
        src = self.DEPLOY.read_text()
        body = src.split('step "$AGENT: installing units"', 1)[1].split("\ndone", 1)[0]
        self.assertIn("installed=0", body)
        self.assertIn("has no units of its own", body)

    def test_the_script_still_parses(self):
        r = subprocess.run(["bash", "-n", str(self.DEPLOY)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class NoCredentialReachesAPublicRepo(unittest.TestCase):
    """.gitignore covered credentials.env and nothing that shadows it.

    nano writes credentials.env.save and credentials.env.save.1 when it is
    killed mid-edit, and nano.<pid>.save when it crashes. check-credentials.py
    opens with advice about pasting a long token into nano on a phone, so
    those files were always going to appear - and three of them were sitting
    in agents/emily/state/ on the droplet holding a live Printify token,
    matched by no ignore rule, one `git add -A` from a public repo.

    Nothing would have stopped it. The CI scan only ever read tests/fixtures/,
    and CI runs AFTER the push: by the time that job goes red the secret is
    already on GitHub. So the check runs here, in the suite, before anything
    leaves the machine.
    """

    SCRIPT = SCRIPTS / "check-secrets.py"

    def setUp(self):
        self.m = load("check_secrets", "check-secrets.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scan_file(self, name, body=""):
        p = self.d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        named, contented = self.m.scan([p])
        return named, contented

    # --- the check that matters --------------------------------------------

    def test_this_repo_is_clean_right_now(self):
        # Not a source assertion: it runs the scan over every tracked file.
        r = subprocess.run([sys.executable, str(self.SCRIPT)],
                           capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tracked file(s) scanned", r.stdout)

    def test_the_scan_really_looked_at_something(self):
        # A scan of zero files exits 0 and means nothing - the shape of the
        # credential-scan test that passed for a week on a missing file.
        r = subprocess.run([sys.executable, str(self.SCRIPT)],
                           capture_output=True, text=True, cwd=str(ROOT))
        m = re.search(r"(\d+) tracked file", r.stdout)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        self.assertGreater(int(m.group(1)), 50,
                           "git ls-files returned almost nothing")

    # --- what it catches ---------------------------------------------------

    def test_a_nano_backup_of_a_credentials_file_is_caught(self):
        named, _ = self.scan_file("credentials.env.save", "TOKEN=whatever\n")
        self.assertTrue(named)

    def test_so_is_the_numbered_one_nano_writes_next(self):
        named, _ = self.scan_file("credentials.env.save.1", "TOKEN=whatever\n")
        self.assertTrue(named)

    def test_an_empty_credentials_file_is_still_caught(self):
        # It is a file the next person will fill in, and they will not think
        # to check whether it is tracked.
        named, _ = self.scan_file("credentials.env", "")
        self.assertTrue(named)

    # The samples below are assembled at runtime rather than written out.
    # check-secrets.py scans every tracked file including this one, and a test
    # for a credential detector that contains a credential-shaped literal
    # fails its own check - which it did, on the first run. Building them from
    # halves keeps the claim true: no tracked file in this repo holds a
    # credential-shaped string, including the one testing for them.
    def jwt(self):
        return "eyJ" + "0eXAiOiJKV1Qi" + "LCJhbGciOiJSUzI1NiJ9" + "." + "abcdefghij"

    def sk_key(self):
        return "sk-" + "or-v1-0123456789abcdef0123456789"

    def long_run(self):
        return "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8S9t0"

    def test_a_jwt_is_caught_wherever_it_sits(self):
        # The Printify token is a JWT, and the broad rule misses short ones:
        # a JWT is dot-separated, so the longest run it sees is one segment.
        _, found = self.scan_file("notes.md", f"token: {self.jwt()}\n")
        self.assertTrue(found)

    def test_an_sk_key_is_caught_wherever_it_sits(self):
        _, found = self.scan_file("readme.md",
                                  f"OPENROUTER_API_KEY={self.sk_key()}\n")
        self.assertTrue(found)

    def test_a_long_token_in_a_state_directory_is_caught(self):
        _, found = self.scan_file("agents/emily/state/leftover.txt",
                                  f"PRINTIFY={self.long_run()}\n")
        self.assertTrue(found)

    def test_a_long_token_outside_one_is_not(self):
        # The same string in a source file is a long identifier, not a leak.
        _, found = self.scan_file("scripts/whatever.py",
                                  f"CONSTANT = '{self.long_run()}'\n")
        self.assertEqual(found, [])

    # --- what it must not cry wolf about ------------------------------------

    def test_the_example_file_is_not_a_finding(self):
        # It is tracked on purpose and holds key names with no values.
        # Flagging it would train everyone to ignore this check on its only
        # true positive.
        named, found = self.scan_file("credentials.env.example",
                                      "PRINTIFY_API_TOKEN=\n")
        self.assertEqual((named, found), ([], []))

    def test_a_long_test_method_name_is_not_a_credential(self):
        # The first version of the broad rule ran everywhere and matched
        # thousands of these, plus every npm integrity hash. A check that
        # cries wolf is one people learn to skip.
        named, found = self.scan_file(
            "tests/some_test.py",
            "def test_a_very_long_method_name_that_is_not_a_secret(self): pass\n")
        self.assertEqual((named, found), ([], []))

    def test_an_npm_integrity_hash_is_not_a_credential(self):
        named, found = self.scan_file(
            "mission-control-api/package-lock.json",
            '"integrity": "sha512-'
            'YmVjYXVzZSB0aGlzIGlzIHdoYXQgbnBtIHdyaXRlcyBldmVyeSBzaW5nbGUgdGltZQ=="\n')
        self.assertEqual((named, found), ([], []))

    def test_nothing_from_inside_the_file_is_ever_printed(self):
        # The report says where, never what. A check that pastes the secret
        # into a CI log has moved the problem rather than found it.
        secret = self.sk_key()
        p = self.d / "agents" / "x" / "state" / "leak.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"KEY={secret}\n")
        r = subprocess.run([sys.executable, str(self.SCRIPT), "--path", str(p)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(secret, r.stdout + r.stderr)
        self.assertIn("leak.txt", r.stdout)

    # --- the ignore rules ---------------------------------------------------

    def ignored(self, path):
        r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path],
                           capture_output=True, text=True)
        return r.returncode == 0

    def test_git_itself_refuses_the_backups_now(self):
        # check-secrets.py is the belt; these are the braces. A file git will
        # not stage cannot be pushed by accident in the first place.
        for path in ("agents/emily/state/credentials.env",
                     "agents/emily/state/credentials.env.save",
                     "agents/emily/state/credentials.env.save.1",
                     "agents/emily/state/nano.48611.save",
                     "agents/emily/state/.credentials.env.swp"):
            with self.subTest(path=path):
                self.assertTrue(self.ignored(path), f"{path} would be stageable")

    def test_the_example_is_still_stageable(self):
        self.assertFalse(self.ignored("agents/emily/state/credentials.env.example"))

    def test_real_work_is_not_swept_up_by_the_new_rules(self):
        # Broad ignore rules that swallow real files are how work disappears.
        for path in ("scripts/check-secrets.py", "tests/fixtures/systemd-show.txt",
                     "agents/emily/_emily-agents-header.md", "CLAUDE.md"):
            with self.subTest(path=path):
                self.assertFalse(self.ignored(path), f"{path} became invisible")

    def test_ci_runs_the_same_script_rather_than_its_own_grep(self):
        # It was an inline grep with its own copy of the patterns, over
        # tests/fixtures/ only. Two copies of "is this a credential" drift.
        wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
        self.assertIn("scripts/check-secrets.py", wf)
        self.assertNotIn("sk-[A-Za-z0-9_-]", wf)


class APhotographIsNotAPrintFile(unittest.TestCase):
    """design.png was a photograph of a sticker lying on a wooden desk.

    Wood grain, a ruler along the bottom, a highlight off the varnish - and it
    went to Printify as the artwork. Printify prints what it is given, so the
    sticker would have arrived with a desk printed on it.

    Two failures met:

    The prompt said "Maple leaf pocket sticker" and the model drew exactly
    that - a picture OF a sticker. It was never told the output is a print
    file rather than a photograph of a product.

    And nothing looked. knockout.py's refusals ran for apparel only, on the
    reasoning that a sticker is die-cut so an opaque square is fine. True, and
    it meant a sticker's file was never examined at all. The check that would
    have caught this was already written and was being skipped.

    Measured on the real image: 20% of its border is one colour. Flat art is
    100%. The signal was there the whole time.
    """

    def setUp(self):
        self.ko = load("knockout3", "knockout.py")
        self.ea = load("emily_assets3", "emily-assets.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, w, h, pixel):
        px = bytearray()
        for y in range(h):
            for x in range(w):
                px += bytes(pixel(x, y)) + b"\xff"
        return w, h, px

    def art(self, w=240, h=180):
        """Flat art: one shape, one even background."""
        return self.image(w, h, lambda x, y:
                          (178, 58, 38) if (60 < x < 180 and 40 < y < 140)
                          else (255, 255, 255))

    def photo(self, w=240, h=180):
        """A wood-grain desk. Continuous variation everywhere, including the
        border - which is exactly what the real design.png was."""
        rnd = random.Random(7)
        rows = [(150 + rnd.randrange(60), 100 + rnd.randrange(50),
                 50 + rnd.randrange(40)) for _ in range(h)]
        return self.image(w, h, lambda x, y: tuple(
            max(0, min(255, c + ((x * 7 + y * 13) % 17) - 8)) for c in rows[y]))

    # --- the check ---------------------------------------------------------

    def test_a_photograph_is_refused(self):
        w, h, px = self.photo()
        ok, facts, problem = self.ko.verdict(w, h, px)
        self.assertFalse(ok, facts)
        self.assertIn("photograph", problem)

    def test_flat_art_is_not_refused(self):
        # The other half. A check that refuses everything is not a check, it
        # is an outage.
        w, h, px = self.art()
        ok, facts, problem = self.ko.verdict(w, h, px)
        self.assertTrue(ok, f"{facts}\n{problem}")

    def test_the_border_is_what_tells_them_apart(self):
        # The premise, measured rather than remembered.
        aw, ah, apx = self.art()
        pw, ph, ppx = self.photo()
        art_agree = self.ko.border_agreement(
            aw, ah, apx, self.ko.background_colour(aw, ah, apx),
            self.ko.DEFAULT_TOLERANCE)
        photo_agree = self.ko.border_agreement(
            pw, ph, ppx, self.ko.background_colour(pw, ph, ppx),
            self.ko.DEFAULT_TOLERANCE)
        self.assertGreater(art_agree, 95.0)
        self.assertLess(photo_agree, self.ko.BORDER_MIN_PCT)

    def test_check_writes_nothing(self):
        src = self.d / "art.png"
        w, h, px = self.art()
        self.ko.encode(src, w, h, px)
        before = sorted(p.name for p in self.d.iterdir())
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(src), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), before)

    def test_check_exits_non_zero_on_a_photograph(self):
        src = self.d / "photo.png"
        w, h, px = self.photo()
        self.ko.encode(src, w, h, px)
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(src), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("photograph", r.stderr)

    def test_the_cutout_still_works_and_still_writes(self):
        # verdict() was lifted out of main(). The write path must be unchanged.
        src, dst = self.d / "art.png", self.d / "cut.png"
        w, h, px = self.art()
        self.ko.encode(src, w, h, px)
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(src), str(dst)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(dst.is_file())
        _w, _h, cut = self.ko.decode(dst)
        self.assertEqual(cut[3], 0, "the background corner should be transparent")

    # --- where it is wired in ----------------------------------------------

    def test_every_product_is_checked_not_only_apparel(self):
        # The whole bug. The check is before the needs_cutout branch, so no
        # product type can skip it.
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        check = body.index('"--check"')
        branch = body.index("if needs_cutout(cat):")
        self.assertLess(check, branch,
                        "the check must run before the apparel-only branch")

    def test_a_refused_file_stops_the_draft(self):
        # Printing a warning and uploading anyway would be worse than nothing.
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        # The window is the check and nothing else. Measured to "if
        # needs_cutout" rather than to "uploading": the apparel branch has a
        # sys.exit(1) of its own, so the wider window passed even with this
        # refusal deleted.
        after = body[body.index('"--check"'):]
        window = after[:after.index("if needs_cutout(cat):")]
        self.assertIn("sys.exit(1)", window)

    # --- the cause ----------------------------------------------------------

    def test_the_prompt_says_it_is_a_print_file(self):
        out = self.ea.directed("Maple leaf pocket sticker")
        self.assertIn("Maple leaf pocket sticker", out)
        for forbidden in ("photograph", "mockup", "ruler", "wood grain", "desk"):
            self.assertIn(forbidden, out.lower(),
                          f"the direction must rule out a {forbidden}")

    def test_the_direction_reaches_the_model(self):
        # Defined and not sent is the same as not defined. Asserted on the
        # request body, not on the constant existing.
        src = (SCRIPTS / "emily-assets.py").read_text()
        body = src.split("def generate(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("directed(prompt)", body)
        self.assertNotIn('"content": prompt', body)

    def test_the_agents_own_words_are_kept(self):
        # The direction is added to her idea, not instead of it.
        self.assertTrue(self.ea.directed("a fox in a scarf")
                        .startswith("a fox in a scarf"))


class ADieCutFollowsTransparency(unittest.TestCase):
    """The sticker came back as a disc with the whole square printed inside it.

    A cream band straight across, white in the corners, the leaf somewhere in
    the middle. needs_cutout() answered "a sticker is die-cut, so an opaque
    square is fine" and skipped the background removal - but the die cut has
    nothing to follow except the artwork's transparency. Given an opaque
    square it falls back to its own shape and prints everything inside it.

    It looked right in every thumbnail, because a thumbnail of a square IS a
    square. It only showed up in Printify's mockup library, on a laptop lid.

    The default inverts: cut the background out unless the catalogue entry
    says this product prints one. That is the safe way round - a product type
    added next year is opaque-by-accident under the old rule and
    transparent-by-default under this one.
    """

    def setUp(self):
        self.m = load("emily_printify4", "emily-printify.py")

    def test_a_sticker_needs_its_background_removed(self):
        # The exact entry that shipped the bug: sticker sizes, no garment
        # sizes for the old rule to infer from.
        self.assertTrue(self.m.needs_cutout(
            {"variant_titles": ['2" × 2"', '3" × 3"', '4" × 4"']}))

    def test_a_garment_still_does(self):
        self.assertTrue(self.m.needs_cutout(
            {"variant_titles": ["Black / S", "Black / M", "Black / 2XL"]}))

    def test_an_entry_that_says_nothing_gets_the_cutout(self):
        # Silence is the safe answer, not the old one.
        self.assertTrue(self.m.needs_cutout({}))
        self.assertTrue(self.m.needs_cutout(None))

    def test_a_product_that_really_prints_its_background_can_say_so(self):
        self.assertFalse(self.m.needs_cutout(
            {"cutout": False, "variant_titles": ['24" × 36"']}))

    def test_pick_records_that_choice_and_only_that_choice(self):
        # Absent means the default. Writing "cutout": true on every entry
        # would freeze today's default into every catalogue row, so changing
        # it later would change nothing.
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_pick(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('cat[a.product]["cutout"] = False', body)
        self.assertNotIn('"cutout": True', body)

    def test_the_flag_exists_to_set_it(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "emily-printify.py"),
                            "pick", "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--no-cutout", r.stdout)

    def test_the_draft_no_longer_calls_everything_apparel(self):
        # The message ran for garments only when it was written. It runs for
        # stickers now, and "sticker is apparel" is the kind of line that
        # makes someone distrust the rest of the output.
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("is apparel", body)

    def test_a_cutout_that_fails_still_stops_the_draft(self):
        # Unchanged, and worth pinning: uploading the opaque file instead is
        # exactly how this shipped.
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        after = body[body.index("if needs_cutout(cat):"):]
        self.assertIn("sys.exit(1)", after[:after.index("upload_from = cut")])


class OneNightIsNotACalibration(unittest.TestCase):
    """The "not enough to conclude" warning never fired.

    It was a floor on threshold CALLS - fewer than 50 - and one fixture
    produces about 120. So after a single Monday night game the report printed
    its table with no caveat at all, and a gap of -46% in the 90-99% row read
    like a finding.

    The unit of independence is a game, not a call. Every call inside one
    fixture shares a defence, a game script and the weather, so they move
    together: 120 calls from one night is one piece of evidence counted 120
    times. Counting calls counts the same evidence over and over.
    """

    def setUp(self):
        self.m = load("props_forecast5", "props-forecast.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.m.STORE = Path(self.tmp.name) / "forecasts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, games, per_game=30):
        # More than one market on purpose: the per-market table only draws
        # when there are two, so a single-market fixture made the test that
        # checks it is held back pass without the branch ever running.
        markets = ("receiving_yards", "receptions")
        out = []
        for g in range(games):
            for p in range(per_game):
                out.append({"event_id": f"evt{g}", "athlete_id": f"{g}-{p}",
                            "player": f"P{p}", "market": markets[p % 2],
                            "probabilities": {"40": 70.0, "50": 60.0,
                                              "60": 50.0, "70": 40.0},
                            "actual": 55.0, "graded_utc": "x"})
        return out

    def report(self, games, per_game=30):
        self.m.save({"forecasts": self.rows(games, per_game)})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.cmd_calibration()
        return out.getvalue()

    # --- the bug ------------------------------------------------------------

    def test_one_game_is_flagged_however_many_calls_it_makes(self):
        # 30 forecasts x 4 bars = 120 calls, comfortably past the old floor
        # of 50, and it said nothing.
        text = self.report(1)
        self.assertIn("120 threshold calls", text)
        self.assertIn("NOT A CALIBRATION YET", text)

    def test_the_warning_comes_before_the_table(self):
        # Under the table it arrived after the numbers had already persuaded
        # you. A caveat you read second is a caveat you read too late.
        text = self.report(1)
        self.assertLess(text.index("NOT A CALIBRATION YET"),
                        text.index("said"))

    def test_many_games_pass_without_the_warning(self):
        text = self.report(self.m.MIN_GAMES_TO_JUDGE, per_game=4)
        self.assertNotIn("NOT A CALIBRATION YET", text)

    def test_the_bar_is_games_not_calls(self):
        # Few games and many calls is flagged; many games and few calls is
        # not. That is the whole change, in one assertion pair.
        self.assertIsNotNone(self.m.too_thin_to_judge(1, 5000))
        self.assertIsNone(self.m.too_thin_to_judge(self.m.MIN_GAMES_TO_JUDGE, 40))

    def test_the_old_call_floor_is_gone(self):
        src = (SCRIPTS / "props-forecast.py").read_text()
        self.assertNotIn("n < 50", src)

    # --- counting the games -------------------------------------------------

    def test_games_are_counted_by_fixture_not_by_row(self):
        self.assertEqual(self.m.games_graded(self.rows(3, per_game=10)), 3)

    def test_an_ungraded_row_is_not_a_graded_game(self):
        rows = self.rows(2, per_game=2)
        for r in rows:
            r["actual"] = None
        rows[0]["actual"] = 55.0
        self.assertEqual(self.m.games_graded(rows), 1)

    def test_no_games_at_all(self):
        self.assertEqual(self.m.games_graded([]), 0)

    # --- what it holds back -------------------------------------------------

    def test_the_per_market_table_waits_for_enough_games(self):
        # Splitting 120 correlated calls seven ways produces rows nobody
        # should read, and a table on screen gets read.
        thin = self.report(1)
        self.assertIn("held back until", thin)
        self.assertNotIn("receiving_yards", thin,
                         "no per-market rows should have been drawn")

    def test_the_per_market_table_arrives_once_there_are_enough(self):
        text = self.report(self.m.MIN_GAMES_TO_JUDGE, per_game=4)
        self.assertIn("receiving_yards", text)
        self.assertIn("receptions", text)
        self.assertNotIn("held back until", text)

    # --- how it reads -------------------------------------------------------

    def test_it_does_not_say_one_games(self):
        # A caveat that reads as a template reads as boilerplate, and
        # boilerplate is what gets skipped.
        text = self.report(1)
        self.assertNotIn("(s)", text)
        self.assertIn("1 game graded", text)
        self.assertIn("1 piece of evidence", text)

    def test_plural_handles_both(self):
        self.assertEqual(self.m.plural(1, "game"), "1 game")
        self.assertEqual(self.m.plural(2, "game"), "2 games")
        self.assertEqual(self.m.plural(0, "game"), "0 games")
        self.assertEqual(self.m.plural(1, "fixture"), "1 fixture")


class TheEtsyKeyIsTwoValuesAndNeitherIsPrinted(unittest.TestCase):
    """Scout invents ideas out of the model's own head.

    An agent asked "what is selling on Etsy" with no data source produces a
    confident, detailed, plausible answer that is fiction - and fiction with
    numbers on it gets acted on, which makes it worse than no researcher at
    all. The Etsy API is the only source that gives code on this droplet real
    listings, prices, tags and favourite counts.

    Its key is two values joined by a colon, which is not written down
    anywhere obvious. The API says so itself when asked without one:

        {"error":"Invalid API key: should be in the format
         'keystring:shared_secret'."}

    One of those two values is a secret, so the rule this class mostly exists
    to hold is that it never appears in output - not in a message, not in an
    error, not in a log.
    """

    def setUp(self):
        self.m = load("etsy_probe", "etsy-probe.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cred = self.root / "agents" / "scout" / "state" / "credentials.env"
        self.cred.parent.mkdir(parents=True)
        self.m.ROOT = self.root
        self.m.CRED = self.cred
        self.was = os.environ.pop("ETSY_API_KEY", None)

    def tearDown(self):
        self.tmp.cleanup()
        if self.was is not None:
            os.environ["ETSY_API_KEY"] = self.was
        else:
            os.environ.pop("ETSY_API_KEY", None)

    # Built from halves so this file holds no credential-shaped literal -
    # check-secrets.py scans it like any other tracked file.
    def fake_key(self):
        return "abc123def456ghi789" + ":" + "0a1b2c3d4e"

    def test_a_missing_key_says_where_to_put_it(self):
        key, why = self.m.api_key()
        self.assertIsNone(key)
        self.assertIn("credentials.env", why)
        self.assertIn("keystring", why)
        self.assertIn("chmod 600", why)

    def test_it_is_read_from_scouts_credentials(self):
        self.cred.write_text(f"ETSY_API_KEY={self.fake_key()}\n")
        key, why = self.m.api_key()
        self.assertIsNone(why)
        self.assertEqual(key, self.fake_key())

    def test_the_environment_wins_over_the_file(self):
        # So a one-off test run does not need the file edited.
        self.cred.write_text("ETSY_API_KEY=from" + ":" + "thefile\n")
        os.environ["ETSY_API_KEY"] = self.fake_key()
        self.assertEqual(self.m.api_key()[0], self.fake_key())

    def test_one_value_instead_of_two_is_caught_here(self):
        # Etsy answers a colon-less key with the same 403 it gives a wrong
        # key, and those need completely different fixes. Cheaper to catch.
        self.cred.write_text("ETSY_API_KEY=onlythekeystring\n")
        key, why = self.m.api_key()
        self.assertIsNone(key)
        self.assertIn("colon", why)

    def test_the_credentials_parser_is_emilys_not_a_second_one(self):
        # Two parsers for one file format drift, and the first thing they
        # drift on is quoting.
        src = (SCRIPTS / "etsy-probe.py").read_text()
        self.assertIn("ea.read_env_file(", src)
        self.assertNotIn("def read_env_file", src)

    def fake_http(self, status=200, body=b'{"application_id": 1}'):
        """Stub urlopen, NOT call().

        The first version of the leak tests stubbed call() - which is exactly
        where a leak would happen - so a mutation that echoed the key into its
        error message passed, and so did one that printed the key on success.
        Two vacuous tests guarding the one rule this class exists for. The
        stub goes underneath the code being tested now, not over it.
        """
        import io as _io

        class Resp:
            # Etsy's real rate-limit headers, spelled the way it spells them.
            headers = {"x-limit-per-second": "10", "x-remaining-this-second": "9",
                       "x-limit-per-day": "10000", "x-remaining-today": "9997"}
            def read(self, n=None):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None, context=None):
            if status == 200:
                return Resp()
            raise urllib.error.HTTPError(
                req.full_url, status, "Forbidden", {},
                _io.BytesIO(b'{"error":"API key not found or not active."}'))
        return urlopen

    def leak_check(self, status, run):
        """Run something with a stubbed transport and return everything said."""
        self.cred.write_text(f"ETSY_API_KEY={self.fake_key()}\n")
        real = self.m.urllib.request.urlopen
        self.m.urllib.request.urlopen = self.fake_http(status)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                run()
        finally:
            self.m.urllib.request.urlopen = real
        return out.getvalue() + err.getvalue()

    def assert_no_secret(self, said):
        secret = self.fake_key().split(":")[1]
        self.assertNotIn(secret, said, "the shared secret reached the output")
        self.assertNotIn(self.fake_key(), said, "the whole key reached the output")

    def test_the_secret_does_not_reach_a_success_message(self):
        said = self.leak_check(200, lambda: self.m.cmd_ping(self.fake_key()))
        self.assertIn("ping ok", said)
        self.assert_no_secret(said)

    def test_the_secret_does_not_reach_an_error_message(self):
        # Through the REAL call(), so its exception handling is what runs.
        said = self.leak_check(403, lambda: self.m.cmd_ping(self.fake_key()))
        self.assertIn("403", said)
        self.assert_no_secret(said)

    def test_the_secret_does_not_reach_a_failed_search(self):
        said = self.leak_check(
            403, lambda: self.m.cmd_search(self.fake_key(), ["fall", "sticker"]))
        self.assert_no_secret(said)

    def test_the_rate_limit_budget_is_reported(self):
        # Etsy's limits are per API key and differ between applications, so a
        # constant in our code would be a guess about somebody else's account.
        # Every successful response carries the real budget; this is the only
        # honest source for it, and Scout will throttle on it later.
        said = self.leak_check(200, lambda: self.m.cmd_ping(self.fake_key()))
        self.assertIn("rate limit", said)
        self.assertIn("9997", said, "the remaining daily quota should show")

    def test_a_429_says_how_long_to_wait(self):
        # Etsy evaluates QPS first then QPD, and returns retry-after. Burning
        # the daily quota and not knowing for how long is a wasted day.
        src = (SCRIPTS / "etsy-probe.py").read_text()
        self.assertIn("retry-after", src)
        self.assertIn("429", src)

    def test_the_header_names_are_not_invented(self):
        # Read off Etsy's own rate-limit documentation, not guessed. A
        # misspelled header name reports nothing and looks like no limit.
        self.assertEqual(sorted(self.m.LIMIT_HEADERS),
                         ["x-limit-per-day", "x-limit-per-second",
                          "x-remaining-this-second", "x-remaining-today"])

    def test_the_key_does_go_in_the_header_though(self):
        # The other half: a test that only checks the key is absent everywhere
        # would pass on a probe that never sends it.
        seen = {}
        real = self.m.urllib.request.urlopen
        def capture(req, timeout=None, context=None):
            seen.update(req.headers)
            return self.fake_http(200)(req, timeout, context)
        self.m.urllib.request.urlopen = capture
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.m.call("/openapi-ping", self.fake_key())
        finally:
            self.m.urllib.request.urlopen = real
        self.assertEqual(seen.get("X-api-key"), self.fake_key())

    def test_a_403_explains_the_approval_step(self):
        # Etsy reviews new apps, so a brand new key 403s until approved. That
        # looks identical to a wrong key and wastes an afternoon.
        def denied(path, key):
            return None, {}, "HTTP 403: not active"
        real, self.m.call = self.m.call, denied
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.m.cmd_ping(self.fake_key()), 1)
        finally:
            self.m.call = real
        self.assertIn("APPROVED", err.getvalue())

    def test_it_reports_which_fields_are_missing(self):
        # The probe exists to answer "what is really in the payload". A
        # scoring rule built on a field that is not there is a rule that
        # silently scores nothing.
        def answer(path, key):
            return {"count": 2, "results": [
                {"title": "A Sticker", "tags": ["fall", "maple"],
                 "price": {"amount": 599, "divisor": 100, "currency_code": "USD"}},
                {"title": "B Sticker", "tags": ["fall"],
                 "price": {"amount": 799, "divisor": 100, "currency_code": "USD"}},
            ]}, {}, None
        real, self.m.call = self.m.call, answer
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                self.m.cmd_search(self.fake_key(), ["fall", "sticker"])
        finally:
            self.m.call = real
        text = out.getvalue()
        self.assertIn("NOT in the payload", text)
        self.assertIn("num_favorers", text)
        self.assertIn("5.99", text, "the price divisor must be applied")
        self.assertIn("fall (2)", text, "tag frequency is the point")


class GettingOneKeyOntoTheDroplet(unittest.TestCase):
    """Three attempts to save one credential produced three different failures.

    A credentials.env edited in nano, killed because the terminal had no
    working Ctrl key, leaving .save backups with a live token in them. Then a
    298-byte "API key" that was the key plus the NEXT command pasted after it,
    because a paste without a trailing newline joins onto whatever follows.
    Then the key in a screenshot, because the prompt echoed it.

    Every one of those was avoidable and none of them was the user's fault:
    they were handed an editor, then two commands to paste in sequence, then
    a visible prompt. So the editor is gone, there is nothing to substitute
    into a command line, the value is never echoed, and the file is validated
    before it is written rather than by whatever fails first afterwards.
    """

    def setUp(self):
        self.m = load("set_credential", "set-credential.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agents" / "scout" / "state").mkdir(parents=True)
        self.m.ROOT = self.root
        self.path = self.root / "agents" / "scout" / "state" / "credentials.env"

    def tearDown(self):
        self.tmp.cleanup()

    def good_key(self):
        return "yg4czc1wc6jkiyyzxg29wj9q" + ":" + "0a1b2c3d4e5f"

    def run_with(self, *answers, agent="scout", name="ETSY_API_KEY"):
        """Feed the prompts in order. It asks twice for a two-part key, and
        retries in-process rather than making the user re-run the command -
        which is when the clipboard stopped holding the key."""
        real_argv, real_getpass = sys.argv, self.m.getpass.getpass
        sys.argv = ["set-credential.py", agent, name]
        queue = list(answers)
        # The prompts themselves are recorded. getpass writes them to the
        # terminal rather than stdout, so asserting on captured output would
        # be testing this stub rather than what the user is asked.
        self.prompts = []
        def ask(prompt=""):
            self.prompts.append(prompt)
            return queue.pop(0) if queue else answers[-1]
        self.m.getpass.getpass = ask
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.m.main()
        finally:
            sys.argv, self.m.getpass.getpass = real_argv, real_getpass
        return code, out.getvalue() + err.getvalue()

    # --- the accident that actually happened --------------------------------

    def test_the_key_plus_the_next_command_is_refused(self):
        # Verbatim shape of the 298-byte file: the key, then the command that
        # was meant to run after it.
        mangled = ("yg4czc1wc6jkiyyzxg29wj9q: cd /root/ecosystem && python3 -c "
                   "\" import pathlib\"")
        code, said = self.run_with(mangled, mangled, mangled)
        self.assertEqual(code, 2)
        self.assertIn("COMMAND, not the key", said,
                      "it should name the clipboard, not just say 'a space'")
        self.assertFalse(self.path.exists(), "nothing should have been written")

    def test_the_command_in_the_clipboard_is_named_as_such(self):
        # Five real attempts failed this way. "it has a space in it" is true
        # and useless; "your clipboard still holds the command" is the fix.
        for pasted in ("cd /root/ecosystem && git pull",
                       "python3 scripts/set-credential.py scout ETSY_API_KEY",
                       "python3 scripts/etsy-probe.py ping"):
            with self.subTest(pasted=pasted):
                _code, said = self.run_with(pasted, pasted, pasted)
                self.assertIn("clipboard", said)

    def test_it_retries_without_re_running_the_command(self):
        # THE fix. Every retry used to mean pasting the command again, which
        # is exactly when the clipboard stopped holding the key.
        code, said = self.run_with(
            "python3 scripts/set-credential.py scout ETSY_API_KEY",
            "yg4czc1wc6jkiyyzxg29wj9q", "0a1b2c3d4e5f")
        self.assertEqual(code, 0, said)
        self.assertIn("no need to re-run the command", said)
        self.assertIn(f"ETSY_API_KEY={self.good_key()}", self.path.read_text())

    def test_it_gives_up_rather_than_looping_forever(self):
        code, said = self.run_with(*(["nonsense with spaces"] * 9))
        self.assertEqual(code, 2)
        self.assertIn("type it instead", said)
        self.assertIn("--show", said)

    def test_the_two_halves_are_asked_for_separately(self):
        # The colon is put in by code that cannot forget it. Asking someone to
        # assemble "keystring:shared_secret" from a web page on a tablet, one
        # clipboard at a time, failed five times out of five.
        code, said = self.run_with("yg4czc1wc6jkiyyzxg29wj9q", "0a1b2c3d4e5f")
        self.assertEqual(code, 0, said)
        self.assertEqual(len(self.prompts), 2, self.prompts)
        self.assertIn("keystring", self.prompts[0])
        self.assertIn("shared secret", self.prompts[1])
        self.assertIn(f"ETSY_API_KEY={self.good_key()}", self.path.read_text())

    def test_an_empty_half_is_refused(self):
        code, _said = self.run_with("yg4czc1wc6jkiyyzxg29wj9q", "", "", "")
        self.assertEqual(code, 2)
        self.assertFalse(self.path.exists())

    def test_an_empty_half_is_refused_with_no_shape_to_catch_it(self):
        # Guards the per-part check itself, not the shape check: for a
        # two-part key whose shape we do not know, the empty second half has
        # to be caught by problems("", second) or by nothing at all.
        self.m.PARTS["TWO_PART_TOKEN"] = ("first part", "second part")
        self.addCleanup(self.m.PARTS.pop, "TWO_PART_TOKEN", None)
        code, said = self.run_with("aaaaaaaaaaaa", "", "", "",
                                   name="TWO_PART_TOKEN")
        self.assertEqual(code, 2)
        self.assertIn("it is empty", said)
        self.assertFalse(self.path.exists())

    def test_the_whole_key_pasted_at_the_first_prompt_is_taken(self):
        # If it is already in hand, do not ask for a half of it.
        code, said = self.run_with(self.good_key())
        self.assertEqual(code, 0, said)
        self.assertEqual(len(self.prompts), 1,
                         "it should not ask for a second half it already has")
        self.assertIn(f"ETSY_API_KEY={self.good_key()}", self.path.read_text())

    def test_a_shell_operator_is_refused(self):
        # No spaces in these, deliberately. The first version of this test
        # used "key && echo hi", which the whitespace rule caught - so
        # deleting the shell-operator rule entirely still passed it.
        for mangled in ("abc12345:def67890&&echo", "abc12345:def67890||echo",
                        "abc12345:def$(whoami)", "abc12345:def`whoami`"):
            with self.subTest(value=mangled):
                code, said = self.run_with(mangled)
                self.assertEqual(code, 2, mangled)
                self.assertIn("shell operator", said)

    def test_nothing_is_written_when_it_refuses(self):
        # A half-written credentials.env is worse than none: the next command
        # fails somewhere else entirely.
        self.path.write_text("OPENROUTER_API_KEY=sk-untouched\n")
        before = self.path.read_text()
        self.run_with("nonsense with spaces")
        self.assertEqual(self.path.read_text(), before)

    # --- what it does when the value is fine --------------------------------

    def test_it_writes_the_key(self):
        code, _ = self.run_with(self.good_key())
        self.assertEqual(code, 0)
        self.assertIn(f"ETSY_API_KEY={self.good_key()}", self.path.read_text())

    def test_the_other_keys_in_the_file_survive(self):
        # credentials.env holds more than one secret. Rewriting the whole file
        # to change one line is how the others disappear.
        self.path.write_text("OPENROUTER_API_KEY=sk-keepme\n"
                             "PRINTIFY_API_TOKEN=keepmetoo\n")
        self.run_with(self.good_key())
        text = self.path.read_text()
        self.assertIn("OPENROUTER_API_KEY=sk-keepme", text)
        self.assertIn("PRINTIFY_API_TOKEN=keepmetoo", text)
        self.assertIn("ETSY_API_KEY=", text)

    def test_the_file_is_never_world_readable_even_for_an_instant(self):
        # Write-then-chmod leaves a window where the secret is on disk with
        # default permissions. It is created 600 before anything goes in it.
        self.run_with(self.good_key())
        self.assertEqual(oct(self.path.stat().st_mode)[-3:], "600")
        src = (SCRIPTS / "set-credential.py").read_text()
        body = src.split("def write(", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(body.index("touch(mode=0o600"), body.index("write_text"))

    # --- the rule that put a key in a screenshot ----------------------------

    def test_the_value_is_never_echoed(self):
        secret = self.good_key().split(":")[1]
        _code, said = self.run_with(self.good_key())
        self.assertNotIn(self.good_key(), said)
        self.assertNotIn(secret, said)

    def test_but_enough_is_shown_to_spot_a_truncated_paste(self):
        _code, said = self.run_with(self.good_key())
        self.assertIn("24 + 12 chars", said)

    def test_the_prompt_does_not_display_what_is_typed(self):
        # getpass, not input(). The last key reached a screenshot because the
        # prompt echoed it.
        src = (SCRIPTS / "set-credential.py").read_text()
        self.assertIn("getpass.getpass", src)
        # input() is reachable, but only behind --show, for someone typing it
        # by hand who needs to see what they typed.
        self.assertIn("asker = input if show else getpass.getpass", src)

    # --- being told what went wrong -----------------------------------------

    def test_an_unknown_agent_lists_the_real_ones(self):
        code, said = self.run_with(self.good_key(), agent="dennis")
        self.assertEqual(code, 1)
        self.assertIn("scout", said)

    def test_a_key_we_do_not_know_the_shape_of_is_still_accepted(self):
        # Guessing at the format of a credential nobody has seen is how a
        # real key gets refused at midnight.
        code, _ = self.run_with("some-other-token-that-is-long-enough",
                                name="SOME_OTHER_TOKEN")
        self.assertEqual(code, 0)


class GoogleSaysOrderAndWeReadItAsPopularity(unittest.TestCase):
    """trend-probe.py, against payloads captured from Google on 2026-09-22.

    Suggest returns a relevance score per completion, which reads like a
    measurement of how popular each one is. For 'fall sticker' the real
    payload scores them

        1250, 601, 600, 561, 560, 559, 558, 557, 556, 555, 554, 553, 552, ...

    Only the first three of those are measurements. The rest is a consecutive
    descending run - Google reporting the ORDER it chose, nothing more. A
    scorer that averaged them, or that preferred the 11th suggestion to the
    12th because 554 > 553, would be inventing differences out of a counter.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def chrome(self):
        return (FIXTURES / "google-suggest-chrome.json").read_text()

    def firefox(self):
        return (FIXTURES / "google-suggest-firefox.json").read_text()

    def test_the_real_chrome_payload_parses(self):
        words, rel, err = self.m.parse_suggest(self.chrome())
        self.assertIsNone(err)
        self.assertEqual(words[0], "fall stickers")
        self.assertEqual(len(words), 15)
        self.assertEqual(rel[:4], [1250, 601, 600, 561])

    def test_the_arity_differs_between_clients(self):
        # client=chrome returns five elements, client=firefox four. Reading
        # the metadata from a fixed index worked on whichever one was tried
        # first and broke on the other.
        c_words, c_rel, c_err = self.m.parse_suggest(self.chrome())
        f_words, f_rel, f_err = self.m.parse_suggest(self.firefox())
        self.assertIsNone(c_err)
        self.assertIsNone(f_err)
        self.assertEqual(len(json.loads(self.chrome())), 5)
        self.assertEqual(len(json.loads(self.firefox())), 4)
        # Both still yield suggestions; only chrome carries relevance.
        self.assertTrue(c_words and f_words)
        self.assertTrue(c_rel)
        self.assertEqual(f_rel, [])

    def test_the_filler_run_is_found_in_the_real_payload(self):
        _words, rel, _err = self.m.parse_suggest(self.chrome())
        self.assertEqual(self.m.filler_from(rel), 12)

    def test_distinct_scores_are_not_called_filler(self):
        # 1250, 601, 600 are real. Calling them rank-filler would throw away
        # the only measurement in the payload.
        self.assertEqual(self.m.filler_from([1250, 601, 600]), 0)
        self.assertEqual(self.m.filler_from([900, 800, 700, 600]), 0)

    def test_a_short_run_is_not_enough_to_call_it_filler(self):
        # Two consecutive scores happen by chance. Three in a row do not.
        self.assertEqual(self.m.filler_from([900, 601, 600]), 0)
        self.assertEqual(self.m.filler_from([900, 602, 601, 600]), 3)

    def test_an_all_filler_payload_is_all_filler(self):
        self.assertEqual(self.m.filler_from([605, 604, 603, 602, 601]), 5)

    def test_html_is_not_mistaken_for_a_payload(self):
        # The exact failure that killed Trends: a 429 that is an HTML page,
        # not JSON. A parser that returned [] here would read as "nobody
        # searches for this", which is the opposite of what happened.
        html = (FIXTURES / "google-trends-explore-429.html").read_text()
        words, rel, err = self.m.parse_suggest(html)
        self.assertEqual(words, [])
        self.assertIsNotNone(err)
        self.assertIn("not JSON", err)
        self.assertIn("429", err)

    def test_the_error_names_the_page_rather_than_pasting_it(self):
        html = (FIXTURES / "google-trends-explore-429.html").read_text()
        said = self.m.looks_like(html)
        self.assertIn("HTML page", said)
        self.assertIn("429", said)
        self.assertLess(len(said), 160, said)
        self.assertNotIn("<style", said)

    def test_valid_json_of_the_wrong_shape_is_refused(self):
        for text in ('{"suggestions": ["a"]}', '[]', '["fall sticker"]', 'null'):
            with self.subTest(text=text):
                words, _rel, err = self.m.parse_suggest(text)
                self.assertEqual(words, [])
                self.assertIsNotNone(err, text)

    def test_relevance_is_never_longer_than_the_suggestions(self):
        # Zipping two lists of different lengths pairs the wrong score with
        # the wrong phrase, silently.
        #
        # CONSTRUCTED, deliberately. The captured payload has 15 of each, so
        # asserting against it cannot fail however the truncation is broken -
        # the first version of this test was exactly that and a mutation
        # removing the truncation sailed past it. The imbalance has to be in
        # the input for the guard to be under test at all.
        payload = json.dumps(["q", ["one", "two"], [], {
            "google:suggestrelevance": [1250, 900, 800, 700, 600]}])
        words, rel, err = self.m.parse_suggest(payload)
        self.assertIsNone(err)
        self.assertEqual(words, ["one", "two"])
        self.assertEqual(rel, [1250, 900])

    def test_a_non_numeric_relevance_entry_is_dropped(self):
        payload = json.dumps(["q", ["one", "two"], [], {
            "google:suggestrelevance": [1250, None, "900"]}])
        _words, rel, err = self.m.parse_suggest(payload)
        self.assertIsNone(err)
        self.assertEqual(rel, [1250])

    def test_a_payload_with_no_relevance_at_all_is_fine(self):
        # client=firefox never sends it, and that is not an error.
        words, rel, err = self.m.parse_suggest(self.firefox())
        self.assertIsNone(err)
        self.assertEqual(len(words), 10)
        self.assertEqual(rel, [])
        self.assertEqual(self.m.filler_from(rel), 0)


class NotEveryCompletionIsACustomer(unittest.TestCase):
    """The intent labels in trend-probe.py, on the real completion list.

    'fall stickers near me', 'fall stickers hobby lobby' and 'fall stickers
    amazon' are people shopping somewhere that is not Etsy. 'fall stickers
    png' and 'fall stickers printable' want a file, which is a line we
    decided not to enter. 'fall sticker ideas' is not shopping at all.

    Seven of the fifteen completions for 'fall sticker' are someone trying to
    buy a physical thing. A researcher that counted all fifteen as demand
    would overstate it by more than double.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def test_the_real_completions_split_the_way_they_should(self):
        words, _rel, _err = self.m.parse_suggest(
            (FIXTURES / "google-suggest-chrome.json").read_text())
        labels = {w: self.m.intent_of(w)[0] for w in words}
        self.assertEqual(labels["fall stickers near me"], "offsite")
        self.assertEqual(labels["fall stickers hobby lobby"], "offsite")
        self.assertEqual(labels["fall stickers amazon"], "offsite")
        self.assertEqual(labels["fall stickers png"], "digital")
        self.assertEqual(labels["fall stickers printable"], "digital")
        self.assertEqual(labels["fall sticker ideas"], "research")
        self.assertIsNone(labels["fall sticker sheet"])
        self.assertIsNone(labels["fall sticker pack"])

    def test_exactly_seven_of_the_fifteen_are_buyers(self):
        words, _rel, _err = self.m.parse_suggest(
            (FIXTURES / "google-suggest-chrome.json").read_text())
        buyable = [w for w in words if self.m.intent_of(w)[0] is None]
        self.assertEqual(len(buyable), 7, buyable)

    def test_a_label_does_not_match_inside_a_longer_word(self):
        # \bfree\b, not 'free' anywhere: 'freezer magnet' and 'freeform' are
        # products, and substring matching would throw both away.
        for phrase in ("freezer magnet", "freeform sticker",
                       "digitally printed", "targeted ad sticker"):
            with self.subTest(phrase=phrase):
                label, _why = self.m.intent_of(phrase)
                self.assertIsNone(label, f"{phrase} -> {label}")

    def test_a_shop_that_is_also_a_word_is_only_a_shop_at_the_end(self):
        # Found by this suite, not on the droplet: 'target' matched anywhere
        # labelled 'target practice sticker' as someone shopping at Target,
        # which silently deletes a real product idea. A retailer query puts
        # the shop last.
        self.assertEqual(self.m.intent_of("fall stickers target")[0], "offsite")
        self.assertEqual(self.m.intent_of("fall stickers costco")[0], "offsite")
        for phrase in ("target practice sticker", "target practice",
                       "on target decal", "cvs receipt sticker joke"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.m.intent_of(phrase)[0], phrase)

    def test_a_plain_product_phrase_is_left_unlabelled(self):
        for phrase in ("fall sticker sheet", "pumpkin spice sticker",
                       "autumn vinyl sticker pack", "cozy fall tumbler"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.m.intent_of(phrase)[0], phrase)


class TrendingIsNewsNotAProductQueue(unittest.TestCase):
    """The Trends RSS feed, against the real US feed of 2026-09-22.

    This one DOES work from the droplet - 200 and real XML, unlike the
    explore endpoint. What it returns is 'taylor swift', 'united nations',
    'hayden panettiere': news spikes. Parsing it is easy; the trap is reading
    it as a list of things to print on a shirt.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def feed(self):
        return (FIXTURES / "google-trending-rss.xml").read_text()

    def test_the_namespaced_fields_are_read(self):
        # approx_traffic and news_item_title live in the ht: namespace.
        # findtext('approx_traffic') finds nothing and returns '' - which
        # reads as a trend with no volume rather than as a parser miss.
        items = self._parse()
        self.assertEqual(len(items), 10)
        phrase, traffic, heads = items[0]
        self.assertEqual(phrase, "taylor swift")
        self.assertEqual(traffic, "500+")
        self.assertTrue(heads and "Taylor Swift" in heads[0])

    def _parse(self):
        calls = {}

        def fake_fetch(url, timeout=20):
            calls["url"] = url
            return self.feed(), None

        real, self.m.fetch = self.m.fetch, fake_fetch
        try:
            items, err = self.m.trending("US")
        finally:
            self.m.fetch = real
        self.assertIsNone(err)
        self.calls = calls
        return items

    def test_every_item_carries_a_traffic_figure(self):
        for phrase, traffic, _heads in self._parse():
            with self.subTest(phrase=phrase):
                self.assertRegex(traffic, r"^\d[\d,]*\+$", phrase)

    def test_the_geo_reaches_the_url(self):
        self._parse()
        self.assertIn("geo=US", self.calls["url"])

    def test_html_where_rss_was_expected_is_an_error_not_an_empty_feed(self):
        html = (FIXTURES / "google-trends-explore-429.html").read_text()

        def fake_fetch(url, timeout=20):
            return html, None

        real, self.m.fetch = self.m.fetch, fake_fetch
        try:
            items, err = self.m.trending("US")
        finally:
            self.m.fetch = real
        self.assertEqual(items, [])
        self.assertIsNotNone(err)
        self.assertIn("not RSS", err)

    def test_a_transport_failure_is_passed_through(self):
        def fake_fetch(url, timeout=20):
            return None, "HTTP 429: an HTML page (Error 429)"

        real, self.m.fetch = self.m.fetch, fake_fetch
        try:
            items, err = self.m.trending("US")
        finally:
            self.m.fetch = real
        self.assertEqual(items, [])
        self.assertIn("429", err)


class ExpansionWidensTheKeyhole(unittest.TestCase):
    """trend-probe.py expand: one Suggest call sees ten completions, and
    there are far more than ten ways to finish a phrase. Asking once per
    letter is the only breadth this endpoint offers."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def fake_suggest(self, table):
        def _suggest(phrase, client="chrome"):
            return table.get(phrase, []), [], None
        return _suggest

    def test_it_asks_for_the_bare_phrase_as_well_as_every_letter(self):
        asked = []

        def _suggest(phrase, client="chrome"):
            asked.append(phrase)
            return [], [], None

        real, self.m.suggest = self.m.suggest, _suggest
        try:
            self.m.expand("fall sticker", pause=0)
        finally:
            self.m.suggest = real
        self.assertEqual(len(asked), 27, asked)
        self.assertEqual(asked[0], "fall sticker")
        self.assertEqual(asked[1], "fall sticker a")
        self.assertEqual(asked[-1], "fall sticker z")

    def test_the_best_rank_wins_when_a_phrase_appears_twice(self):
        # The same completion turns up under several letters. Keeping the
        # worst rank would bury a phrase that was top of another list.
        table = {"fall sticker": ["cozy fall sticker", "pumpkin sticker"],
                 "fall sticker c": ["a", "b", "cozy fall sticker"]}
        real, self.m.suggest = self.m.suggest, self.fake_suggest(table)
        try:
            found, errors = self.m.expand("fall sticker", letters="c", pause=0)
        finally:
            self.m.suggest = real
        self.assertEqual(errors, [])
        self.assertEqual(found["cozy fall sticker"], 0)

    def test_results_are_lowercased_and_deduped(self):
        table = {"fall sticker": ["Cozy Fall Sticker", "cozy fall sticker",
                                  "  COZY FALL STICKER  "]}
        real, self.m.suggest = self.m.suggest, self.fake_suggest(table)
        try:
            found, _errors = self.m.expand("fall sticker", letters="", pause=0)
        finally:
            self.m.suggest = real
        self.assertEqual(list(found), ["cozy fall sticker"])

    def test_one_failed_letter_does_not_lose_the_other_twenty_six(self):
        # A single 429 in the middle of a 27-request sweep used to be the
        # kind of thing that raised and threw away everything collected.
        def _suggest(phrase, client="chrome"):
            if phrase.endswith(" m"):
                return [], [], "HTTP 429: an HTML page (Error 429)"
            return [f"{phrase} thing"], [], None

        real, self.m.suggest = self.m.suggest, _suggest
        try:
            found, errors = self.m.expand("fall sticker", pause=0)
        finally:
            self.m.suggest = real
        self.assertEqual(len(errors), 1)
        self.assertIn("429", errors[0])
        self.assertEqual(len(found), 26)


class SomebodyElsesPropertyReachesTheArtGenerator(unittest.TestCase):
    """ip-check.py.

    The first real expansion of 'fall sticker' produced 229 searches and put
    'fall sticker pikmin' and 'fall sticker decor pikmin' in a bucket labelled
    BUYING. Pikmin is Nintendo's. The chain from there is automatic - Scout
    proposes, Emily generates the art and drafts the listing - and nothing in
    between was looking at trademarks.

    Sixteen of that sweep's 229 were somebody else's property: Pikmin,
    Fallout, Snoopy, Starbucks, and six Roblox games that never say Roblox.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("ip_check", SCRIPTS / "ip-check.py")

    def test_the_phrases_that_started_this_are_blocked(self):
        for phrase in ("fall sticker pikmin", "fall sticker decor pikmin",
                       "fallout decal", "fall snoopy sticker",
                       "fall sticker starbucks"):
            with self.subTest(phrase=phrase):
                tier, _what, why = self.m.risky(phrase)
                self.assertEqual(tier, "blocked", f"{phrase}: {why}")

    def test_a_game_is_blocked_by_the_name_people_actually_type(self):
        # 'fall decals bloxburg' and 'fall decal codes berry avenue' are
        # Roblox searches that never contain the word Roblox. Listing the
        # publisher alone would have missed all six in the real sweep.
        for phrase in ("fall decals bloxburg", "fall decals berry avenue",
                       "fall decal codes berry avenue", "fall tree decal bloxburg"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.m.risky(phrase)[0], "blocked", phrase)

    def test_an_ordinary_word_is_flagged_not_refused(self):
        # 'frozen hot chocolate sticker' is a product. Blocking it outright
        # is the 'target practice sticker' mistake again.
        tier, _what, _why = self.m.risky("frozen hot chocolate sticker")
        self.assertEqual(tier, "check")
        self.assertEqual(self.m.risky("friends are like autumn leaves")[0], "check")

    def test_a_plain_product_phrase_passes(self):
        for phrase in ("cozy fall sweatshirt", "fall leaf sticker",
                       "pumpkin spice tumbler", "autumn vinyl sticker pack"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.m.risky(phrase)[0], phrase)

    def test_the_phrasings_sellers_believe_are_a_defence(self):
        for phrase in ("stanley cup dupe", "sticker inspired by hogwarts",
                       "official fall sticker", "licensed autumn decal"):
            with self.subTest(phrase=phrase):
                tier, what, _why = self.m.risky(phrase)
                self.assertEqual(tier, "blocked", phrase)

    def test_phrasing_is_checked_before_the_name_list(self):
        # 'inspired by' has to win, because the whole point is that it is a
        # problem whatever name follows it - including one not on the list.
        _tier, what, _why = self.m.risky("sticker inspired by a nameless thing")
        self.assertEqual(what, "trademark phrasing")

    def test_the_cli_exit_code_says_how_bad_it_is(self):
        def run(*args):
            r = subprocess.run([sys.executable, str(SCRIPTS / "ip-check.py"), *args],
                               capture_output=True, text=True)
            return r.returncode, r.stdout
        self.assertEqual(run("fall leaf sticker")[0], 0)
        self.assertEqual(run("frozen hot chocolate sticker")[0], 1)
        self.assertEqual(run("fall sticker pikmin")[0], 2)
        # The worst of several wins, so a batch cannot be passed by its
        # innocent members.
        self.assertEqual(run("fall leaf sticker", "fall sticker pikmin")[0], 2)

    def test_it_never_claims_to_be_complete(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "ip-check.py"),
                            "fall sticker pikmin"], capture_output=True, text=True)
        self.assertIn("not a complete list", r.stdout)
        self.assertIn("not been cleared", r.stdout)


class ASynonymIsNotDrift(unittest.TestCase):
    """trend-probe.py's drift check, and the synonym map that keeps it honest.

    Expanding 'fall sticker' returned 'fall vinyl decals' and 'fall leaf
    decals'. A decal IS a sticker. Expanding 'cozy fall sweatshirt' returned
    pullover, sweater, hoodie and crewneck - the four best results in the run.

    A literal head-noun match called all of those off-topic. Drift detection
    without a synonym map does not merely fail to help; it deletes the
    findings you ran the sweep for.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def test_the_four_best_sweatshirt_results_are_not_drift(self):
        for phrase in ("cozy fall pullover", "cozy fall sweater",
                       "cozy fall hoodie", "cozy fall crewneck",
                       "cozy fall crewnecks", "cozy fall knit sweater"):
            with self.subTest(phrase=phrase):
                self.assertFalse(self.m.drifted("cozy fall sweatshirt", phrase),
                                 phrase)

    def test_a_decal_is_a_sticker(self):
        for phrase in ("fall vinyl decals", "fall leaf decals",
                       "fall shirt decals", "fall mirror decals"):
            with self.subTest(phrase=phrase):
                self.assertFalse(self.m.drifted("fall sticker", phrase), phrase)

    def test_the_real_drifters_are_still_caught(self):
        # Every one of these came back from the live sweep.
        self.assertTrue(self.m.drifted("cozy fall sweatshirt", "cozy fall desserts"))
        self.assertTrue(self.m.drifted("cozy fall sweatshirt", "cozy fall snacks"))
        self.assertTrue(self.m.drifted("fall sticker", "fall autumn quotes"))
        self.assertTrue(self.m.drifted("fall sticker", "fall sayings for signs"))

    def test_a_label_is_not_a_sticker(self):
        # It looks like one, and admitting it pulls in a hospital sign, a
        # music company and a clothing brand - all real results.
        for phrase in ("fall risk label", "fall records label",
                       "fall hazard label", "fall the label bags"):
            with self.subTest(phrase=phrase):
                self.assertTrue(self.m.drifted("fall sticker", phrase), phrase)

    def test_plurals_do_not_drift(self):
        self.assertFalse(self.m.drifted("fall sticker", "fall stickers"))
        self.assertFalse(self.m.drifted("fall stickers", "fall sticker"))
        self.assertFalse(self.m.drifted("fall magnet", "fall magnets"))

    def test_the_stemmer_is_crude_but_not_wrong(self):
        self.assertEqual(self.m.stem("stickers"), "sticker")
        self.assertEqual(self.m.stem("decals"), "decal")
        # Short words must survive: 'bus' -> 'bu' would be a silent disaster.
        for word in ("mug", "tee", "bus", "gas", "pins"):
            with self.subTest(word=word):
                self.assertGreaterEqual(len(self.m.stem(word)), 3, word)

    def test_a_hyphenated_head_still_matches(self):
        self.assertFalse(self.m.drifted("fall shirt", "fall t-shirt"))

    def test_an_empty_seed_never_drifts(self):
        self.assertFalse(self.m.drifted("", "anything at all"))


class TheOffsiteBucketWasTheBestSignalInIt(unittest.TestCase):
    """Somebody typing 'fall window stickers near me' has decided to buy,
    knows the product, and has not thought of Etsy. The first version of
    trend-probe.py put fifteen such searches in a bucket and ignored them."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def test_the_shop_is_stripped_off_the_end(self):
        self.assertEqual(self.m.without_retailer("fall nail stickers amazon"),
                         "fall nail stickers")
        self.assertEqual(self.m.without_retailer("fall window stickers near me"),
                         "fall window stickers")
        self.assertEqual(self.m.without_retailer("fall stickers at walmart"),
                         "fall stickers")
        self.assertEqual(self.m.without_retailer("fall stickers hobby lobby"),
                         "fall stickers")

    def test_a_phrase_with_no_shop_in_it_returns_nothing(self):
        # None, not the phrase itself - a caller that got the phrase back
        # would count every search as retail demand.
        for phrase in ("fall leaf sticker", "cozy fall hoodie",
                       "target practice sticker"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.m.without_retailer(phrase), phrase)

    def test_the_shop_is_only_stripped_from_the_end(self):
        # 'amazon rainforest sticker' is a product, not a shopping trip.
        self.assertIsNone(self.m.without_retailer("amazon rainforest sticker"))

    def test_shops_are_counted_per_product(self):
        found = {"fall nail stickers amazon": 0, "fall nail stickers near me": 1,
                 "fall window stickers amazon": 2, "fall leaf sticker": 3}
        hunted = self.m.retail_demand(found)
        self.assertEqual(hunted["fall nail stickers"], {"amazon", "near me"})
        self.assertEqual(hunted["fall window stickers"], {"amazon"})
        self.assertNotIn("fall leaf sticker", hunted)

    def test_the_real_sweep_finds_the_four_it_found(self):
        found = {p: 0 for p in (
            "fall stickers hobby lobby", "fall stickers on amazon",
            "fall stickers walmart", "fall stickers amazon",
            "fall stickers michaels", "fall stickers near me",
            "fall leaf stickers near me", "fall stickers target",
            "fall leaf stickers michaels", "fall stickers at walmart",
            "fall stickers dollar tree", "fall window stickers amazon",
            "fall nail stickers amazon", "fall window stickers near me",
            "fall nail stickers near me")}
        hunted = self.m.retail_demand(found)
        multi = {p for p, shops in hunted.items() if len(shops) > 1}
        self.assertEqual(multi, {"fall stickers", "fall leaf stickers",
                                 "fall nail stickers", "fall window stickers"})


class OneBucketPerCompletionWorstNewsFirst(unittest.TestCase):
    """bucket() in trend-probe.py. A completion that is both infringing and
    off-topic has to report as infringing: that is the one with a
    consequence."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def test_property_beats_everything_else(self):
        # 'fall decals bloxburg' is a Roblox search - offsite-ish, arguably
        # drift, and definitely not ours to print.
        self.assertEqual(self.m.bucket("fall sticker", "fall decals bloxburg")[0],
                         "blocked")
        self.assertEqual(self.m.bucket("fall sticker", "pikmin sticker png")[0],
                         "blocked")

    def test_drift_beats_intent(self):
        self.assertEqual(self.m.bucket("fall sticker", "fall risk label")[0],
                         "drift")

    def test_a_named_intent_beats_buying(self):
        self.assertEqual(
            self.m.bucket("cozy fall sweatshirt", "cozy fall sweater crochet pattern")[0],
            "digital")
        self.assertEqual(self.m.bucket("fall sticker", "fall stickers amazon")[0],
                         "offsite")

    def test_an_ordinary_product_phrase_lands_in_buying(self):
        for phrase in ("fall leaf sticker", "cozy fall hoodie",
                       "fall sticker sheet"):
            with self.subTest(phrase=phrase):
                seed = "cozy fall sweatshirt" if "hoodie" in phrase else "fall sticker"
                self.assertEqual(self.m.bucket(seed, phrase)[0], "buying", phrase)

    def test_a_check_word_is_its_own_bucket_not_buying(self):
        name, what = self.m.bucket("fall sticker", "frozen sticker")
        self.assertEqual(name, "check")
        self.assertTrue(what)

    def test_the_new_digital_rules_catch_what_the_sweep_missed(self):
        # All three sat in BUYING on the first real run.
        for phrase in ("cozy fall sweater crochet pattern", "fall sticker clipart",
                       "fall stickers goodnotes"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.m.intent_of(phrase)[0], "digital", phrase)


class AFieldThatIsNotTheShapeItShouldBe(unittest.TestCase):
    """market-scan.py reads four of Etsy's 59 fields and scores on them.

    Each of these produces a confident, wrong number rather than an error if
    it is not checked: a timestamp in milliseconds makes every listing three
    weeks old and every favs/day figure enormous; a null view count makes
    favs/view a division by zero or a TypeError depending on where it lands;
    a missing divisor turns 899 minor units into a price of 899 dollars.

    So every component is dropped rather than guessed, and the output names
    what it dropped.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    NOW = 1758500000.0          # a fixed clock, so these never rot

    def row(self, **over):
        r = {"original_creation_timestamp": int(self.NOW - 100 * 86400),
             "num_favorers": 50, "views": 1000,
             "price": {"amount": 899, "divisor": 100, "currency_code": "USD"}}
        r.update(over)
        return r

    def test_the_baseline_row_computes(self):
        r = self.row()
        self.assertAlmostEqual(self.m.age_days(r, self.NOW), 100, places=3)
        self.assertAlmostEqual(self.m.favs_per_day(r, self.NOW), 0.5, places=3)
        self.assertAlmostEqual(self.m.pull(r), 0.05, places=6)
        self.assertAlmostEqual(self.m.price_of(r), 8.99, places=2)

    def test_a_millisecond_timestamp_is_refused(self):
        # The one that would do real damage: it is a plausible integer, it is
        # in the right field, and it makes a five-year-old listing look three
        # weeks old.
        r = self.row(original_creation_timestamp=int((self.NOW - 100 * 86400) * 1000))
        self.assertIsNone(self.m.age_days(r, self.NOW))
        self.assertIsNone(self.m.favs_per_day(r, self.NOW))

    def test_a_timestamp_from_before_etsy_existed_is_refused(self):
        for ts in (0, -1, 1104537599):
            with self.subTest(ts=ts):
                self.assertIsNone(
                    self.m.age_days(self.row(original_creation_timestamp=ts),
                                    self.NOW))

    def test_a_future_timestamp_is_refused(self):
        r = self.row(original_creation_timestamp=int(self.NOW + 10 * 86400))
        self.assertIsNone(self.m.age_days(r, self.NOW))

    def test_a_listing_posted_today_does_not_divide_by_zero(self):
        # age is floored at one day. Without it, a listing minutes old with
        # one favourite scores hundreds of favourites per day and tops every
        # ranking it appears in.
        r = self.row(original_creation_timestamp=int(self.NOW - 60), num_favorers=1)
        self.assertEqual(self.m.age_days(r, self.NOW), 1.0)
        self.assertEqual(self.m.favs_per_day(r, self.NOW), 1.0)

    def test_the_fallback_timestamp_field_is_used(self):
        r = self.row()
        del r["original_creation_timestamp"]
        r["creation_timestamp"] = int(self.NOW - 50 * 86400)
        self.assertAlmostEqual(self.m.age_days(r, self.NOW), 50, places=3)

    def test_a_missing_or_null_field_drops_the_component(self):
        for field in ("num_favorers", "views", "price",
                      "original_creation_timestamp"):
            for value in (None, "", {}):
                with self.subTest(field=field, value=value):
                    r = self.row(**{field: value})
                    # None of these may raise, and none may return a number.
                    self.m.age_days(r, self.NOW)
                    self.m.favs_per_day(r, self.NOW)
                    self.m.pull(r)
                    self.m.price_of(r)

    def test_a_boolean_timestamp_is_refused_by_the_window(self):
        # True == 1 in Python, so a bool passes an isinstance int check. It
        # does NOT pass the Etsy-launched-in-2005 window, which is the check
        # that owns this - a separate isinstance(ts, bool) guard here was
        # unreachable behind it and no test could fail on it.
        self.assertIsNone(self.m.age_days(
            self.row(original_creation_timestamp=True), self.NOW))
        self.assertIsNone(self.m.favs_per_day(
            self.row(original_creation_timestamp=False), self.NOW))

    def test_a_boolean_is_not_a_number(self):
        # True == 1 in Python, so a bool sails through an isinstance int
        # check and scores as one favourite.
        self.assertIsNone(self.m.favs_per_day(self.row(num_favorers=True),
                                              self.NOW))
        self.assertIsNone(self.m.pull(self.row(views=True)))
        self.assertIsNone(self.m.price_of(
            self.row(price={"amount": True, "divisor": 100})))

    def test_zero_views_is_not_a_division(self):
        self.assertIsNone(self.m.pull(self.row(views=0)))

    def test_zero_favourites_is_a_real_answer_not_a_missing_one(self):
        # 'fall sticker' returned a listing with favs=0. That is the finding,
        # not a gap - dropping it would delete the evidence of saturation.
        self.assertEqual(self.m.favs_per_day(self.row(num_favorers=0), self.NOW), 0.0)
        self.assertEqual(self.m.pull(self.row(num_favorers=0)), 0.0)

    def test_a_price_with_no_divisor_is_refused(self):
        for price in ({"amount": 899}, {"amount": 899, "divisor": 0},
                      {"amount": 899, "divisor": None}, {"divisor": 100}):
            with self.subTest(price=price):
                self.assertIsNone(self.m.price_of(self.row(price=price)))

    def test_a_negative_count_is_refused(self):
        self.assertIsNone(self.m.favs_per_day(self.row(num_favorers=-1), self.NOW))
        self.assertIsNone(self.m.pull(self.row(views=-5)))


class ASaturatedPhraseScoresLikeOne(unittest.TestCase):
    """The arithmetic on top of those fields, and the refusal to invent a
    verdict when a component is missing."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    NOW = 1758500000.0

    def payload(self, count, rows):
        return {"count": count, "results": rows}

    def row(self, favs, views, days, cents=899):
        return {"original_creation_timestamp": int(self.NOW - days * 86400),
                "num_favorers": favs, "views": views,
                "price": {"amount": cents, "divisor": 100}}

    def test_the_real_fall_sticker_shape_reads_as_saturated(self):
        # 109,099 listings whose top results have 1, 14 and 0 favourites -
        # the numbers from the live probe.
        m = self.m.measure(self.payload(109099, [
            self.row(1, 400, 300), self.row(14, 900, 300), self.row(0, 200, 300)]),
            self.NOW)
        self.assertEqual(m["supply"], 109099)
        self.assertLess(m["heat"], 0.01)
        self.assertLess(self.m.opportunity(m), 0.002)

    def test_a_thin_lively_phrase_outscores_it(self):
        thin = self.m.measure(self.payload(900, [
            self.row(30, 300, 60), self.row(45, 500, 60)]), self.NOW)
        fat = self.m.measure(self.payload(109099, [
            self.row(1, 400, 300), self.row(0, 200, 300)]), self.NOW)
        self.assertGreater(self.m.opportunity(thin), self.m.opportunity(fat))

    def test_the_divisor_is_logarithmic_not_linear(self):
        # A linear divisor would make any narrow phrase win on arithmetic
        # alone. Ten times the competition must cost less than ten times the
        # score.
        a = {"heat": 1.0, "supply": 1000}
        b = {"heat": 1.0, "supply": 10000}
        self.assertLess(self.m.opportunity(b), self.m.opportunity(a))
        self.assertGreater(self.m.opportunity(b), self.m.opportunity(a) / 10)

    def test_no_score_without_both_halves(self):
        self.assertIsNone(self.m.opportunity({"heat": None, "supply": 1000}))
        self.assertIsNone(self.m.opportunity({"heat": 1.0, "supply": None}))
        self.assertIsNone(self.m.opportunity({"heat": 1.0, "supply": 0}))

    def test_what_was_dropped_is_named(self):
        m = self.m.measure(self.payload(500, [
            {"num_favorers": 5, "views": None, "price": None}]), self.NOW)
        self.assertIsNone(m["heat"])
        self.assertIsNone(m["pull"])
        self.assertIsNone(m["price"])
        self.assertTrue(m["dropped"])
        self.assertIn("views", " ".join(m["dropped"]))

    def test_an_empty_result_set_does_not_crash(self):
        m = self.m.measure(self.payload(0, []), self.NOW)
        self.assertEqual(m["returned"], 0)
        self.assertIsNone(self.m.opportunity(m))

    def test_one_bad_row_does_not_poison_the_median(self):
        # The median is taken over what computed, not over zeros substituted
        # for what did not.
        m = self.m.measure(self.payload(500, [
            self.row(30, 300, 60), self.row(30, 300, 60),
            {"num_favorers": None, "views": None}]), self.NOW)
        self.assertAlmostEqual(m["heat"], 0.5, places=3)


class TheScanSpendsItsEtsyCallsOnTheBestEvidence(unittest.TestCase):
    """pick() in market-scan.py. Etsy allows 5 calls a second and 5,000 a
    day, and an expansion produces 229 candidates. Which twelve get asked
    about is the whole question."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    def test_multi_shop_phrases_go_first(self):
        found = {"fall nail stickers amazon": 5, "fall nail stickers near me": 6,
                 "fall sticker sheet": 0, "fall sticker pack": 1}
        picked = self.m.pick("fall sticker", found)
        self.assertEqual(picked[0], "fall nail stickers")

    def test_the_rest_is_filled_by_rank(self):
        found = {"fall sticker sheet": 0, "fall sticker pack": 1,
                 "fall sticker roll": 2}
        self.assertEqual(self.m.pick("fall sticker", found),
                         ["fall sticker sheet", "fall sticker pack",
                          "fall sticker roll"])

    def test_infringing_and_off_topic_phrases_are_never_asked_about(self):
        found = {"fall sticker pikmin": 0, "fall risk label": 1,
                 "fall stickers png": 2, "fall sticker sheet": 3}
        self.assertEqual(self.m.pick("fall sticker", found),
                         ["fall sticker sheet"])

    def test_the_budget_is_respected(self):
        # Distinct WORDS, not numbers. The first version numbered them
        # ('fall sticker 00'), and a digit is not a word - once candidates
        # were keyed by stem they all collapsed to one key and the test was
        # asserting against a degenerate fixture.
        words = ("sheet pack roll set book kit tin bundle strip label card "
                 "page album box folder pouch case wrap tab dot").split()
        found = {f"fall sticker {w}": i for i, w in enumerate(words)}
        self.assertGreater(len(found), self.m.CANDIDATES)
        self.assertEqual(len(self.m.pick("fall sticker", found)),
                         self.m.CANDIDATES)

    def test_no_phrase_is_asked_about_twice(self):
        # A multi-shop phrase can also be a plain completion. Asking twice
        # spends a call to learn nothing.
        found = {"fall nail stickers": 0, "fall nail stickers amazon": 1,
                 "fall nail stickers near me": 2}
        picked = self.m.pick("fall sticker", found)
        self.assertEqual(len(picked), len(set(picked)))
        self.assertEqual(picked.count("fall nail stickers"), 1)


class ARatioAgainstZeroIsNotALargeNumber(unittest.TestCase):
    """The summary line market-scan.py printed on its first real run:

        'fall nail stickers' has 19508025.0x the demand per unit of
        competition that 'fall sticker pack' does.

    'fall sticker pack' scored exactly 0.0000 - 5,536 listings and not one
    favourite a day across the top 25 - and the code divided by
    max(score, 1e-9). Nineteen and a half million is not a large ratio; it
    is not a ratio. Printed to one decimal place it reads like a
    measurement, which is worse than printing nothing.

    The zero is the actual finding, and it now gets said out loud.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    NOW = 1758500000.0

    def rows(self, favs, views=500, days=100, title="fall sticker"):
        return [{"original_creation_timestamp": int(self.NOW - days * 86400),
                 "num_favorers": favs, "views": views, "title": title,
                 "price": {"amount": 499, "divisor": 100}}]

    def scan_output(self, table):
        """Run cmd_scan against canned Etsy answers and capture what it says."""
        mod = self.m
        real_expand, real_fetch, real_sleep = mod.tp.expand, mod.fetch, time.sleep
        mod.tp.expand = lambda phrase, **kw: ({c: i for i, c in enumerate(table)}, [])
        mod.fetch = lambda key, cand: (table[cand], None)
        time.sleep = lambda *_a: None
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                mod.cmd_scan("k:s", ["fall", "sticker"])
        finally:
            mod.tp.expand, mod.fetch = real_expand, real_fetch
            time.sleep = real_sleep
        return buf.getvalue()

    def test_no_ratio_is_printed_against_a_zero(self):
        table = {
            "fall nail stickers": self.m.measure(
                {"count": 1260, "results": self.rows(20)}, self.NOW),
            "fall sticker pack": self.m.measure(
                {"count": 5536, "results": self.rows(0)}, self.NOW),
        }
        said = self.scan_output(table)
        self.assertNotIn("19508025", said)
        self.assertNotRegex(said, r"\d{5,}\.\dx")

    def test_the_zero_is_reported_as_the_finding_it_is(self):
        table = {
            "fall nail stickers": self.m.measure(
                {"count": 1260, "results": self.rows(20)}, self.NOW),
            "fall sticker pack": self.m.measure(
                {"count": 5536, "results": self.rows(0)}, self.NOW),
        }
        said = self.scan_output(table)
        self.assertIn("SCORED ZERO", said)
        self.assertIn("fall sticker pack", said)
        self.assertIn("5,536", said)

    def test_a_ratio_between_two_real_scores_is_still_printed(self):
        table = {
            "fall nail stickers": self.m.measure(
                {"count": 1000, "results": self.rows(40)}, self.NOW),
            "fall sticker roll": self.m.measure(
                {"count": 1000, "results": self.rows(10)}, self.NOW),
        }
        said = self.scan_output(table)
        self.assertRegex(said, r"4\.0x the demand")
        self.assertNotIn("SCORED ZERO", said)

    def test_a_loosely_matched_phrase_is_flagged_in_the_output(self):
        # title_match() being right is not the same as the scan SAYING so.
        # A mutation that computed the fraction and then never printed the
        # warning passed every direct test of the function.
        table = {
            "fall sticker emojis": self.m.measure(
                {"count": 110, "results": self.rows(20, title="Autumn Leaf Decal")},
                self.NOW, phrase="fall sticker emojis"),
            "fall nail stickers": self.m.measure(
                {"count": 1260, "results": self.rows(10, title="Fall Nail Stickers Set")},
                self.NOW, phrase="fall nail stickers"),
        }
        said = self.scan_output(table)
        head, _, tail = said.partition("LEFT OUT OF THE RANKING")
        self.assertTrue(tail, said)
        # The loose phrase must not appear in the ranked table at all - it
        # ranked THIRD in the live scan on 4% match, and anyone reading a
        # ranking reads the top of it.
        ranked = head.split("RANKED")[1]
        self.assertNotIn("fall sticker emojis", ranked)
        self.assertIn("fall nail stickers", ranked)
        # But it is still shown, with what it would have scored, because
        # deleting it would hide that the check changed the answer.
        self.assertIn("fall sticker emojis", tail)
        # The NUMBER, not just the words: it would have scored 0.0980 and
        # taken first place, which is the whole reason to print it.
        self.assertRegex(tail, r"would have scored 0\.0\d\d\d")

    def test_a_market_with_no_readable_price_shows_a_question_not_zero(self):
        # Etsy's price field can be missing or malformed on a listing. A
        # median of none must print as unknown: "$0.00 median asking price"
        # reads as a market giving things away.
        priceless = [{"original_creation_timestamp": int(self.NOW - 100 * 86400),
                      "num_favorers": 9, "views": 200,
                      "title": "fall sticker roll"}]
        table = {"fall sticker roll": self.m.measure(
            {"count": 272, "results": priceless}, self.NOW,
            phrase="fall sticker roll")}
        said = self.scan_output(table)
        row = next(l for l in said.splitlines()
                   if l.strip().startswith("fall sticker roll") and "272" in l)
        self.assertIn("?", row)
        self.assertNotIn("$0.00", row)

    def test_the_match_number_is_shown_on_every_row_not_just_bad_ones(self):
        # A warning that only appears below a threshold cannot be told apart
        # from a warning that is broken: the reader sees silence either way.
        # The real run printed no warning at all, and there was no way to
        # know whether that meant 'all fine' or 'never ran'.
        table = {
            "fall nail stickers": self.m.measure(
                {"count": 1260, "results": self.rows(10, title="Fall Nail Stickers Set")},
                self.NOW, phrase="fall nail stickers"),
            "fall sticker roll": self.m.measure(
                {"count": 272, "results": self.rows(5, title="Fall Sticker Roll")},
                self.NOW, phrase="fall sticker roll"),
        }
        said = self.scan_output(table)
        self.assertIn("match", said)
        self.assertEqual(said.count("100%"), 2, said)
        self.assertIn("at or above 50%", said)

    def test_the_n_columns_reach_the_output(self):
        # median_n() being right is not the same as the table SHOWING it.
        # Mutations that computed the counts and then printed neither the
        # columns nor the warning passed every direct test of the function.
        partial = [
            {"original_creation_timestamp": int(self.NOW - 100 * 86400),
             "num_favorers": 10, "views": 200, "title": "fall sticker roll"},
            {"original_creation_timestamp": int(self.NOW * 1000),
             "num_favorers": 4, "views": 205, "title": "fall sticker roll"},
        ]
        table = {"fall sticker roll": self.m.measure(
            {"count": 272, "results": partial}, self.NOW,
            phrase="fall sticker roll")}
        said = self.scan_output(table)
        self.assertEqual(table["fall sticker roll"]["heat_n"], 1)
        self.assertEqual(table["fall sticker roll"]["pull_n"], 2)
        self.assertIn("favs/day from 1, favs/view from 2, of 2", said)
        self.assertIn("should not be read against each other", said)
        # And on the RANKED ROW itself, not only in the footnote: the two n
        # columns have to survive between the median and the printing.
        row = next(l for l in said.splitlines()
                   if l.strip().startswith("fall sticker roll")
                   and "272" in l)
        # supply, price, favs/day, n, favs/view, n - the price column was
        # added between supply and favs/day, and this regex spans it so it
        # keeps holding the two n values in place.
        self.assertRegex(row, r"272\s+\S+\s+[\d.]+\s+1\s+[\d.]+\s+2\s")
        self.assertRegex(said, r"price\s+favs/day\s+n\s+favs/view\s+n\s+match")

    def test_a_complete_payload_prints_no_partial_warning(self):
        # And the check must be min(), not max(): one metric short of the
        # full set is enough to make the two columns incomparable.
        whole = [{"original_creation_timestamp": int(self.NOW - 100 * 86400),
                  "num_favorers": 10, "views": 200,
                  "title": "fall sticker roll"} for _ in range(3)]
        table = {"fall sticker roll": self.m.measure(
            {"count": 272, "results": whole}, self.NOW,
            phrase="fall sticker roll")}
        said = self.scan_output(table)
        self.assertNotIn("should not be read against each other", said)

    def test_one_metric_short_is_enough_to_warn(self):
        rows = [{"original_creation_timestamp": int(self.NOW - 100 * 86400),
                 "num_favorers": 10, "views": 200, "title": "fall sticker roll"},
                {"original_creation_timestamp": int(self.NOW - 100 * 86400),
                 "num_favorers": 10, "views": 0, "title": "fall sticker roll"}]
        m = self.m.measure({"count": 272, "results": rows}, self.NOW,
                           phrase="fall sticker roll")
        self.assertEqual(m["heat_n"], 2)          # complete
        self.assertEqual(m["pull_n"], 1)          # one row short
        said = self.scan_output({"fall sticker roll": m})
        self.assertIn("should not be read against each other", said)

    def test_a_loose_phrase_cannot_take_the_top_of_the_ranking(self):
        # The live scan of 'nail sticker' put 'nail sticker japan' THIRD on
        # 4% match - one of 25 returned listings contained the phrase - with
        # the best favourites-per-view in the table. The warning printed
        # below the table while the row sat near the top of it.
        table = {
            "nail sticker japan": self.m.measure(
                {"count": 273, "results": self.rows(50, views=100,
                                                    title="Nail Decal Set")},
                self.NOW, phrase="nail sticker japan"),
            "nail sticker glue": self.m.measure(
                {"count": 404, "results": self.rows(2, views=100,
                                                    title="Nail Sticker Glue")},
                self.NOW, phrase="nail sticker glue"),
        }
        said = self.scan_output(table)
        ranked = said.split("RANKED")[1].split("LEFT OUT")[0]
        first_row = [l for l in ranked.splitlines()
                     if "0.0" in l and "phrase" not in l][0]
        self.assertIn("nail sticker glue", first_row)
        self.assertNotIn("nail sticker japan", ranked)
        # And the sentence underneath must name the ranked winner, not the
        # loose phrase that was excluded from the table above it.
        summary = said.split("LEFT OUT")[0]
        self.assertNotIn("'nail sticker japan' has", summary)

    def test_the_summary_names_the_ranked_winner_not_the_excluded_one(self):
        # Needs TWO survivors, or the ratio sentence never prints and a
        # mutation taking `best` from the unfiltered list stays invisible.
        table = {
            "nail sticker japan": self.m.measure(      # loose, would win
                {"count": 273, "results": self.rows(50, views=100,
                                                    title="Nail Decal Set")},
                self.NOW, phrase="nail sticker japan"),
            "nail sticker glue": self.m.measure(
                {"count": 404, "results": self.rows(8, views=100,
                                                    title="Nail Sticker Glue")},
                self.NOW, phrase="nail sticker glue"),
            "nail sticker kit": self.m.measure(
                {"count": 382, "results": self.rows(2, views=100,
                                                    title="Nail Sticker Kit")},
                self.NOW, phrase="nail sticker kit"),
        }
        said = self.scan_output(table)
        self.assertIn("'nail sticker glue' has", said)
        self.assertNotIn("'nail sticker japan' has", said)
        self.assertIn("nail sticker kit", said.split(" has ")[1])

    def test_an_excluded_phrase_is_not_the_ratio_denominator(self):
        # The other end of the same mistake. When the loose phrase scores
        # LOWEST rather than highest, taking it as 'the lowest phrase here
        # that scored at all' quotes a ratio against a market nobody
        # searched.
        table = {
            "nail sticker glue": self.m.measure(
                {"count": 404, "results": self.rows(50, views=100,
                                                    title="Nail Sticker Glue")},
                self.NOW, phrase="nail sticker glue"),
            "nail sticker kit": self.m.measure(
                {"count": 382, "results": self.rows(20, views=100,
                                                    title="Nail Sticker Kit")},
                self.NOW, phrase="nail sticker kit"),
            "nail sticker japan": self.m.measure(     # loose, scores lowest
                {"count": 273, "results": self.rows(1, views=100,
                                                    title="Nail Decal Set")},
                self.NOW, phrase="nail sticker japan"),
        }
        said = self.scan_output(table)
        ratio = said.split(" has ")[1].split("\n\n")[0]
        self.assertIn("nail sticker kit", ratio)
        self.assertNotIn("nail sticker japan", ratio)

    def test_an_excluded_phrase_is_not_reported_as_a_dead_market(self):
        # A loosely matched phrase scoring zero says nothing about the
        # phrase - Etsy never searched for it. Listing it under SCORED ZERO
        # would report a dead market that was never measured.
        table = {
            "nail sticker japan": self.m.measure(
                {"count": 273, "results": self.rows(0, views=100,
                                                    title="Nail Decal Set")},
                self.NOW, phrase="nail sticker japan"),
            "nail sticker glue": self.m.measure(
                {"count": 404, "results": self.rows(8, views=100,
                                                    title="Nail Sticker Glue")},
                self.NOW, phrase="nail sticker glue"),
        }
        said = self.scan_output(table)
        zeros = said.split("SCORED ZERO")[1] if "SCORED ZERO" in said else ""
        self.assertNotIn("nail sticker japan", zeros)
        self.assertIn("nail sticker japan", said.split("LEFT OUT")[1])

    def test_the_n_footnote_does_not_generalise_from_one_row(self):
        # It said 'of the 25 returned listings', taken from the first row.
        # 'nail sticker company' returned 17.
        short = [{"original_creation_timestamp": int(self.NOW - 100 * 86400),
                  "num_favorers": 10, "views": 0, "title": "fall sticker roll"},
                 {"original_creation_timestamp": int(self.NOW - 100 * 86400),
                  "num_favorers": 10, "views": 50, "title": "fall sticker roll"}]
        table = {"fall sticker roll": self.m.measure(
            {"count": 272, "results": short}, self.NOW,
            phrase="fall sticker roll")}
        said = self.scan_output(table)
        self.assertIn("how many of the listings Etsy returned", said)
        self.assertNotIn("of the 25 returned listings", said)
        self.assertIn("of 2", said)

    def test_everything_zero_says_so_rather_than_ranking_nothing(self):
        table = {
            "fall sticker pack": self.m.measure(
                {"count": 5536, "results": self.rows(0)}, self.NOW),
            "fall sticker set": self.m.measure(
                {"count": 13433, "results": self.rows(0)}, self.NOW),
        }
        said = self.scan_output(table)
        self.assertIn("no ratio to report", said)
        self.assertIn("SCORED ZERO", said)


class LowSupplyHasTwoMeanings(unittest.TestCase):
    """'fall sticker emojis' came back with 110 active listings - by far the
    thinnest market in the sweep, and the second-highest score.

    That is either the best find in it or an artefact of an odd phrasing
    that Etsy matched loosely, and the supply number alone cannot tell you
    which. So the titles get checked against the phrase."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    def test_titles_that_contain_the_phrase_score_one(self):
        rows = [{"title": "Cozy Fall Sticker Sheet, Autumn Journal"},
                {"title": "FALL STICKERS - vinyl pack"}]
        self.assertEqual(self.m.title_match(rows, "fall sticker"), 1.0)

    def test_a_loose_match_is_caught(self):
        rows = [{"title": "Autumn Leaf Vinyl Decal"},
                {"title": "Pumpkin Spice Tumbler"},
                {"title": "Fall Sticker Emojis Pack"}]
        self.assertAlmostEqual(
            self.m.title_match(rows, "fall sticker emojis"), 1 / 3, places=3)

    def test_plurals_still_count_as_a_match(self):
        rows = [{"title": "Fall Stickers for Journals"}]
        self.assertEqual(self.m.title_match(rows, "fall sticker"), 1.0)

    def test_stopwords_are_not_required_to_appear(self):
        # 'fall stickers for kids' must not be judged on whether the word
        # 'for' is in the title.
        rows = [{"title": "Fall Stickers, Kids Craft Pack"}]
        self.assertEqual(self.m.title_match(rows, "fall stickers for kids"), 1.0)

    def test_a_missing_title_is_not_a_match_and_does_not_crash(self):
        rows = [{"title": None}, {}, {"title": "Fall Sticker Sheet"}]
        self.assertAlmostEqual(self.m.title_match(rows, "fall sticker"),
                               1 / 3, places=3)

    def test_no_rows_means_no_opinion(self):
        self.assertIsNone(self.m.title_match([], "fall sticker"))
        self.assertIsNone(self.m.title_match([{"title": "x"}], ""))

    def test_measure_carries_the_match_through(self):
        m = self.m.measure(
            {"count": 110, "results": [{"title": "Autumn Leaf Decal",
                                        "num_favorers": 3, "views": 100}]},
            1758500000.0, phrase="fall sticker emojis")
        self.assertEqual(m["match"], 0.0)


class AMedianOfThreeRowsIsNotAMedianOfTwentyFive(unittest.TestCase):
    """The scan of 'cozy fall sweatshirt' reported, for 'cozy fall sweater':

        favs/view 0.0195     favs/day 0.000

    Those cannot both describe the same listings. They did not: a row with a
    view count but an unusable timestamp counted towards one median and not
    the other, and nothing in the output said so.

    `dropped` only ever caught a metric missing ENTIRELY. A median computed
    from three rows out of twenty-five was presented exactly like a median
    of all twenty-five - the same defect one level down.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    NOW = 1758500000.0

    def test_the_count_comes_back_with_the_median(self):
        value, n = self.m.median_n([1.0, None, 3.0, None])
        self.assertEqual(value, 2.0)
        self.assertEqual(n, 2)

    def test_all_missing_is_none_and_zero(self):
        self.assertEqual(self.m.median_n([None, None]), (None, 0))
        self.assertEqual(self.m.median_n([]), (None, 0))

    def test_the_two_metrics_report_their_own_row_counts(self):
        # Row 1: usable for both. Row 2: views but a millisecond timestamp,
        # so it counts towards favs/view and not favs/day. Exactly the shape
        # that produced the impossible pair above.
        rows = [
            {"original_creation_timestamp": int(self.NOW - 100 * 86400),
             "num_favorers": 10, "views": 200, "title": "cozy fall sweater"},
            {"original_creation_timestamp": int(self.NOW * 1000),
             "num_favorers": 4, "views": 205, "title": "cozy fall sweater"},
        ]
        m = self.m.measure({"count": 97843, "results": rows}, self.NOW,
                           phrase="cozy fall sweater")
        self.assertEqual(m["returned"], 2)
        self.assertEqual(m["heat_n"], 1)
        self.assertEqual(m["pull_n"], 2)
        self.assertNotEqual(m["heat_n"], m["pull_n"])

    def test_a_clean_payload_reports_every_row(self):
        rows = [{"original_creation_timestamp": int(self.NOW - 100 * 86400),
                 "num_favorers": 10, "views": 200,
                 "price": {"amount": 100, "divisor": 100}} for _ in range(5)]
        m = self.m.measure({"count": 10, "results": rows}, self.NOW)
        self.assertEqual((m["heat_n"], m["pull_n"], m["price_n"]), (5, 5, 5))


class EtsyStemsItsOwnSearchSoTwoCallsBoughtOneAnswer(unittest.TestCase):
    """'cozy fall sweatshirt' and 'cozy fall sweatshirts' both returned
    exactly 137,321 listings. 'cozy fall crewneck' and 'cozy fall crewnecks'
    returned 71,176 and 71,177. Two of ten Etsy calls spent, and two rows of
    the ranking taken, for answers already in hand."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    def test_a_plural_does_not_get_its_own_call(self):
        found = {"cozy fall sweatshirt": 0, "cozy fall sweatshirts": 1,
                 "cozy fall crewneck": 2, "cozy fall crewnecks": 3}
        picked = self.m.pick("cozy fall sweatshirt", found)
        self.assertEqual(len(picked), 2, picked)

    def test_word_order_does_not_make_a_new_phrase(self):
        found = {"fall nail stickers": 0, "nail fall stickers": 1}
        self.assertEqual(len(self.m.pick("fall sticker", found)), 1)

    def test_genuinely_different_phrases_all_survive(self):
        found = {"cozy fall sweater": 0, "cozy fall sweater women": 1,
                 "cozy fall sweater dress": 2, "cozy fall hoodie": 3}
        self.assertEqual(len(self.m.pick("cozy fall sweatshirt", found)), 4)

    def test_the_freed_budget_goes_to_another_phrase(self):
        # The point of the dedup: twelve DISTINCT markets asked about, not
        # twelve calls spent.
        found = {f"fall sticker {w}": i for i, w in enumerate(
            ["sheet", "sheets", "pack", "packs", "roll", "rolls", "set",
             "sets", "book", "books", "kit", "kits", "tin", "tins"])}
        picked = self.m.pick("fall sticker", found)
        self.assertEqual(len(picked), 7)
        self.assertEqual(len(picked), len(set(picked)))


class AnIdeaWithNoMeasurementIsAnOpinion(unittest.TestCase):
    """scout-ideas.py propose.

    Scout's job was to answer 'what is selling on Etsy' out of the model's
    own head. Asked that, a model produces a confident, detailed, plausible
    answer that is fiction - and fiction with numbers on it gets built,
    which is worse than having no researcher at all.

    So Scout names a phrase and the numbers are copied off disk. A figure
    that is never typed cannot be invented, and an idea with no measurement
    behind it cannot be filed at all.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.scans = self.root / "agents" / "scout" / "state" / "scans"
        self.scans.mkdir(parents=True)
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def write_scan(self, name="water bottle sticker", days_ago=0, rows=None,
                   excluded=None):
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        doc = {"seed": name,
               "scanned_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "rows": rows if rows is not None else [
                   {"phrase": "water bottle stickers", "supply": 436862,
                    "heat": 0.066, "pull": 0.1497, "price": 4.99,
                    "match": 1.0, "returned": 25, "heat_n": 25,
                    "pull_n": 25, "score": 0.0117}],
               "excluded": excluded or []}
        (self.scans / "scan.json").write_text(json.dumps(doc))

    def propose(self, *args):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-ideas.py"), "propose", *args],
            capture_output=True, text=True, env=self.env)
        return r.returncode, r.stdout + r.stderr

    def filed(self):
        p = self.root / "agents" / "scout" / "state" / "proposals.json"
        return json.loads(p.read_text())["proposals"] if p.is_file() else []

    def test_a_measured_phrase_is_filed_with_its_numbers(self):
        self.write_scan()
        code, said = self.propose("--phrase", "water bottle stickers",
                                  "--title", "Trail Marker Set",
                                  "--product", "sticker")
        self.assertEqual(code, 0, said)
        ev = self.filed()[0]["evidence"]
        self.assertEqual(ev["supply"], 436862)
        self.assertEqual(ev["favs_per_day"], 0.066)
        self.assertEqual(ev["phrase"], "water bottle stickers")

    def test_an_unmeasured_phrase_is_refused_and_nothing_is_written(self):
        self.write_scan()
        code, said = self.propose("--phrase", "fall pumpkin sticker",
                                  "--title", "X", "--product", "sticker")
        self.assertEqual(code, 2)
        self.assertIn("has not been measured", said)
        self.assertEqual(self.filed(), [])

    def test_with_no_scans_at_all_it_says_how_to_make_one(self):
        code, said = self.propose("--phrase", "anything", "--title", "X",
                                  "--product", "sticker")
        self.assertEqual(code, 2)
        self.assertIn("market-scan.py", said)
        self.assertEqual(self.filed(), [])

    def test_a_dead_market_is_refused(self):
        # 'fall sticker pack': 5,536 listings, score zero. Refusing costs one
        # idea; building into it costs a build.
        self.write_scan(rows=[{"phrase": "fall sticker pack", "supply": 5536,
                               "heat": 0.0, "pull": 0.0, "price": 4.0,
                               "match": 1.0, "returned": 25, "heat_n": 25,
                               "pull_n": 25, "score": 0.0}])
        code, said = self.propose("--phrase", "fall sticker pack",
                                  "--title", "X", "--product", "sticker")
        self.assertEqual(code, 2)
        self.assertIn("scored zero", said)
        self.assertIn("5,536", said)
        self.assertEqual(self.filed(), [])

    def test_a_loosely_matched_phrase_is_refused_with_the_reason(self):
        self.write_scan(excluded=[{"phrase": "water bottle sticker etsy",
                                   "match": 0.08, "supply": 284,
                                   "why": "loose"}])
        code, said = self.propose("--phrase", "water bottle sticker etsy",
                                  "--title", "X", "--product", "sticker")
        self.assertEqual(code, 2)
        self.assertIn("8%", said)
        self.assertEqual(self.filed(), [])

    def test_a_stale_measurement_is_refused(self):
        # Supply moves. A six-week-old number would be presented at review
        # with exactly the confidence of one from this morning.
        self.write_scan(days_ago=40)
        code, said = self.propose("--phrase", "water bottle stickers",
                                  "--title", "X", "--product", "sticker")
        self.assertEqual(code, 2)
        self.assertIn("40 days old", said)
        self.assertEqual(self.filed(), [])

    def test_a_scan_with_no_readable_date_is_refused(self):
        # Found by mutation: an unparseable timestamp read as 'never stale',
        # so such a file would back proposals forever. An unknown age is not
        # a young one.
        self.write_scan()
        path = self.scans / "scan.json"
        doc = json.loads(path.read_text())
        doc["scanned_at"] = "last Tuesday"
        path.write_text(json.dumps(doc))
        code, said = self.propose("--phrase", "water bottle stickers",
                                  "--title", "X", "--product", "sticker")
        self.assertEqual(code, 2)
        self.assertIn("no readable date", said)
        self.assertEqual(self.filed(), [])

    def test_a_fresh_measurement_inside_the_window_is_fine(self):
        self.write_scan(days_ago=13)
        code, said = self.propose("--phrase", "water bottle stickers",
                                  "--title", "X", "--product", "sticker")
        self.assertEqual(code, 0, said)

    def test_a_trademark_anywhere_in_the_idea_is_refused(self):
        # The phrase can be clean while the idea built on it is not:
        # 'water bottle stickers, Pikmin style'.
        self.write_scan()
        for field, args in (
                ("title", ("--title", "Pikmin Bloom Trail Set",
                           "--product", "sticker")),
                ("angle", ("--title", "X", "--product", "sticker",
                           "--angle", "in the style of Hogwarts")),
                ("brief", ("--title", "X", "--product", "sticker",
                           "--brief", "a Stanley cup dupe"))):
            with self.subTest(field=field):
                code, said = self.propose("--phrase", "water bottle stickers",
                                          *args)
                self.assertEqual(code, 2, said)
                self.assertIn("property", said)
                self.assertEqual(self.filed(), [])

    def test_an_ambiguous_word_is_flagged_for_a_human_not_refused(self):
        self.write_scan()
        code, said = self.propose("--phrase", "water bottle stickers",
                                  "--title", "Frozen Lake Series",
                                  "--product", "sticker")
        self.assertEqual(code, 0, said)
        self.assertIn("FLAGGED", said)
        self.assertIn("ip_flag", json.dumps(self.filed()[0]))

    def test_the_evidence_survives_the_merge_into_the_log(self):
        self.write_scan()
        self.propose("--phrase", "water bottle stickers", "--title", "T",
                     "--product", "sticker")
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-ideas.py"), "merge"],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = json.loads((self.root / "agents" / "scout" / "state"
                          / "ideas.json").read_text())
        self.assertEqual(log["ideas"][0]["evidence"]["supply"], 436862)

    def test_a_broken_scan_file_does_not_crash_the_lookup(self):
        self.write_scan()
        (self.scans / "broken.json").write_text("{not json")
        code, said = self.propose("--phrase", "water bottle stickers",
                                  "--title", "X", "--product", "sticker")
        self.assertEqual(code, 0, said)


class TheScanFileIsWhatScoutIsAllowedToBelieve(unittest.TestCase):
    """market-scan.py scan --save. Whatever is in `rows` is proposable and
    whatever is not, is not - so a loosely matched phrase landing in `rows`
    would hand Scout exactly the numbers the loose check exists to withhold."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    NOW = 1758500000.0

    def measured(self, phrase, favs, title):
        rows = [{"original_creation_timestamp": int(self.NOW - 100 * 86400),
                 "num_favorers": favs, "views": 200, "title": title,
                 "price": {"amount": 499, "divisor": 100}}]
        return self.m.measure({"count": 1000, "results": rows}, self.NOW,
                              phrase=phrase)

    def save(self, ranked, loose):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        real, self.m.SCANS = self.m.SCANS, out
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                path = self.m.save_scan("water bottle sticker", ranked, loose)
        finally:
            self.m.SCANS = real
        return json.loads(path.read_text())

    def test_measured_phrases_land_in_rows_with_their_numbers(self):
        doc = self.save([("water bottle stickers",
                          self.measured("water bottle stickers", 20,
                                        "Water Bottle Stickers Pack"))], [])
        self.assertEqual(doc["seed"], "water bottle sticker")
        row = doc["rows"][0]
        self.assertEqual(row["phrase"], "water bottle stickers")
        self.assertEqual(row["supply"], 1000)
        self.assertEqual(row["match"], 1.0)
        self.assertGreater(row["score"], 0)

    def test_a_loose_phrase_never_lands_in_rows(self):
        loose = self.measured("water bottle sticker etsy", 20, "Sticker Pack")
        doc = self.save([], [("water bottle sticker etsy", loose)])
        self.assertEqual(doc["rows"], [])
        self.assertEqual(doc["excluded"][0]["phrase"], "water bottle sticker etsy")
        self.assertEqual(doc["excluded"][0]["match"], 0.0)

    def test_the_timestamp_is_the_format_scout_ideas_parses(self):
        # The two scripts agree on this string or every scan reads as
        # undateable, which scout-ideas treats as never stale.
        doc = self.save([("water bottle stickers",
                          self.measured("water bottle stickers", 20,
                                        "Water Bottle Stickers"))], [])
        si = load("scout_ideas", SCRIPTS / "scout-ideas.py")
        self.assertIsNotNone(si.age_days(doc["scanned_at"]))
        self.assertLess(si.age_days(doc["scanned_at"]), 1)

    def test_the_slug_survives_a_phrase_with_punctuation(self):
        self.assertEqual(self.m.slug("water bottle sticker"),
                         "water-bottle-sticker")
        self.assertEqual(self.m.slug("  FALL/sticker!! "), "fall-sticker")
        self.assertEqual(self.m.slug("!!!"), "scan")


class TheApprovalScreenShowsWhatItIsBasedOn(unittest.TestCase):
    """scout-review.py is where the user decides. Before the evidence block,
    an idea from a market scan and an idea from the model's imagination read
    identically on that screen."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = self.root / "agents" / "scout" / "state"
        self.state.mkdir(parents=True)
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def write_log(self, idea):
        (self.state / "ideas.json").write_text(json.dumps({"ideas": [idea]}))

    def listed(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-review.py"), "list"],
            capture_output=True, text=True, env=self.env)
        return r.stdout + r.stderr

    def test_it_reads_the_root_it_is_told_to(self):
        # scout-review.py hardcoded ROOT and ignored ECOSYSTEM_ROOT, so it
        # read a different ideas.json from the one scout-ideas.py had just
        # written. CLAUDE.md: nothing hardcodes a path.
        self.write_log({"id": 1, "title": "Only In The Temp Root",
                        "product": "sticker", "status": "pending"})
        self.assertIn("Only In The Temp Root", self.listed())

    def test_the_numbers_reach_the_screen(self):
        self.write_log({"id": 1, "title": "Trail Marker Set",
                        "product": "sticker", "status": "pending",
                        "evidence": {"phrase": "water bottle stickers",
                                     "supply": 436862, "favs_per_day": 0.066,
                                     "favs_per_view": 0.1497, "match": 1.0,
                                     "score": 0.0117, "typical_price": 4.99,
                                     "measured_at": "2026-09-23T12:00:00Z",
                                     "from_scan": "water bottle sticker"}})
        said = self.listed()
        self.assertIn("436,862", said)
        self.assertIn("0.066", said)
        self.assertIn("0.1497", said)
        self.assertIn("100%", said)
        self.assertIn("water bottle stickers", said)

    def test_an_idea_with_no_evidence_says_so_rather_than_looking_the_same(self):
        # The log predates the requirement. Hiding the gap would make an
        # unmeasured idea look like a measured one that passed.
        self.write_log({"id": 1, "title": "Old Idea From Scouts Head",
                        "product": "hoodie", "status": "pending"})
        said = self.listed()
        self.assertIn("evidence: NONE", said)
        self.assertIn("has been checked", said)

    def test_the_ip_flag_is_shown_as_the_users_call(self):
        self.write_log({"id": 1, "title": "Frozen Lake", "product": "sticker",
                        "status": "pending",
                        "evidence": {"phrase": "water bottle stickers",
                                     "supply": 1, "favs_per_day": 0.1,
                                     "favs_per_view": 0.1, "match": 1.0,
                                     "score": 0.01, "typical_price": 4.0,
                                     "measured_at": "x", "from_scan": "y",
                                     "ip_flag": "frozen: the film?"}})
        said = self.listed()
        self.assertIn("IP FLAG", said)
        self.assertIn("Your call", said)


class TheArtDirectionLivesInOnePlace(unittest.TestCase):
    """emily-assets.py compose().

    The prompt was built in two places: emily-new-build.py appended 'Flat
    vector illustration for a <product>, clean edges, no text' and
    emily-assets.py appended PRINT_DIRECTION, which says the same thing in
    different words. They had already drifted on whether text was allowed.

    And neither carried a single thing that had been measured, so a design
    for a market of 436,862 listings was briefed exactly like one for 900.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_assets", SCRIPTS / "emily-assets.py")

    BIG = {"phrase": "water bottle stickers", "supply": 436862,
           "typical_price": 4.99, "tags": ["vinyl sticker", "hydroflask"]}
    SMALL = {"phrase": "pottery poster", "supply": 900,
             "typical_price": 28.0, "tags": []}

    def test_a_crowded_market_asks_for_a_design_that_survives_a_thumbnail(self):
        # 436,862 competing listings is not trivia; it means the work is
        # first seen at about 170 pixels in a grid of forty.
        said = self.m.compose("X", "", "sticker", self.BIG)
        self.assertIn("436,862", said)
        self.assertIn("thumbnail", said)
        self.assertIn("no fine detail", said)

    def test_a_narrow_market_is_told_it_can_be_specific(self):
        said = self.m.compose("X", "", "poster", self.SMALL)
        self.assertIn("narrow market", said)
        self.assertNotIn("thumbnail", said)

    def test_a_cheap_market_and_a_dear_one_are_briefed_differently(self):
        cheap = self.m.compose("X", "", "sticker", self.BIG)
        dear = self.m.compose("X", "", "poster", self.SMALL)
        self.assertIn("impulse buy", cheap)
        self.assertNotIn("impulse buy", dear)
        self.assertIn("reward a second look", dear)

    def test_the_markets_own_words_reach_the_prompt(self):
        said = self.m.compose("X", "", "sticker", self.BIG)
        self.assertIn("vinyl sticker", said)
        self.assertIn("hydroflask", said)
        self.assertIn("without copying any individual listing", said)

    def test_no_evidence_means_no_invented_direction(self):
        # A build with no measurement behind it must not be handed made-up
        # numbers to design against.
        said = self.m.compose("Something", "a brief", "mug", None)
        for word in ("thumbnail", "impulse", "listings compete", "Sellers in"):
            self.assertNotIn(word, said)
        self.assertIn("Something", said)
        self.assertIn("a brief", said)

    def test_a_malformed_evidence_blob_is_ignored_not_crashed_on(self):
        for bad in ("not a dict", [], 7, {"supply": "lots", "typical_price": None}):
            with self.subTest(bad=bad):
                said = self.m.compose("X", "", "sticker", bad)
                self.assertIn("X", said)

    def test_previous_designs_are_part_of_the_brief(self):
        # An image model handed the same brief twice draws the same picture
        # twice, and Emily builds one at a time with no memory.
        said = self.m.compose("New One", "", "sticker", self.BIG,
                              ["Pine Ridge Compass", "Switchback Arrow"])
        self.assertIn("visibly different", said)
        self.assertIn("Pine Ridge Compass", said)
        self.assertIn("Switchback Arrow", said)

    def test_the_art_direction_is_appended_once(self):
        said = self.m.directed(self.m.compose("X", "", "sticker", self.BIG))
        self.assertEqual(said.count("NOT a photograph"), 1)
        # And the old second copy is gone from the build script - checked
        # against its CODE, not its comments. The first version of this
        # assertion failed on the comment that explains the removal, which
        # quotes the very string it is looking for.
        code = "\n".join(
            l for l in (SCRIPTS / "emily-new-build.py").read_text().splitlines()
            if not l.lstrip().startswith("#"))
        self.assertNotIn("clean edges, no text", code)
        self.assertIn("compose(", code)


class EmilyRemembersWhatSheAlreadyMade(unittest.TestCase):
    """prior_designs() in emily-assets.py."""

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_assets", SCRIPTS / "emily-assets.py")

    def builds(self, *entries):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for i, (idea, phrase) in enumerate(entries):
            d = root / f"build-{i}"
            d.mkdir()
            body = {"idea": idea}
            if phrase is not None:
                body["evidence"] = {"phrase": phrase}
            (d / "build.json").write_text(json.dumps(body))
        return root

    def test_only_designs_for_the_same_phrase_come_back(self):
        root = self.builds(("Pine Ridge Compass", "water bottle stickers"),
                           ("Autumn Leaf", "fall stickers"),
                           ("Switchback Arrow", "water bottle stickers"))
        got = self.m.prior_designs(root, "water bottle stickers")
        self.assertEqual(sorted(got), ["Pine Ridge Compass", "Switchback Arrow"])

    def test_the_match_ignores_case_and_padding(self):
        root = self.builds(("Pine Ridge", "  Water Bottle STICKERS "))
        self.assertEqual(self.m.prior_designs(root, "water bottle stickers"),
                         ["Pine Ridge"])

    def test_a_build_with_no_evidence_is_not_claimed_for_every_phrase(self):
        root = self.builds(("Old Build", None))
        self.assertEqual(self.m.prior_designs(root, "water bottle stickers"), [])

    def test_a_broken_build_file_does_not_stop_the_others(self):
        root = self.builds(("Good One", "water bottle stickers"))
        bad = root / "broken"
        bad.mkdir()
        (bad / "build.json").write_text("{not json")
        self.assertEqual(self.m.prior_designs(root, "water bottle stickers"),
                         ["Good One"])

    def test_a_missing_folder_is_empty_not_an_error(self):
        self.assertEqual(self.m.prior_designs("/no/such/place", "x"), [])

    def test_duplicates_are_not_listed_twice(self):
        root = self.builds(("Same Name", "p"), ("Same Name", "p"))
        self.assertEqual(self.m.prior_designs(root, "p"), ["Same Name"])


class APriceTypedFromNothing(unittest.TestCase):
    """emily-printify.py market-price.

    Prices were typed from nothing, and a number typed from nothing is as
    likely to be half the market as twice it. The median asking price of the
    top listings for a measured phrase is an anchor - not a recommendation,
    and the user still confirms it.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_printify", SCRIPTS / "emily-printify.py")

    IDS = [1, 2, 3, 4, 5]

    def test_an_existing_ladder_keeps_its_shape(self):
        # A five-size sticker is not one price. Flattening it to the market
        # median would undo the reason sizes exist.
        current = {"1": 300, "2": 400, "3": 500, "4": 650, "5": 800}
        out, kept = self.m.anchored(current, self.IDS, 4.99)
        self.assertTrue(kept)
        self.assertEqual(statistics.median(out.values()), 499)
        order = [out[str(v)] for v in self.IDS]
        self.assertEqual(order, sorted(order), "the ladder must stay rising")

    def test_the_level_really_moves(self):
        current = {"1": 300, "2": 400, "3": 500, "4": 650, "5": 800}
        up, _ = self.m.anchored(current, self.IDS, 10.0)
        down, _ = self.m.anchored(current, self.IDS, 2.5)
        self.assertGreater(statistics.median(up.values()),
                           statistics.median(current.values()))
        self.assertLess(statistics.median(down.values()),
                        statistics.median(current.values()))

    def test_with_nothing_priced_the_median_goes_on_everything(self):
        out, kept = self.m.anchored({}, self.IDS, 4.99)
        self.assertFalse(kept, "there is no ladder to keep")
        self.assertEqual(set(out.values()), {499})

    def test_one_lonely_price_is_not_a_ladder(self):
        out, kept = self.m.anchored({"3": 500}, self.IDS, 4.99)
        self.assertFalse(kept)
        self.assertEqual(set(out.values()), {499})

    def test_unset_variants_are_filled_rather_than_left_at_zero(self):
        # A variant with no price would otherwise go to Printify at nothing.
        current = {"1": 300, "2": 400, "3": 500}
        out, kept = self.m.anchored(current, self.IDS, 4.0)
        self.assertTrue(kept)
        self.assertEqual(len(out), 5)
        self.assertTrue(all(v > 0 for v in out.values()), out)

    def test_zero_and_junk_prices_are_not_treated_as_a_ladder(self):
        for junk in ({"1": 0, "2": 0}, {"1": None, "2": "5.00"},
                     {"1": -100, "2": 0}):
            with self.subTest(junk=junk):
                out, kept = self.m.anchored(junk, self.IDS, 4.99)
                self.assertFalse(kept)
                self.assertEqual(set(out.values()), {499})


class TheEvidenceSurvivesEveryHandOff(unittest.TestCase):
    """The joins, not the links.

    market-scan measures it, scout-ideas copies it, scout-review approves it,
    emily-new-build queues it, emily-assets draws from it. Every one of those
    functions had a test and the chain still had two breaks in it, because a
    function that computes the right thing and a function that is called with
    it are different facts.

    Both breaks were found by mutation, not by reading.
    """

    def review(self):
        return load("scout_review", SCRIPTS / "scout-review.py")

    def test_approval_hands_the_evidence_to_the_build(self):
        # cmd_approve passed title, brief and product and stopped there, so
        # everything measured died at that line.
        m = self.review()
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return Done()

        idea = {"id": 1, "title": "Trail Marker", "product": "sticker",
                "brief": "six die-cut", "status": "pending",
                "evidence": {"phrase": "water bottle stickers",
                             "supply": 436862, "typical_price": 4.99,
                             "tags": ["vinyl sticker"]}}
        doc = {"ideas": [idea]}
        real_run, m.subprocess.run = m.subprocess.run, fake_run
        real_save, m.save = m.save, lambda *_a, **_k: None
        real_note, m.note_lesson = m.note_lesson, lambda *_a, **_k: None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_approve(types.SimpleNamespace(id=1, force=False,
                                                    reason=""), doc)
        finally:
            m.subprocess.run, m.save, m.note_lesson = real_run, real_save, real_note

        cmd = seen["cmd"]
        self.assertIn("--evidence", cmd)
        blob = json.loads(cmd[cmd.index("--evidence") + 1])
        self.assertEqual(blob["phrase"], "water bottle stickers")
        self.assertEqual(blob["supply"], 436862)

    def test_an_idea_with_no_evidence_still_queues(self):
        # The existing log predates the requirement. Refusing to approve
        # those would strand every idea already in it.
        m = self.review()
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, "", ""

        real_run, m.subprocess.run = m.subprocess.run, \
            lambda cmd, **kw: (seen.__setitem__("cmd", cmd), Done())[1]
        real_save, m.save = m.save, lambda *_a, **_k: None
        real_note, m.note_lesson = m.note_lesson, lambda *_a, **_k: None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.cmd_approve(types.SimpleNamespace(id=1, force=False, reason=""),
                              {"ideas": [{"id": 1, "title": "Old", "product": "mug",
                                          "status": "pending"}]})
        finally:
            m.subprocess.run, m.save, m.note_lesson = real_run, real_save, real_note
        self.assertNotIn("--evidence", seen["cmd"])

    def test_the_build_puts_the_evidence_into_the_image_prompt(self):
        # generate_artwork() shells out to emily-assets.py with --prompt.
        # Passing None for evidence there would silently undo the whole
        # chain, and nothing noticed.
        m = load("emily_new_build", SCRIPTS / "emily-new-build.py")
        slug = "zz-test-evidence-reaches-the-prompt"
        built = (SCRIPTS.parent / "agents" / "emily" / "builds" / slug)
        self.addCleanup(shutil.rmtree, built, ignore_errors=True)
        seen = {}

        def fake_run(cmd, **kw):
            seen["prompt"] = cmd[cmd.index("--prompt") + 1]
            Path(cmd[cmd.index("--out") + 1]).write_bytes(b"x" * 4096)
            return types.SimpleNamespace(returncode=0, stdout='"generated"',
                                         stderr="")

        real, m.subprocess.run = m.subprocess.run, fake_run
        try:
            ok, _msg = m.generate_artwork(
                slug, "Trail Marker", "six die-cut", "sticker",
                {"phrase": "water bottle stickers", "supply": 436862,
                 "typical_price": 4.99, "tags": ["vinyl sticker"]})
        finally:
            m.subprocess.run = real
        self.assertTrue(ok)
        self.assertIn("436,862", seen["prompt"])
        self.assertIn("thumbnail", seen["prompt"])
        self.assertIn("impulse buy", seen["prompt"])
        self.assertIn("vinyl sticker", seen["prompt"])

    def test_what_the_shop_already_sells_reaches_the_prompt(self):
        # prior_designs() being right is not the same as generate_artwork()
        # passing it. With no sibling build on disk the argument is empty
        # either way, so the fixture has to contain one.
        m = load("emily_new_build", SCRIPTS / "emily-new-build.py")
        builds = SCRIPTS.parent / "agents" / "emily" / "builds"
        earlier = builds / "zz-test-earlier-design"
        earlier.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, earlier, ignore_errors=True)
        (earlier / "build.json").write_text(json.dumps(
            {"idea": "Pine Ridge Compass",
             "evidence": {"phrase": "water bottle stickers"}}))

        slug = "zz-test-differs-from-earlier"
        self.addCleanup(shutil.rmtree, builds / slug, ignore_errors=True)
        seen = {}

        def fake_run(cmd, **kw):
            seen["prompt"] = cmd[cmd.index("--prompt") + 1]
            Path(cmd[cmd.index("--out") + 1]).write_bytes(b"x" * 4096)
            return types.SimpleNamespace(returncode=0, stdout='"generated"',
                                         stderr="")

        real, m.subprocess.run = m.subprocess.run, fake_run
        try:
            m.generate_artwork(slug, "New One", "", "sticker",
                               {"phrase": "water bottle stickers",
                                "supply": 436862, "typical_price": 4.99})
        finally:
            m.subprocess.run = real
        self.assertIn("Pine Ridge Compass", seen["prompt"])
        self.assertIn("visibly different", seen["prompt"])

    def test_a_build_with_no_evidence_gets_no_invented_direction(self):
        m = load("emily_new_build", SCRIPTS / "emily-new-build.py")
        slug = "zz-test-no-evidence-no-invention"
        built = (SCRIPTS.parent / "agents" / "emily" / "builds" / slug)
        self.addCleanup(shutil.rmtree, built, ignore_errors=True)
        seen = {}

        def fake_run(cmd, **kw):
            seen["prompt"] = cmd[cmd.index("--prompt") + 1]
            Path(cmd[cmd.index("--out") + 1]).write_bytes(b"x" * 4096)
            return types.SimpleNamespace(returncode=0, stdout='"generated"',
                                         stderr="")

        real, m.subprocess.run = m.subprocess.run, fake_run
        try:
            m.generate_artwork(slug, "Something", "a brief", "mug", None)
        finally:
            m.subprocess.run = real
        for word in ("thumbnail", "impulse", "listings compete"):
            self.assertNotIn(word, seen["prompt"])


class ADepositIsNotAGain(unittest.TestCase):
    """The Command Deck reported +101.93% all time on a profit of $193.49.

        portfolio value   $20,193.49
        +101.93% all time · +$10,193.49 vs start

    Belfort held $10,193.49 against a recorded start of $10,000. Ace held
    $10,000 and had no recorded start, so `a.starting_cash ?? 0` contributed
    ZERO to the baseline while its whole $10,000 contributed to the value.
    Adding a second agent's bankroll was reported as doubling the money.

        (20193.49 - 10000) / 10000 = 101.93%

    Three pieces of code decided Ace's starting capital and two of them made
    it up: ace.js fell back to the literal 10000, ace-verify.py falls back to
    10000, and dashboard-data.js returned null. The Deck and Ace's own panel
    disagreed on screen.
    """

    API = ROOT / "mission-control-api"

    def node(self, expr):
        r = subprocess.run(
            ["node", "-e", f"const m=require({json.dumps(str(self.API / 'dashboard-data.js'))});"
                           f"process.stdout.write(JSON.stringify({expr}))"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_exact_numbers_off_the_screenshot(self):
        got = self.node("m.combineCapital(["
                        "{name:'belfort',value:10193.49,starting_cash:10000},"
                        "{name:'ace',value:10000,starting_cash:10000}])")
        self.assertAlmostEqual(got["value"], 20193.49, places=2)
        self.assertEqual(got["start"], 20000)
        self.assertAlmostEqual(got["pnl"], 193.49, places=2)
        self.assertAlmostEqual(got["pct"], 0.967, places=2)

    def test_the_wrong_answer_is_not_produced(self):
        # The number that was on the screen. If it ever comes back, this is
        # the test that says so.
        got = self.node("m.combineCapital(["
                        "{name:'belfort',value:10193.49,starting_cash:10000},"
                        "{name:'ace',value:10000,starting_cash:10000}])")
        self.assertNotAlmostEqual(got["pct"], 101.93, places=1)
        self.assertNotAlmostEqual(got["pnl"], 10193.49, places=1)

    def test_an_unknown_start_withholds_the_total_and_names_the_agent(self):
        # Not zero, and not a guess: no combined baseline at all, plus the
        # name of whoever has to be fixed.
        got = self.node("m.combineCapital(["
                        "{name:'belfort',value:10193.49,starting_cash:10000},"
                        "{name:'ace',value:10000,starting_cash:null}])")
        self.assertAlmostEqual(got["value"], 20193.49, places=2)
        self.assertIsNone(got["start"])
        self.assertIsNone(got["pnl"])
        self.assertIsNone(got["pct"])
        self.assertEqual(got["noStart"], ["ace"])

    def test_undefined_counts_as_unknown_too(self):
        got = self.node("m.combineCapital([{name:'x',value:100}])")
        self.assertIsNone(got["start"])
        self.assertEqual(got["noStart"], ["x"])

    def test_one_agent_alone_is_unchanged(self):
        got = self.node("m.combineCapital("
                        "[{name:'belfort',value:10193.49,starting_cash:10000}])")
        self.assertAlmostEqual(got["pct"], 1.9349, places=3)

    def test_no_agents_is_not_a_division(self):
        got = self.node("m.combineCapital([])")
        self.assertIsNone(got["value"])
        self.assertIsNone(got["pct"])
        self.assertEqual(got["noStart"], [])

    def test_a_loss_is_still_reported(self):
        got = self.node("m.combineCapital(["
                        "{name:'a',value:9000,starting_cash:10000},"
                        "{name:'b',value:9500,starting_cash:10000}])")
        self.assertAlmostEqual(got["pnl"], -1500, places=2)
        self.assertLess(got["pct"], 0)


class OneOpinionAboutWhatAnAgentStartedWith(unittest.TestCase):
    """startingCapital() in dashboard-data.js, imported by ace.js rather than
    written twice. The seed file is a real recorded value; the literal 10000
    that used to be here was not."""

    API = ROOT / "mission-control-api"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "state").mkdir()

    def call(self, portfolio):
        r = subprocess.run(
            ["node", "-e",
             f"const m=require({json.dumps(str(self.API / 'dashboard-data.js'))});"
             f"process.stdout.write(JSON.stringify("
             f"m.startingCapital({json.dumps(str(self.dir))},{json.dumps(portfolio)})))"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def seed(self, name, body):
        (self.dir / "state" / name).write_text(json.dumps(body))

    def test_the_live_file_wins(self):
        self.seed("bankroll.seed.json", {"starting_bankroll": 5000})
        self.assertEqual(self.call({"starting_bankroll": 12345}), 12345)

    def test_the_seed_file_is_used_when_the_live_one_forgot(self):
        # What actually happened to Ace: a live bankroll.json with no
        # starting_bankroll in it.
        self.seed("bankroll.seed.json", {"starting_bankroll": 10000})
        self.assertEqual(self.call({"bankroll": 10000}), 10000)

    def test_a_seed_with_only_a_bankroll_still_answers(self):
        self.seed("bankroll.seed.json", {"bankroll": 2500})
        self.assertEqual(self.call({"cash": 2500}), 2500)

    def test_with_neither_the_answer_is_unknown_not_ten_thousand(self):
        self.assertIsNone(self.call({"bankroll": 10000}))
        self.assertIsNone(self.call(None))

    def test_absent_is_not_zero(self):
        # num(null) returned 0, because Number(null) is 0 and 0 is finite.
        # THIS, not the `?? 0` beside it, is what gave an agent with no
        # recorded starting capital a baseline of zero - a number that looks
        # real and reads as "started with nothing".
        r = subprocess.run(
            ["node", "-e",
             f"const m=require({json.dumps(str(self.API / 'dashboard-data.js'))});"
             "const d=require('fs').mkdtempSync('/tmp/sc-');"
             "require('fs').mkdirSync(d+'/state');"
             "process.stdout.write(JSON.stringify("
             "[m.startingCapital(d,{bankroll:1}),m.startingCapital(d,{starting_cash:''}),"
             "m.startingCapital(d,{starting_cash:0})]))"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        absent, empty, real_zero = json.loads(r.stdout)
        self.assertIsNone(absent, "a missing key is unknown, not zero")
        self.assertIsNone(empty, "an empty string is unknown, not zero")
        self.assertEqual(real_zero, 0, "a recorded zero is still a zero")

    def test_no_invented_starting_balance_survives_in_the_code(self):
        # The literal that caused it. ace.js said `?? 10000`, which is how
        # Ace's own panel showed a confident percentage against a number
        # nobody had read from anywhere.
        ace = (self.API / "ace.js").read_text()
        code = "\n".join(l for l in ace.splitlines()
                         if not l.lstrip().startswith("//"))
        self.assertNotIn("?? 10000", code)
        self.assertIn("startingCapital", code)

    def test_the_rule_is_imported_not_copied(self):
        ace = (self.API / "ace.js").read_text()
        self.assertIn("require('./dashboard-data')", ace)


class TheValueChartRecordsWhatWasPutIn(unittest.TestCase):
    """The near-vertical rise on the left of the value history is the moment
    a second bankroll was added. Without the baseline stored next to the
    value, a deposit and a gain draw identically."""

    API = ROOT / "mission-control-api"

    def test_each_snapshot_carries_its_baseline(self):
        src = (self.API / "dashboard-data.js").read_text()
        self.assertIn("b: totalStart === null ? null", src)
        self.assertIn("function snapshotHistory(totalValue, totalStart)", src)

    def test_the_caller_passes_it(self):
        src = (self.API / "dashboard-data.js").read_text()
        self.assertIn("snapshotHistory(totalValue, totalStart)", src)


class WhatEtsyAndPrintifyTakeFirst(unittest.TestCase):
    """market-price set prices from the median ASKING price and said nothing
    about cost or fees.

    For 'water bottle stickers' the median is $3.99, and the proposed ladder
    started at $3.10 for a 2"x2". Out of that come 6.5% transaction, 3% +
    $0.25 processing, $0.20 listing and whatever Printify charges to make it.
    At a $2.60 production cost that sale LOSES 24 cents, and the first
    version of this command would have set it and reported success.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_printify", SCRIPTS / "emily-printify.py")

    FEES = {"transaction_pct": 0.065, "processing_pct": 0.03,
            "processing_flat": 0.25, "listing_fee": 0.20,
            "offsite_ads_pct": 0.0}

    def test_what_you_actually_keep(self):
        # $3.10 - 9.5% - $0.25 - $0.20 - $1.32 production
        self.assertAlmostEqual(self.m.net_of(3.10, 1.32, self.FEES), 1.0355,
                               places=4)

    def test_break_even_is_where_it_crosses_zero(self):
        floor = self.m.floor_price(1.32, self.FEES)
        self.assertAlmostEqual(floor, 1.9558, places=3)
        self.assertAlmostEqual(self.m.net_of(floor, 1.32, self.FEES), 0.0,
                               places=9)

    def test_the_losing_case_off_the_real_ladder(self):
        self.assertLess(self.m.net_of(3.10, 2.60, self.FEES), 0)
        self.assertGreater(self.m.floor_price(2.60, self.FEES), 3.10)

    def test_offsite_ads_make_the_floor_higher(self):
        with_ads = dict(self.FEES, offsite_ads_pct=0.15)
        self.assertGreater(self.m.floor_price(1.32, with_ads),
                           self.m.floor_price(1.32, self.FEES))

    def test_a_fee_table_that_takes_everything_has_no_floor(self):
        # Nonsense in, no number out - rather than a negative or infinite
        # "break-even" presented as a price.
        self.assertIsNone(self.m.floor_price(
            1.0, dict(self.FEES, transaction_pct=0.99, processing_pct=0.02)))

    def test_defaults_are_used_and_can_be_overridden(self):
        self.assertEqual(self.m.fees_of({})["transaction_pct"], 0.065)
        got = self.m.fees_of({"_fees": {"transaction_pct": 0.05}})
        self.assertEqual(got["transaction_pct"], 0.05)
        self.assertEqual(got["listing_fee"], 0.20, "the rest keep their defaults")

    def test_junk_in_the_saved_fees_is_ignored(self):
        got = self.m.fees_of({"_fees": {"transaction_pct": "loads",
                                        "nonsense_key": 1}})
        self.assertEqual(got["transaction_pct"], 0.065)
        self.assertNotIn("nonsense_key", got)


class PricingRunsEndToEndOrNotAtAll(unittest.TestCase):
    """--apply crashed with NameError: write_catalog is not defined.

        File "scripts/emily-printify.py", line 707, in cmd_market_price
          write_catalog(cat)

    The catalogue write was copy-pasted inline at five call sites and
    existed as a function at none of them. The table had already printed, so
    it looked like it had worked right up to the traceback.

    It shipped because every test here called anchored(), a pure function,
    and none ran the COMMAND. I had run it myself - without --apply, so the
    write path never executed. Fourth time this session that a function
    passed its test while its caller was broken.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "agents" / "emily" / "state").mkdir(parents=True)
        (self.root / "agents" / "scout" / "state" / "scans").mkdir(parents=True)
        self.cat = self.root / "agents" / "emily" / "state" / "printify-catalog.json"
        self.cat.write_text(json.dumps({"sticker": {
            "blueprint_id": 1, "provider_id": 1,
            "blueprint_title": "Kiss-Cut Vinyl Decals",
            "variant_ids": [1, 2, 3],
            "variant_titles": ['2" x 2"', '3" x 3"', '4" x 4"'],
            "prices": {"1": 699, "2": 799, "3": 899}}}))
        (self.root / "agents" / "scout" / "state" / "scans" / "s.json").write_text(
            json.dumps({"seed": "water bottle sticker",
                        "scanned_at": datetime.now(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "rows": [{"phrase": "water bottle stickers",
                                  "supply": 436800, "heat": 0.066, "pull": 0.1497,
                                  "price": 3.99, "match": 1.0, "returned": 25,
                                  "heat_n": 25, "pull_n": 25, "score": 0.0049}],
                        "excluded": []}))
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def run_it(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "emily-printify.py"), *args],
            capture_output=True, text=True, env=self.env)

    def prices(self):
        return json.loads(self.cat.read_text())["sticker"].get("prices")

    def test_apply_writes_the_prices(self):
        self.run_it("costs", "--product", "sticker", "1.32", "1.55", "1.86")
        r = self.run_it("market-price", "--product", "sticker",
                        "--market", "water bottle stickers", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("NameError", r.stderr)
        # median of 699/799/899 is 799, so the factor is 399/799.
        self.assertEqual(self.prices(), {"1": 349, "2": 399, "3": 449})

    def test_without_apply_nothing_is_written(self):
        self.run_it("costs", "--product", "sticker", "1.32", "1.55", "1.86")
        before = self.prices()
        r = self.run_it("market-price", "--product", "sticker",
                        "--market", "water bottle stickers")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.prices(), before)

    def test_a_below_cost_ladder_is_refused_and_nothing_is_written(self):
        # At $3.49 a $3.20 cost loses 49c once 9.5% + $0.45 comes out.
        self.run_it("costs", "--product", "sticker", "3.20", "3.40", "3.60")
        before = self.prices()
        r = self.run_it("market-price", "--product", "sticker",
                        "--market", "water bottle stickers", "--apply")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("BELOW BREAK-EVEN", r.stdout)
        self.assertIn("break-even is", r.stdout)
        self.assertEqual(self.prices(), before, "nothing may be written")

    def test_uncosted_variants_are_called_out(self):
        r = self.run_it("market-price", "--product", "sticker",
                        "--market", "water bottle stickers")
        self.assertIn("NO RECORDED PRODUCTION COST", r.stdout)
        self.assertIn("Printify product page", r.stdout)

    def test_costs_are_recorded_one_per_variant_in_order(self):
        # --by-size cannot work here: size_of() knows apparel sizes, and a
        # sticker's variants are '2" x 2"'. It matched nothing and refused
        # every variant as missing.
        r = self.run_it("costs", "--product", "sticker", "1.32", "1.55", "1.86")
        self.assertEqual(r.returncode, 0, r.stderr)
        entry = json.loads(self.cat.read_text())["sticker"]
        self.assertEqual(entry["costs"], {"1": 1.32, "2": 1.55, "3": 1.86})

    def test_the_wrong_number_of_costs_is_refused(self):
        r = self.run_it("costs", "--product", "sticker", "1.32", "1.55")
        self.assertEqual(r.returncode, 2)
        self.assertIn("3 variant(s)", r.stderr)
        self.assertNotIn("costs", json.loads(self.cat.read_text())["sticker"])

    def test_listing_costs_says_how_to_record_them(self):
        r = self.run_it("costs", "--product", "sticker")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unrecorded", r.stdout)
        self.assertIn("Printify product page", r.stdout)

    def test_the_fees_used_are_recorded_with_the_price(self):
        # So a price set under one fee structure can be told from one set
        # under another, months later.
        self.run_it("costs", "--product", "sticker", "1.32", "1.55", "1.86")
        self.run_it("market-price", "--product", "sticker",
                    "--market", "water bottle stickers", "--apply")
        entry = json.loads(self.cat.read_text())["sticker"]
        self.assertEqual(entry["priced_against"]["costs_known"], 3)
        self.assertEqual(entry["priced_against"]["fees_used"]["transaction_pct"],
                         0.065)

    def test_the_shipping_table_reaches_the_margin_column(self):
        # net_of() taking shipping is not the same as market-price PASSING
        # it. Every other command test here has zero postage, so the
        # argument made no difference and a mutation dropping it survived.
        #
        # These are the real Printify numbers: $1.42 to print, $4.59 to
        # post. At the $3.99 median with free shipping every size loses
        # money, and the ladder must be refused.
        self.run_it("costs", "--product", "sticker", "1.42", "1.68", "2.02")
        self.run_it("fees", "--set", "ship_cost=4.59")
        before = self.prices()
        r = self.run_it("market-price", "--product", "sticker",
                        "--market", "water bottle stickers", "--apply")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("BELOW BREAK-EVEN", r.stdout)
        self.assertIn("absorbing $4.59 postage", r.stdout)
        self.assertIn("MULTI-PACK", r.stdout)
        # The break-even figure itself, not just the fact of a refusal:
        # ($1.42 + $4.59 + $0.45) / (1 - 0.095) = $7.14. Without postage in
        # it the same line reads $2.07, and the refusal still fires - so
        # only this assertion can tell the two apart.
        self.assertIn("$7.14", r.stdout)
        self.assertEqual(self.prices(), before)

    def test_charging_the_postage_makes_the_same_ladder_pass(self):
        self.run_it("costs", "--product", "sticker", "1.42", "1.68", "2.02")
        self.run_it("fees", "--set", "ship_cost=4.59", "ship_charged=4.59")
        r = self.run_it("market-price", "--product", "sticker",
                        "--market", "water bottle stickers", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("BELOW BREAK-EVEN", r.stdout)
        self.assertEqual(self.prices(), {"1": 349, "2": 399, "3": 449})

    def test_there_is_one_catalogue_writer(self):
        src = (SCRIPTS / "emily-printify.py").read_text()
        code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
        inline = [l for l in code if "CATALOG.write_text" in l]
        self.assertEqual(len(inline), 1,
                         "the catalogue write belongs in write_catalog() alone")
        self.assertIn("def write_catalog(cat):", src)


class PostageIsTheWholeStoryOnASticker(unittest.TestCase):
    """Printify's Kiss-Cut sticker: $1.42 to print, $4.59 to ship.

    The postage costs three times the product and more than the $3.99 that
    market is asking for the item. The first version of net_of() had no
    shipping term at all, so every margin it printed assumed postage was
    free - and on this product that is the difference between making 70
    cents and losing $3.65.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_printify", SCRIPTS / "emily-printify.py")

    FEES = {"transaction_pct": 0.065, "processing_pct": 0.03,
            "processing_flat": 0.25, "listing_fee": 0.20,
            "offsite_ads_pct": 0.0, "ship_cost": 4.59, "ship_charged": 0.0}

    def test_free_shipping_on_a_cheap_sticker_loses_money(self):
        # 3.99 - 9.5% - 0.45 flat - 1.42 print - 4.59 post
        net = self.m.net_of(3.99, 1.42, self.FEES, 4.59, 0.0)
        self.assertLess(net, -2.5)
        self.assertAlmostEqual(net, -2.84905, places=4)

    def test_the_buyer_paying_postage_turns_it_positive(self):
        # revenue 3.99 + 4.59 = 8.58, and Etsy's cut applies to both
        net = self.m.net_of(3.99, 1.42, self.FEES, 4.59, 4.59)
        self.assertGreater(net, 0)
        self.assertAlmostEqual(net, 1.3049, places=4)

    def test_break_even_moves_by_roughly_the_postage(self):
        free = self.m.floor_price(1.42, self.FEES, 4.59, 0.0)
        charged = self.m.floor_price(1.42, self.FEES, 4.59, 4.59)
        self.assertAlmostEqual(free - charged, 4.59, places=2)
        self.assertGreater(free, 7)
        self.assertLess(charged, 4)

    def test_break_even_is_where_it_crosses_zero_with_shipping_too(self):
        for charged in (0.0, 2.0, 4.59):
            with self.subTest(charged=charged):
                floor = self.m.floor_price(1.42, self.FEES, 4.59, charged)
                self.assertAlmostEqual(
                    self.m.net_of(floor, 1.42, self.FEES, 4.59, charged),
                    0.0, places=9)

    def test_etsy_taxes_the_shipping_you_charge(self):
        # Charging $4.59 does not return $4.59: the transaction and
        # processing percentages apply to it as well as to the item.
        taxed = self.m.net_of(3.99, 1.42, self.FEES, 4.59, 4.59)
        untaxed = self.m.net_of(3.99, 1.42, self.FEES, 4.59, 0.0) + 4.59
        self.assertLess(taxed, untaxed)

    def test_a_multipack_carries_the_postage_five_ways(self):
        # The real reason to sell packs: postage is per PARCEL, print is per
        # sticker. One envelope of five costs one postage.
        single = self.m.net_of(3.99, 1.42, self.FEES, 4.59, 0.0)
        pack = self.m.net_of(16.99, 1.42 * 5, self.FEES, 4.59, 0.0)
        self.assertLess(single, 0)
        self.assertGreater(pack, 0)

    def test_no_shipping_arguments_is_the_old_behaviour(self):
        # Defaults of zero, so every existing caller keeps its meaning.
        self.assertAlmostEqual(self.m.net_of(3.10, 1.32, self.FEES),
                               1.0355, places=4)

    def test_the_default_table_carries_shipping_keys(self):
        for k in ("ship_cost", "ship_charged"):
            self.assertIn(k, self.m.DEFAULT_FEES)
            self.assertEqual(self.m.DEFAULT_FEES[k], 0.0)

    def test_fees_can_be_set_and_are_read_back(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "agents" / "emily" / "state").mkdir(parents=True)
        env = dict(os.environ, ECOSYSTEM_ROOT=str(root))
        run = lambda *a: subprocess.run(
            [sys.executable, str(SCRIPTS / "emily-printify.py"), "fees", *a],
            capture_output=True, text=True, env=env)
        r = run("--set", "ship_cost=4.59")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("4.59", run().stdout)
        saved = json.loads((root / "agents" / "emily" / "state"
                            / "printify-catalog.json").read_text())
        self.assertEqual(saved["_fees"]["ship_cost"], 4.59)

    def test_an_unknown_fee_name_is_refused(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "agents" / "emily" / "state").mkdir(parents=True)
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "emily-printify.py"), "fees",
             "--set", "etsy_vibes=0.5"], capture_output=True, text=True,
            env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a fee", r.stderr)

    def test_zero_shipping_is_called_out_rather_than_assumed_fine(self):
        # The state this shipped in: every margin printed as if postage were
        # free, with nothing saying so.
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        state = root / "agents" / "emily" / "state"
        state.mkdir(parents=True)
        (root / "agents" / "scout" / "state" / "scans").mkdir(parents=True)
        (state / "printify-catalog.json").write_text(json.dumps({"sticker": {
            "variant_ids": [1], "variant_titles": ['2" x 2"'],
            "prices": {"1": 399}, "costs": {"1": 1.42}}}))
        (root / "agents" / "scout" / "state" / "scans" / "s.json").write_text(
            json.dumps({"seed": "s", "scanned_at": datetime.now(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "rows": [{"phrase": "water bottle stickers", "supply": 1,
                                  "heat": 0.1, "pull": 0.1, "price": 3.99,
                                  "match": 1.0, "returned": 25, "heat_n": 25,
                                  "pull_n": 25, "score": 0.1}], "excluded": []}))
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "emily-printify.py"), "market-price",
             "--product", "sticker", "--market", "water bottle stickers"],
            capture_output=True, text=True,
            env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
        self.assertIn("SHIPPING IS NOT IN THESE NUMBERS", r.stdout)


class SellersShoppingForAssetsAreNotBuyers(unittest.TestCase):
    """The sticker-sheet scan put these in the top two places:

        sticker sheet mockup        219 listings   score 0.0144
        sticker sheet for printer   388 listings   score 0.0049

    Both are other Etsy SELLERS shopping for design assets - a mockup
    template to photograph a design on, a printable file to run off at home.
    That is a real market; it is not the one this shop is in, and it took
    the top of a ranking meant to say what to make and post.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("trend_probe", SCRIPTS / "trend-probe.py")

    def test_the_two_that_topped_the_real_scan(self):
        self.assertEqual(self.m.intent_of("sticker sheet mockup")[0], "digital")
        self.assertEqual(self.m.intent_of("sticker sheet for printer")[0],
                         "digital")

    def test_other_seller_tools_are_caught_too(self):
        for phrase in ("sticker mockup psd", "svg for cricut",
                       "sticker sheet mock up", "decal dxf file",
                       "sticker pack commercial use"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.m.intent_of(phrase)[0], "digital", phrase)

    def test_real_products_are_not_swept_up(self):
        # 'sticker sheet custom' and 'sticker sheet' are 100%-match product
        # searches and must stay in BUYING; 'holder' is a physical object.
        for phrase in ("sticker sheet custom", "sticker sheet",
                       "sticker sheet holder", "water bottle stickers",
                       "custom vinyl sticker"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.m.intent_of(phrase)[0], phrase)

    def test_printer_as_an_object_is_not_a_file(self):
        # 'for printer' is the seller phrase. A sticker OF a printer, or a
        # printer-themed design, is a product.
        self.assertIsNone(self.m.intent_of("retro printer sticker")[0])


class ThePriceWasComputedAndNeverShown(unittest.TestCase):
    """market-scan measured the median asking price of every market, saved
    it to the scan file, and printed a table without it.

    It is the number the whole decision turns on - whether a thing can be
    made for less than the market charges - and it was the one column
    missing. Found by needing it and not having it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.scans = self.root / "agents" / "scout" / "state" / "scans"
        self.scans.mkdir(parents=True)
        (self.scans / "s.json").write_text(json.dumps({
            "seed": "sticker sheet",
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [
                {"phrase": "sticker sheet custom", "supply": 17120,
                 "heat": 0.013, "pull": 0.0199, "price": 8.5, "match": 1.0,
                 "returned": 25, "heat_n": 25, "pull_n": 23, "score": 0.0030},
                {"phrase": "no price here", "supply": 100, "heat": 0.01,
                 "pull": 0.01, "price": None, "match": 1.0, "returned": 25,
                 "heat_n": 25, "pull_n": 25, "score": 0.005}],
            "excluded": []}))
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def test_the_median_price_is_on_the_evidence_listing(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-ideas.py"), "evidence"],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("$8.50", r.stdout)
        self.assertIn("median", r.stdout)

    def test_a_missing_price_shows_as_unknown_not_as_zero(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-ideas.py"), "evidence"],
            capture_output=True, text=True, env=self.env)
        line = [l for l in r.stdout.splitlines() if "no price here" in l][0]
        self.assertIn("?", line)
        self.assertNotIn("$0.00", line)

    def test_the_scan_table_carries_a_price_column(self):
        src = (SCRIPTS / "market-scan.py").read_text()
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertIn("'price':>8", code)
        self.assertIn("m['price']", code)


class WhichProductIsADifferentQuestion(unittest.TestCase):
    """market-scan compare.

    "Should I sell stickers or mugs" was being answered by whichever scan
    happened to be on screen. This reads the saved scans - no API calls, no
    model - and puts every measured market in one table.

    The trap it has to avoid is the one the reference dashboard falls into:
    multiplying a demand proxy by a price and calling the result revenue.
    Favourites are not sales. Price and demand are shown side by side and
    nothing is multiplied by anything.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.scans = self.root / "agents" / "scout" / "state" / "scans"
        self.scans.mkdir(parents=True)
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def scan(self, seed, rows):
        (self.scans / f"{seed.replace(' ', '-')}.json").write_text(json.dumps({
            "seed": seed,
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": rows, "excluded": []}))

    def row(self, phrase, supply, heat, price, score, pull=0.05):
        return {"phrase": phrase, "supply": supply, "heat": heat, "pull": pull,
                "price": price, "match": 1.0, "returned": 25, "heat_n": 25,
                "pull_n": 25, "score": score}

    def run_it(self):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "market-scan.py"), "compare"],
            capture_output=True, text=True, env=self.env)

    def test_the_real_numbers_line_up_in_one_table(self):
        self.scan("water bottle sticker",
                  [self.row("water bottle stickers", 436800, 0.066, 4.99, 0.0049)])
        self.scan("sticker sheet",
                  [self.row("sticker sheet", 245012, 0.008, 8.50, 0.0016)])
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("436,800", r.stdout)
        self.assertIn("$4.99", r.stdout)
        self.assertIn("$8.50", r.stdout)
        self.assertIn("0.066", r.stdout)

    def test_the_best_phrase_in_each_scan_is_the_one_shown(self):
        self.scan("sticker sheet", [
            self.row("sticker sheet", 245012, 0.008, 8.50, 0.0016),
            self.row("sticker sheet custom", 17120, 0.013, 12.0, 0.0030)])
        out = self.run_it().stdout
        self.assertIn("sticker sheet custom", out)
        self.assertNotIn("245,012", out, "the weaker phrase must not be the row")

    def test_the_table_is_ordered_best_first(self):
        # A comparison table that is not sorted is a list, and the reader
        # takes the top row as the answer either way.
        self.scan("weak", [self.row("weak thing", 100000, 0.002, 5.0, 0.0004)])
        self.scan("strong", [self.row("strong thing", 1000, 0.08, 5.0, 0.0267)])
        self.scan("middle", [self.row("middle thing", 10000, 0.02, 5.0, 0.005)])
        body = self.run_it().stdout.split("best phrase")[1]
        # Data rows only: the footer contains the word "anything", which a
        # looser filter picked up as a fourth market.
        order = [l for l in body.splitlines()
                 if re.match(r"\s+(weak|strong|middle)\s", l)]
        self.assertEqual([o.split()[0] for o in order],
                         ["strong", "middle", "weak"])

    def test_demand_and_price_are_named_separately(self):
        self.scan("cheap sticker",
                  [self.row("cheap vinyl sticker", 1000, 0.200, 4.00, 0.06)])
        self.scan("dear tote",
                  [self.row("dear canvas tote", 1000, 0.005, 28.00, 0.002)])
        out = self.run_it().stdout
        self.assertIn("Most demand:  cheap sticker", out)
        self.assertIn("Dearest item: dear tote", out)

    def test_nothing_is_multiplied_into_a_revenue_figure(self):
        # The Dennis trap: price x demand looks like money and is not.
        self.scan("tote", [self.row("canvas tote", 1000, 0.100, 10.00, 0.03)])
        out = self.run_it().stdout
        self.assertIn("Favourites are not sales", out)
        self.assertNotIn("revenue", out.lower())
        self.assertNotIn("$/day", out)
        self.assertNotIn("100.00", out, "0.100 x 10.00 scaled must not appear")

    def test_a_market_with_no_price_is_shown_with_a_question(self):
        self.scan("no price sticker", [self.row("no price sticker", 100, 0.01, None, 0.004)])
        out = self.run_it().stdout
        self.assertIn("?", out)
        self.assertNotIn("$0.00", out)

    def test_it_says_what_actually_settles_it(self):
        self.scan("tote", [self.row("canvas tote", 1000, 0.1, 10.0, 0.03)])
        out = self.run_it().stdout
        self.assertIn("margin", out)
        self.assertIn("production cost", out)

    def test_no_scans_is_an_instruction_not_a_crash(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("--save", r.stderr)

    def test_a_scan_with_no_scorable_phrase_is_skipped_not_crashed_on(self):
        self.scan("empty", [])
        self.scan("good sticker", [self.row("good vinyl sticker", 1000, 0.1, 10.0, 0.03)])
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("good vinyl sticker", r.stdout)
        self.assertIn("1 market(s)", r.stdout)

    def test_a_broken_scan_file_does_not_stop_the_others(self):
        (self.scans / "broken.json").write_text("{not json")
        self.scan("good sticker", [self.row("good vinyl sticker", 1000, 0.1, 10.0, 0.03)])
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("good vinyl sticker", r.stdout)


class AMugIsNotThreeNinety(unittest.TestCase):
    """Three real scans each put a digital market in or near first place, and
    the intent labels missed all three because nothing in the WORDS gives
    them away:

        funny coffee mug designs    $3.90   against a $19.70 market
        canvas tote bag pattern     $6.00   against a $22.62 market
        wall art print etsy         $5.95   against a $18.50 market

    Design files, sewing patterns and listing services wearing a product's
    phrase. The price is what gives them away, and the scan already knew
    every price.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("market_scan", SCRIPTS / "market-scan.py")

    def scored(self, pairs):
        return [(p, {"price": v}) for p, v in pairs]

    MUGS = [("funny coffee mug designs", 3.90),
            ("funny coffee mugs for your boss", 19.99),
            ("funny coffee mug for men", 19.95),
            ("funny coffee mug coworker", 21.98),
            ("funny coffee mugs etsy", 18.70),
            ("funny coffee mugs", 19.99),
            ("funny coffee cups", 19.46),
            ("funny coffee mug gifts", 17.95)]

    TOTES = [("canvas tote bag insert", 40.00),
             ("canvas tote bag embroidered", 23.84),
             ("canvas tote bag pattern", 6.00),
             ("canvas tote bag", 22.62),
             ("canvas tote bag designer", 39.98),
             ("canvas tote bag with zipper", 29.00),
             ("canvas tote bag custom", 22.62)]

    def test_the_mug_scan_flags_the_design_files(self):
        odd, typical = self.m.price_outliers(self.scored(self.MUGS))
        self.assertAlmostEqual(typical, 19.70, places=2)
        self.assertEqual([c for c, _ in odd], ["funny coffee mug designs"])

    def test_the_tote_scan_flags_the_sewing_pattern(self):
        odd, _typical = self.m.price_outliers(self.scored(self.TOTES))
        self.assertEqual([c for c, _ in odd], ["canvas tote bag pattern"])

    def test_a_dearer_phrase_is_never_flagged(self):
        # 'canvas tote bag insert' at $40 against a $22.62 market is not an
        # outlier in the direction this looks for. Expensive is a product
        # decision; a tenth of the market price is a different product.
        odd, _t = self.m.price_outliers(self.scored(self.TOTES))
        self.assertNotIn("canvas tote bag insert", [c for c, _ in odd])

    def test_an_ordinary_spread_flags_nothing(self):
        odd, _t = self.m.price_outliers(self.scored(
            [("a", 18.0), ("b", 20.0), ("c", 22.0), ("d", 25.0), ("e", 12.0)]))
        self.assertEqual(odd, [])

    def test_too_few_prices_means_no_opinion(self):
        # With three phrases there is no "rest of the market" to be out of
        # step with, and calling one an outlier would be arithmetic on noise.
        odd, typical = self.m.price_outliers(self.scored(
            [("a", 20.0), ("b", 19.0), ("c", 1.0)]))
        self.assertEqual(odd, [])
        self.assertIsNone(typical)

    def test_unreadable_prices_do_not_drag_the_market_down(self):
        # Etsy's price field comes back unreadable often enough that a scan
        # can carry several zeros. Counting them in the median pulls
        # "typical" towards nothing, and then a genuine outlier sits ABOVE
        # the threshold and is never flagged - the check goes quiet exactly
        # when the data is worst.
        scored = self.scored([("cheap file", 6.0), ("a", 20.0), ("b", 20.0),
                              ("c", 20.0), ("d", 22.0)]) + \
            [(f"unreadable {i}", {"price": 0.0}) for i in range(5)]
        odd, typical = self.m.price_outliers(scored)
        self.assertAlmostEqual(typical, 20.0, places=2)
        self.assertEqual([c for c, _ in odd], ["cheap file"])

    def test_missing_and_zero_prices_are_not_outliers(self):
        rows = [("a", 20.0), ("b", 19.0), ("c", 21.0), ("d", 22.0)]
        scored = self.scored(rows) + [("no price", {"price": None}),
                                      ("free", {"price": 0.0})]
        odd, _t = self.m.price_outliers(scored)
        self.assertEqual(odd, [])

    def test_the_warning_reaches_the_scan_output(self):
        # price_outliers() being right is not the same as the scan SAYING so.
        m = self.m
        now = 1758500000.0

        def measured(phrase, price, favs=10):
            rows = [{"original_creation_timestamp": int(now - 100 * 86400),
                     "num_favorers": favs, "views": 200, "title": phrase,
                     "price": {"amount": int(price * 100), "divisor": 100}}]
            return m.measure({"count": 5000, "results": rows}, now, phrase=phrase)

        table = {p: measured(p, v) for p, v in self.MUGS}
        real_expand, real_fetch, real_sleep = m.tp.expand, m.fetch, time.sleep
        m.tp.expand = lambda phrase, **kw: ({c: i for i, c in enumerate(table)}, [])
        m.fetch = lambda key, cand: (table[cand], None)
        time.sleep = lambda *_a: None
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                m.cmd_scan("k:s", ["funny", "coffee", "mug"])
        finally:
            m.tp.expand, m.fetch = real_expand, real_fetch
            time.sleep = real_sleep
        said = buf.getvalue()
        self.assertIn("PRICED LIKE A DIFFERENT PRODUCT", said)
        self.assertIn("funny coffee mug designs", said.split(
            "PRICED LIKE A DIFFERENT PRODUCT")[1])
        self.assertIn("$3.90", said)


class WhatMustICharge(unittest.TestCase):
    """price_for() - the other direction from floor_price().

    The median says what the market asks. It does not say what you need. A
    canvas tote whose blank costs $12.60 and posts for $6.00 clears $1.42 at
    the market's $22.62 and $8.09 at $29.99, and which of those is the
    business is a decision rather than a measurement.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_printify", SCRIPTS / "emily-printify.py")

    FEES = {"transaction_pct": 0.065, "processing_pct": 0.03,
            "processing_flat": 0.25, "listing_fee": 0.20,
            "offsite_ads_pct": 0.0, "ship_cost": 6.00, "ship_charged": 0.0}

    def test_it_is_the_inverse_of_net_of(self):
        for target in (0.0, 1.42, 8.09, 25.0):
            with self.subTest(target=target):
                price = self.m.price_for(target, 12.60, self.FEES, 6.00, 0.0)
                self.assertAlmostEqual(
                    self.m.net_of(price, 12.60, self.FEES, 6.00, 0.0),
                    target, places=9)

    def test_a_target_of_zero_is_the_break_even_price(self):
        self.assertAlmostEqual(
            self.m.price_for(0.0, 12.60, self.FEES, 6.00, 0.0),
            self.m.floor_price(12.60, self.FEES, 6.00, 0.0), places=9)

    def test_the_real_tote_numbers(self):
        # $12.60 blank, $6.00 postage, free shipping to the buyer.
        # 22.62 - 9.5% - 0.45 flat - 12.60 blank - 6.00 postage
        self.assertAlmostEqual(
            self.m.net_of(22.62, 12.60, self.FEES, 6.00, 0.0), 1.4211, places=4)
        self.assertAlmostEqual(
            self.m.net_of(29.99, 12.60, self.FEES, 6.00, 0.0), 8.09095, places=4)

    def test_a_dearer_blank_needs_a_dearer_price(self):
        cheap = self.m.price_for(8.0, 12.60, self.FEES, 6.00, 0.0)
        dear = self.m.price_for(8.0, 19.26, self.FEES, 6.00, 0.0)
        self.assertGreater(dear, cheap)
        self.assertAlmostEqual(dear - cheap, (19.26 - 12.60) / 0.905, places=3)

    def test_charging_postage_lowers_the_price_needed(self):
        free = self.m.price_for(8.0, 12.60, self.FEES, 6.00, 0.0)
        charged = self.m.price_for(8.0, 12.60, self.FEES, 6.00, 6.00)
        self.assertAlmostEqual(free - charged, 6.00, places=2)

    def test_a_fee_table_that_takes_everything_has_no_answer(self):
        self.assertIsNone(self.m.price_for(
            8.0, 1.0, dict(self.FEES, transaction_pct=0.99, processing_pct=0.02)))

    def test_the_target_reaches_the_output_with_the_gap_to_the_market(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        state = root / "agents" / "emily" / "state"
        state.mkdir(parents=True)
        (root / "agents" / "scout" / "state" / "scans").mkdir(parents=True)
        (state / "printify-catalog.json").write_text(json.dumps({
            "_fees": {"ship_cost": 6.00},
            "tote": {"variant_ids": [1], "variant_titles": ["one size"],
                     "prices": {"1": 2262}, "costs": {"1": 12.60}}}))
        (root / "agents" / "scout" / "state" / "scans" / "s.json").write_text(
            json.dumps({"seed": "canvas tote bag",
                        "scanned_at": datetime.now(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "rows": [{"phrase": "canvas tote bag", "supply": 359529,
                                  "heat": 0.013, "pull": 0.0249, "price": 22.62,
                                  "match": 1.0, "returned": 25, "heat_n": 25,
                                  "pull_n": 23, "score": 0.0024}],
                        "excluded": []}))
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "emily-printify.py"), "market-price",
             "--product", "tote", "--market", "canvas tote bag",
             "--margin", "8.00"],
            capture_output=True, text=True,
            env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("TO KEEP $8.00 A SALE", r.stdout)
        # The column is padded, so match the number rather than "$29.89".
        self.assertRegex(r.stdout, r"\$\s*29\.89")
        self.assertIn("vs the market's $22.62", r.stdout)
        self.assertIn("positioning", r.stdout)


class ASavedScanIsFrozenAtTheRulesThatWroteIt(unittest.TestCase):
    """compare read six scans saved before the seller-tool labels and the
    price check existed, and picked these as each market's best:

        laptop sticker         laptop sticker jiji          $3.75
        canvas tote bag        canvas tote bag insert      $40.00
        sticker sheet          sticker sheet mockup         $6.00
        water bottle sticker   water bottle sticker design  $3.75
        funny coffee mug       funny coffee mug designs     $3.90

    A marketplace in Nigeria, a bag organiser at 76% match, and three
    digital markets. Every rule that catches them existed by then - just not
    when the files were written. Everything needed to re-judge is in the
    row, so the rules are applied on READ and a scan improves when they do.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.scans = self.root / "agents" / "scout" / "state" / "scans"
        self.scans.mkdir(parents=True)
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        self.m = load("market_scan", SCRIPTS / "market-scan.py")

    def row(self, phrase, supply, heat, price, score, match=1.0):
        return {"phrase": phrase, "supply": supply, "heat": heat, "pull": 0.05,
                "price": price, "match": match, "returned": 25, "heat_n": 25,
                "pull_n": 25, "score": score}

    def write(self, seed, rows):
        (self.scans / f"{seed.replace(' ', '-')}.json").write_text(json.dumps({
            "seed": seed,
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": rows, "excluded": []}))

    def test_the_offsite_marketplace_is_not_a_market(self):
        self.write("laptop sticker", [
            self.row("laptop sticker jiji", 82, 0.043, 3.75, 0.0223),
            self.row("laptop stickers", 944997, 0.022, 4.50, 0.0037)])
        got = self.m.best_row(json.loads(
            (self.scans / "laptop-sticker.json").read_text()))
        self.assertEqual(got["phrase"], "laptop stickers")

    def test_a_genuinely_loose_match_is_dropped_on_read(self):
        self.write("canvas tote bag", [
            self.row("canvas tote bag japan", 3978, 0.090, 25.0, 0.030,
                     match=0.24),
            self.row("canvas tote bag embroidered", 18149, 0.062, 23.84, 0.0145)])
        got = self.m.best_row(json.loads(
            (self.scans / "canvas-tote-bag.json").read_text()))
        self.assertEqual(got["phrase"], "canvas tote bag embroidered")

    def test_a_different_product_at_a_good_match_is_NOT_caught(self):
        # Recorded because it is a real limit, not an oversight.
        #
        # 'canvas tote bag insert' is an organiser that goes INSIDE a tote.
        # Its match is 76% - well clear of the loose threshold - and at $40
        # against a $22.62 market it is dearer, not cheaper, so the price
        # check does not see it either. Both checks are working; neither is
        # for this.
        #
        # Nothing here can tell "a thing that goes in a tote" from "a tote".
        # That is a job for the person reading the table, and pretending
        # otherwise would mean tuning a threshold until one case passed.
        self.write("canvas tote bag", [
            self.row("canvas tote bag insert", 395, 0.052, 40.0, 0.0199,
                     match=0.76),
            self.row("canvas tote bag embroidered", 18149, 0.062, 23.84, 0.0145)])
        got = self.m.best_row(json.loads(
            (self.scans / "canvas-tote-bag.json").read_text()))
        self.assertEqual(got["phrase"], "canvas tote bag insert")

    def test_the_seller_tool_is_dropped_on_read(self):
        self.write("sticker sheet", [
            self.row("sticker sheet mockup", 219, 0.034, 6.0, 0.0144),
            self.row("sticker sheet custom", 17120, 0.013, 12.0, 0.0030)])
        got = self.m.best_row(json.loads(
            (self.scans / "sticker-sheet.json").read_text()))
        self.assertEqual(got["phrase"], "sticker sheet custom")

    def test_the_price_outlier_is_dropped_on_read(self):
        # 'funny coffee mug designs' at $3.90 in a $19.70 market. Its words
        # are innocent and its match is 96% - only the price gives it away.
        self.write("funny coffee mug", [
            self.row("funny coffee mug designs", 4143, 0.022, 3.90, 0.0060,
                     match=0.96),
            self.row("funny coffee mug for men", 31941, 0.003, 19.95, 0.0006),
            self.row("funny coffee mug coworker", 52354, 0.002, 21.98, 0.0004),
            self.row("funny coffee mugs", 452510, 0.001, 19.99, 0.0002),
            self.row("funny coffee cups", 280142, 0.001, 19.46, 0.0001)])
        got = self.m.best_row(json.loads(
            (self.scans / "funny-coffee-mug.json").read_text()))
        self.assertEqual(got["phrase"], "funny coffee mug for men")

    def test_a_scan_left_with_nothing_is_named_rather_than_vanishing(self):
        # Silently dropping a market reads as "never scanned it", which is a
        # different thing from "everything in it was a seller tool".
        self.write("sticker sheet", [
            self.row("sticker sheet mockup", 219, 0.034, 6.0, 0.0144)])
        self.write("canvas tote bag", [
            self.row("canvas tote bag", 359529, 0.013, 22.62, 0.0024)])
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "market-scan.py"), "compare"],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nothing usable left in: sticker sheet", r.stdout)
        self.assertIn("Re-scan", r.stdout)

    def test_the_survivor_is_what_the_table_reports(self):
        self.write("canvas tote bag", [
            self.row("canvas tote bag japan", 3978, 0.09, 40.0, 0.0199,
                     match=0.24),
            self.row("canvas tote bag", 359529, 0.013, 22.62, 0.0024)])
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "market-scan.py"), "compare"],
            capture_output=True, text=True, env=self.env).stdout
        self.assertIn("$22.62", out)
        self.assertNotIn("$40.00", out)
        self.assertNotIn("Dearest item: canvas tote bag ($40.00)", out)


class AToteIsNotASticker(unittest.TestCase):
    """cmd_providers printed '--product sticker' whatever blueprint you
    asked about:

        emily-printify.py providers 1389        (Tote Bag (AOP))
        -> emily-printify.py pick --product sticker --blueprint 1389 ...

    A command that runs, does the wrong thing, and is wrong nowhere you
    would look: the tote gets saved under the sticker entry, and every
    later `--product sticker` resolves to a bag.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_printify", SCRIPTS / "emily-printify.py")

    def test_the_blueprint_that_found_this(self):
        self.assertEqual(self.m.product_word("Tote Bag (AOP)"), "tote")

    def test_the_real_blueprint_titles(self):
        for title, want in [
                ("Kiss-Cut Stickers", "sticker"),
                ("Sticker Sheets", "sticker"),
                ("Cotton Tote Bag", "tote"),
                ("Woven Tote", "tote"),
                ("Adjustable Tote Bag (AOP)", "tote"),
                ("Square Vinyl Stickers", "sticker"),
                ("Holographic Die-cut Stickers", "sticker")]:
            with self.subTest(title=title):
                self.assertEqual(self.m.product_word(title), want)

    def test_sweatshirt_is_not_read_as_shirt(self):
        # Longest-first, or 'Unisex Hooded Sweatshirt' registers as a shirt
        # and resolve() then has two entries answering to the same word.
        self.assertEqual(self.m.product_word("Unisex Heavy Blend Hooded Sweatshirt"),
                         "sweatshirt")
        # 'Crewneck Sweatshirt' answers to sweatshirt, and should: a
        # crewneck IS one, and the broader word is the better entry name.
        # The narrower words are there for a blueprint that says only
        # 'Crewneck'.
        self.assertEqual(self.m.product_word("Crewneck Sweatshirt"), "sweatshirt")
        self.assertEqual(self.m.product_word("Heavyweight Crewneck"), "crewneck")
        self.assertEqual(self.m.product_word("Unisex Jersey Short Sleeve Tee"), "tee")

    def test_the_order_decides_between_two_words_in_one_title(self):
        # Not the boundary - both are whole words in this title. The list
        # order is what makes the broader category win.
        self.assertEqual(self.m.PRODUCT_WORDS.index("sweatshirt"),
                         self.m.PRODUCT_WORDS.index("crewneck") - 1)
        self.assertEqual(self.m.product_word("Crewneck Sweatshirt"), "sweatshirt")

    def test_a_word_inside_a_longer_word_is_not_a_match(self):
        # 'Magnetic' is not a magnet and 'Pillowcase' is not a pillow. A
        # substring search finds both and names the blueprint wrongly.
        self.assertIsNone(self.m.product_word("Magnetic Bottle Opener"))
        self.assertIsNone(self.m.product_word("Teether Ring"))

    def test_the_parenthetical_is_not_searched(self):
        # '(AOP)' and '(DTG)' are process notes, not products. A blueprint
        # called 'Poster (in a Tote-style tube)' must not become a tote.
        self.assertEqual(self.m.product_word("Matte Poster (Tote-style tube)"),
                         "poster")

    def test_an_unknown_product_gets_no_guess(self):
        # Better to ask than to invent a name that quietly collides with an
        # entry that already exists.
        self.assertIsNone(self.m.product_word("Something Unheard Of"))
        self.assertIsNone(self.m.product_word(""))
        self.assertIsNone(self.m.product_word(None))

    def test_the_suggestion_no_longer_hardcodes_a_product(self):
        code = "\n".join(
            l for l in (SCRIPTS / "emily-printify.py").read_text().splitlines()
            if not l.lstrip().startswith("#"))
        providers = code.split("def cmd_providers")[1].split("\ndef ")[0]
        self.assertNotIn("--product sticker", providers)
        self.assertIn("product_word", providers)


class TheFeeTableIsNotAProduct(unittest.TestCase):
    """The fee table lives at the top level of the catalogue beside the
    product entries, and everything that enumerated the catalogue counted it
    as one:

        The catalogue already has:
          _fees          blueprint None
          hoodie         blueprint 77
          ...
        If one of those IS this product, say so rather than picking it twice:
          emily-printify.py alias _fees tote

    Following that advice would have aliased the fee table to a tote bag.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = load("emily_printify", SCRIPTS / "emily-printify.py")

    CAT = {"_fees": {"transaction_pct": 0.065, "ship_cost": 6.0},
           "hoodie": {"blueprint_id": 77, "aliases": ["sweatshirt"]},
           "kisscut": {"blueprint_id": 400},
           "sticker": {"blueprint_id": 564}}

    def test_metadata_is_not_listed_as_a_product(self):
        self.assertEqual(sorted(k for k, _v in self.m.products(self.CAT)),
                         ["hoodie", "kisscut", "sticker"])

    def test_any_underscore_key_is_housekeeping(self):
        self.assertTrue(self.m.is_meta("_fees"))
        self.assertTrue(self.m.is_meta("_anything_later"))
        self.assertFalse(self.m.is_meta("sticker"))
        self.assertFalse(self.m.is_meta("t-shirt"))

    def test_a_non_dict_value_is_not_a_product_either(self):
        cat = dict(self.CAT, version=3, notes="hello")
        self.assertEqual(sorted(k for k, _v in self.m.products(cat)),
                         ["hoodie", "kisscut", "sticker"])

    def test_an_empty_or_missing_catalogue_is_no_products(self):
        self.assertEqual(self.m.products({}), [])
        self.assertEqual(self.m.products(None), [])

    def test_resolve_cannot_return_the_fee_table(self):
        for word in ("_fees", "fees"):
            with self.subTest(word=word):
                _key, entry = self.m.resolve(self.CAT, word)
                self.assertIsNone(entry)

    def test_the_message_does_not_offer_to_alias_it(self):
        said = self.m.no_entry(self.CAT, "tote")
        self.assertNotIn("_fees", said)
        self.assertIn("hoodie", said)

    def test_a_real_product_still_resolves(self):
        _key, entry = self.m.resolve(self.CAT, "sweatshirt")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["blueprint_id"], 77)


class TheGateBelongsWhereTheDataEntersTheLog(unittest.TestCase):
    """Scout was told in bold never to write proposals.json. On 2026-09-23 it
    wrote it by hand - its own tool log says read and write, the propose
    script was never called - with three ideas carrying no phrase, no
    evidence, a free-text product of 'all-over-print canvas tote bag', and a
    stray newline inside a JSON string that broke the file.

    propose refuses an idea with no measurement. merge did not: it took
    whatever was in the file. So every guarantee the evidence gate makes was
    one hand-written file away from being void.

    Rewording the instruction has never fixed this class of thing here - it
    is the third agent told to run a tool that wrote a file instead - so the
    rule moved to where it cannot be walked around.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = self.root / "agents" / "scout" / "state"
        (self.state / "scans").mkdir(parents=True)
        (self.state / "ideas.json").write_text('{"ideas":[]}')
        (self.state / "scans" / "s.json").write_text(json.dumps({
            "seed": "canvas tote bag",
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [{"phrase": "canvas tote bag", "supply": 359529,
                      "heat": 0.013, "pull": 0.0249, "price": 22.62,
                      "match": 1.0, "returned": 25, "heat_n": 25,
                      "pull_n": 23, "score": 0.0024},
                     {"phrase": "canvas tote bag kids", "supply": 31184,
                      "heat": 0.0, "pull": 0.0, "price": 19.99, "match": 1.0,
                      "returned": 25, "heat_n": 25, "pull_n": 25,
                      "score": 0.0}],
            "excluded": []}))
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def hand_write(self, rows):
        (self.state / "proposals.json").write_text(json.dumps({"proposals": rows}))

    def run_it(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-ideas.py"), *args],
            capture_output=True, text=True, env=self.env)

    def log(self):
        return json.loads((self.state / "ideas.json").read_text())["ideas"]

    def pending(self):
        d = json.loads((self.state / "proposals.json").read_text())
        return d.get("proposals", []) if isinstance(d, dict) else d

    def test_scouts_three_hand_written_ideas_are_all_refused(self):
        self.hand_write([
            {"title": "Botanical Map All-Over Tote",
             "product": "all-over-print canvas tote bag",
             "angle": "Layered hand-drawn map of a local park."},
            {"title": "Metro Tile Pattern Tote", "product": "all-over-print canvas tote bag"},
            {"title": "Rainy Window Watercolor Scene Tote",
             "product": "all-over-print canvas tote bag"}])
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("3 proposal(s) REFUSED", r.stderr)
        self.assertIn("no evidence", r.stderr)
        self.assertEqual(self.log(), [], "nothing may reach the log")

    def test_a_refused_idea_is_kept_not_deleted(self):
        # A refusal that also deletes the work is how an agent learns to
        # stop telling you.
        self.hand_write([{"title": "Botanical Map All-Over Tote", "product": "tote"}])
        self.run_it("merge")
        self.assertEqual([r["title"] for r in self.pending()],
                         ["Botanical Map All-Over Tote"])

    def test_a_refused_idea_survives_a_merge_that_also_succeeds(self):
        # THE BUG IN THE FIX, found by running it: the end of merge empties
        # proposals.json, which wiped the refused rows a moment after the
        # message promised they were kept.
        self.run_it("propose", "--phrase", "canvas tote bag",
                    "--title", "A Measured One", "--product", "tote")
        rows = self.pending()
        rows.append({"title": "Hand Written One", "product": "tote"})
        self.hand_write(rows)
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([i["title"] for i in self.log()], ["A Measured One"])
        self.assertEqual([r["title"] for r in self.pending()],
                         ["Hand Written One"])

    def test_evidence_naming_an_unmeasured_phrase_is_refused(self):
        # Forging the block is no better than omitting it.
        self.hand_write([{"title": "Invented", "product": "tote",
                          "evidence": {"phrase": "solid gold tote",
                                       "supply": 3, "favs_per_day": 99.0}}])
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has not been measured", r.stderr)
        self.assertEqual(self.log(), [])

    def test_evidence_naming_a_dead_market_is_refused(self):
        self.hand_write([{"title": "Kids Tote", "product": "tote",
                          "evidence": {"phrase": "canvas tote bag kids"}}])
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 1)
        self.assertIn("scored zero", r.stderr)
        self.assertEqual(self.log(), [])

    def test_a_trademark_in_a_hand_written_row_is_refused(self):
        self.hand_write([{"title": "Pikmin Bloom Tote", "product": "tote",
                          "evidence": {"phrase": "canvas tote bag"}}])
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 1)
        self.assertIn("property", r.stderr)
        self.assertEqual(self.log(), [])

    def test_the_proper_door_is_unaffected(self):
        r = self.run_it("propose", "--phrase", "canvas tote bag",
                        "--title", "Botanical Map All-Over Tote",
                        "--product", "tote")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.run_it("merge")
        self.assertEqual(m.returncode, 0, m.stderr)
        self.assertEqual(self.log()[0]["evidence"]["supply"], 359529)

    def test_the_refusal_says_how_to_do_it_properly(self):
        self.hand_write([{"title": "X", "product": "tote"}])
        said = self.run_it("merge").stderr
        # It now hands back a usable line and the phrases to choose from,
        # rather than naming two more commands to go and run.
        self.assertIn("Their words are not lost", said)
        self.assertIn("<phrase> | X | tote", said)
        self.assertIn("Measured phrases, best first", said)
        self.assertIn("canvas tote bag", said)


class TheModelCannotRunTheScript(unittest.TestCase):
    """Twice Scout read the instructions, understood them, and wrote
    proposals.json by hand anyway. The second time it said why:

        "tell me whether to run it and provide approval to execute shell
         commands"

    and its tool log showed an exec attempt with a failure. It was not
    refusing. It was blocked, and then doing the only thing left to it.

    Emily wrote 49-byte text files named design.png, Fury wrote its
    briefing, Scout cleared MEMORY.md twice. Every time, rewording failed
    and moving the job to code worked. So Scout writes ONE PLAIN TEXT FILE -
    the thing it does reliably - and intake does the JSON, the evidence
    lookup and the refusals.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = self.root / "agents" / "scout" / "state"
        (self.state / "scans").mkdir(parents=True)
        (self.state / "ideas.json").write_text('{"ideas":[]}')
        (self.state / "scans" / "s.json").write_text(json.dumps({
            "seed": "canvas tote bag",
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [{"phrase": "canvas tote bag", "supply": 359529,
                      "heat": 0.013, "pull": 0.0249, "price": 22.62,
                      "match": 1.0, "returned": 25, "heat_n": 25,
                      "pull_n": 23, "score": 0.0024},
                     {"phrase": "canvas tote bag kids", "supply": 31184,
                      "heat": 0.0, "pull": 0.0, "price": 19.99, "match": 1.0,
                      "returned": 25, "heat_n": 25, "pull_n": 25, "score": 0.0}],
            "excluded": []}))
        self.drafts = self.state / "drafts.txt"
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def run_it(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-ideas.py"), *args],
            capture_output=True, text=True, env=self.env)

    def filed(self):
        p = self.state / "proposals.json"
        if not p.is_file() or not p.read_text().strip():
            return []
        d = json.loads(p.read_text())
        return d.get("proposals", []) if isinstance(d, dict) else d

    def test_scouts_three_real_ideas_go_in_as_text(self):
        self.drafts.write_text(
            "# phrase | title | product | angle\n"
            "canvas tote bag | Vintage Coastal Collage Tote | tote | torn paper\n"
            "canvas tote bag | Market Map Folded-City Tote | tote | folded map\n"
            "canvas tote bag | Rainlight Ceramic Tile Tote | tote | glazed tile\n")
        r = self.run_it("intake")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("3 filed, 0 refused", r.stdout)
        rows = self.filed()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["evidence"]["supply"], 359529)
        self.assertEqual(rows[0]["angle"], "torn paper")

    def test_every_refusal_reaches_the_line_it_came_from(self):
        self.drafts.write_text(
            "canvas tote bag | Good One | tote | fine\n"
            "canvas tote bag kids | Dead Market | tote | scored zero\n"
            "solid gold tote | Never Measured | tote | invented\n"
            "canvas tote bag | Pikmin Bloom Tote | tote | not ours\n"
            "this line is broken\n")
        r = self.run_it("intake")
        self.assertIn("1 filed, 4 refused", r.stdout)
        self.assertIn("line 2", r.stderr)
        self.assertIn("scored zero", r.stderr)
        self.assertIn("line 3", r.stderr)
        self.assertIn("has not been measured", r.stderr)
        self.assertIn("line 4", r.stderr)
        self.assertIn("property", r.stderr)
        self.assertIn("line 5", r.stderr)
        self.assertIn("at least", r.stderr)
        self.assertEqual(len(self.filed()), 1)

    def test_blank_lines_and_comments_are_skipped(self):
        self.drafts.write_text(
            "# a comment\n\n   \n"
            "canvas tote bag | Only One | tote | yes\n\n")
        r = self.run_it("intake")
        self.assertIn("1 filed, 0 refused", r.stdout)

    def test_a_missing_angle_is_fine(self):
        self.drafts.write_text("canvas tote bag | Bare Minimum | tote\n")
        r = self.run_it("intake")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.filed()), 1)

    def test_a_brief_in_the_fifth_field_is_carried(self):
        self.drafts.write_text(
            "canvas tote bag | With Brief | tote | an angle | and a brief\n")
        self.run_it("intake")
        self.assertEqual(self.filed()[0]["brief"], "and a brief")

    def test_the_file_is_emptied_so_nothing_is_filed_twice(self):
        self.drafts.write_text("canvas tote bag | Once Only | tote | yes\n")
        self.run_it("intake")
        self.run_it("intake")
        self.assertEqual(len(self.filed()), 1)
        self.assertEqual(self.drafts.read_text().strip(), "")

    def test_no_drafts_file_is_not_an_error(self):
        r = self.run_it("intake")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to take in", r.stdout)

    def test_intake_runs_in_the_cycle_before_the_merge(self):
        # Scout never calls either; the cycle script does, in that order, or
        # the drafts sit on disk unread.
        cycle = (SCRIPTS / "scout-cycle.sh").read_text()
        self.assertIn("scout-ideas.py\" intake", cycle)
        self.assertLess(cycle.index('scout-ideas.py" intake'),
                        cycle.index('scout-ideas.py" merge'))

    def test_the_header_tells_it_to_write_text_not_run_a_script(self):
        head = (ROOT / "agents" / "scout" / "_scout-agents-header.md").read_text()
        self.assertIn("state/drafts.txt", head)
        self.assertIn("DO NOT run any script", head)
        self.assertNotIn("RUN, once per new idea", head)


class ARefusalThatIsADeadEndLosesTheWork(unittest.TestCase):
    """Scout's words - the title, the angle - are the part a model is
    actually for. Refused, they sat in proposals.json with no way forward
    but retyping them, and Scout produced three fresh ones the next run
    instead of recovering the last three.

    The one thing missing from a hand-written row is the phrase. So the
    refusal hands them back as drafts.txt lines with the phrase left to
    choose, and lists the measured phrases to choose from.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = self.root / "agents" / "scout" / "state"
        (self.state / "scans").mkdir(parents=True)
        (self.state / "ideas.json").write_text('{"ideas":[]}')
        (self.state / "scans" / "t.json").write_text(json.dumps({
            "seed": "canvas tote bag",
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [
                {"phrase": "canvas tote bag", "supply": 359529, "heat": 0.013,
                 "pull": 0.02, "price": 22.62, "match": 1.0, "returned": 25,
                 "heat_n": 25, "pull_n": 23, "score": 0.0024},
                {"phrase": "canvas tote bag embroidered", "supply": 18149,
                 "heat": 0.062, "pull": 0.035, "price": 23.84, "match": 1.0,
                 "returned": 25, "heat_n": 25, "pull_n": 23, "score": 0.0145}],
            "excluded": []}))
        self.env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))

    def merge_with(self, rows):
        (self.state / "proposals.json").write_text(json.dumps({"proposals": rows}))
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "scout-ideas.py"), "merge"],
            capture_output=True, text=True, env=self.env)

    def test_the_words_come_back_as_a_draft_line(self):
        r = self.merge_with([
            {"title": "Coastal Tide Botanical Collage Tote",
             "product": "all-over-print canvas tote bag",
             "angle": "torn-paper shoreline with pressed seaweed"}])
        self.assertIn("Their words are not lost", r.stderr)
        self.assertIn("<phrase> | Coastal Tide Botanical Collage Tote | tote "
                      "| torn-paper shoreline with pressed seaweed", r.stderr)

    def test_the_free_text_product_becomes_a_catalogue_word(self):
        # 'all-over-print canvas tote bag' is not a product Emily can
        # resolve. The word list is emily-printify's, imported rather than
        # written twice.
        r = self.merge_with([{"title": "X",
                              "product": "all-over-print canvas tote bag"}])
        line = [l for l in r.stderr.splitlines() if l.strip().startswith("<phrase>")][0]
        self.assertTrue(line.strip().endswith("| tote"), line)
        self.assertNotIn("all-over-print", line)

    def test_the_measured_phrases_are_listed_best_first(self):
        r = self.merge_with([{"title": "X", "product": "tote"}])
        tail = r.stderr.split("Measured phrases, best first:")[1]
        rows = [l.strip() for l in tail.splitlines() if l.strip()]
        self.assertEqual(rows[:2],
                         ["canvas tote bag embroidered", "canvas tote bag"])

    def test_with_nothing_measured_it_says_to_scan(self):
        for f in (self.state / "scans").glob("*.json"):
            f.unlink()
        r = self.merge_with([{"title": "X", "product": "tote"}])
        self.assertIn("Nothing has been measured yet", r.stderr)
        self.assertIn("market-scan.py scan", r.stderr)

    def test_a_missing_angle_still_produces_a_usable_line(self):
        r = self.merge_with([{"title": "Bare", "product": "tote"}])
        self.assertIn("<phrase> | Bare | tote", r.stderr)
        self.assertFalse(r.stderr.rstrip().endswith("|"),
                         "no dangling separator on an empty field")

    def test_the_brief_is_used_when_there_is_no_angle(self):
        r = self.merge_with([{"title": "B", "product": "tote",
                              "brief": "a brief instead"}])
        self.assertIn("a brief instead", r.stderr)

    def test_an_unknown_product_is_left_as_written_not_guessed(self):
        r = self.merge_with([{"title": "X", "product": "something unheard of"}])
        line = [l for l in r.stderr.splitlines() if l.strip().startswith("<phrase>")][0]
        self.assertIn("something unheard of", line)
