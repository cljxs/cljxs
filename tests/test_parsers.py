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

import collections
import copy
import contextlib
import io
import math
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
import sqlite3
import time
import urllib.error
import urllib.request
import unittest
import unittest.mock
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


def rising_qqq(price=500.0):
    """QQQ above a rising 50-day average: a favourable regime for
    belfort-trade.py, so a test about something else is not sized down."""
    closes = [400.0 + i for i in range(70)]
    return {"price": price, "sma50": sum(closes[-50:]) / 50,
            "history": [[f"2026-07-{1 + i // 3:02d}", c] for i, c in enumerate(closes)]}


def fresh_news(*symbols):
    """news.json with one headline per name, id "h-<NAME>", published now."""
    from email.utils import format_datetime
    stamp = format_datetime(datetime.now(timezone.utc))
    return {"headlines": [{"id": f"h-{s}", "symbol": s, "title": f"EARNINGS: {s} raised guidance",
                           "published": stamp, "publisher": "example.com"} for s in symbols]}


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

    def test_the_daily_cap_on_the_key_is_named_as_the_cap(self):
        # 2026-09-26: Belfort's Friday close run exited 1 on the first day of
        # the owner's $1/day OpenRouter cap. OpenRouter's refusal wording, as
        # reported by other OpenRouter users, was matched by nothing here, so
        # a spent budget would read as an agent that skipped its work.
        for line in ('{"error":{"message":"Key limit exceeded (daily limit). Manage it using '
                     'https://openrouter.ai/settings/keys","code":403}}',
                     "403 Key limit exceeded. Manage it using https://openrouter.ai/keys",
                     "OpenRouter 403: API key budget limit exceeded (monthly limit)"):
            with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
                f.write(line + "\n")
            r = self.run_checker(f.name)
            self.assertEqual(r.returncode, 2, line)
            self.assertIn("DAILY SPENDING CAP REACHED", r.stdout, line)
            self.assertIn("7pm Central", r.stdout)

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

    def test_small_art_on_a_big_plain_field_is_art(self):
        # Emily's "Paws & Kissies" sticker (2026-10-07): a paw and one line of
        # lettering on a light-blue field, 92.2% background, refused as "the
        # artwork itself matched the background" under the old 92% line. Same
        # colours, same share: about 8% of the frame is art.
        W = H = 100
        def art(x, y):
            if (x - 50) ** 2 + (y - 38) ** 2 < 11 ** 2:
                return (251, 238, 214)                          # the cream paw
            if 30 <= x < 70 and 62 <= y < 67 and x % 5 != 4:
                return (122, 31, 38)                            # the lettering
            return (168, 217, 231)                              # the field
        p = self.png("paws.png", W, H, art)
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"), str(p), str(self.d / "paws-cut.png")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        removed = float(re.search(r"\(([\d.]+)%\)", r.stdout).group(1))
        self.assertGreater(removed, 92.0, "the fixture must be past the old line, or it tests nothing")
        self.assertIn("trim: 100x100 ->", r.stdout, "and it is cropped to the art, so it prints full size")

    def test_a_speck_on_a_blank_field_is_still_refused(self):
        p = self.png("speck.png", 100, 100, lambda x, y: (20, 20, 20) if 50 <= x < 55 and 50 <= y < 55 else (255, 255, 255))
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"), str(p), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("the artwork itself matched the background", r.stderr)

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
    preflight.py reports as stale/broken. A ONE-RUN focus is an argument: it
    is written to no file and nothing remembers it.

    A STANDING focus ("totes until we find another market") exists too, and
    answers the same worry differently: it lives in one file, is printed at
    the top of every run, is shown in Scout's house, and is cleared by one
    command. A focus nobody can see is the problem; this one is always seen.

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
        # Unfocused unless asked: by words on the command line for one run,
        # or by the standing focus scout-ideas.py owns. Nothing else.
        src = self.WRAPPER.read_text()
        self.assertIn('MESSAGE="scheduled idea run"', src)
        self.assertIn('FOCUS_ON="$*"', src)
        self.assertIn("focus --word", src)

    # A focus reaching the message is tested end to end, through the real
    # wrapper, in TheStandingFocusAndTheCountAreCode. This used to run a copy
    # of the wrapper's bash typed into the test, which tested the copy.

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
                      "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25, "pull_n": 25,
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
                      "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25, "pull_n": 25,
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

        def fake_generate(path, prompt, key, model=None, product="", **_kw):
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
        # More baseball than the window holds (it was 8 games then, 24 now).
        for i in range(self.m.MAX_GAMES + 6):
            self.game("mlb", f"mlb{i}", 1 + i * 0.05)
        for i in range(6):
            self.game("cfb", f"cfb{i}", 5 + i * 0.1)
        _out, games = self.slate()
        sports = {s for s, _ in games}
        self.assertIn("cfb", sports, "college football must reach the slate")
        self.assertLessEqual(len([s for s, _ in games if s == "mlb"]),
                             self.m.MAX_GAMES - 1)

    def test_one_sport_alone_still_fills_the_window(self):
        # Sharing must not mean holding slots empty for a sport with no games.
        for i in range(self.m.MAX_GAMES + 4):
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
        # Read at the moment a bet is recorded; a crash there would cost a
        # cycle over a file that is not there yet. The book counts from
        # state/bets.jsonl now, skipping a line it cannot read.
        with tempfile.TemporaryDirectory() as tmp:
            b = load("ace_book_cap", "ace-book.py")
            path = Path(tmp) / "bets.jsonl"
            self.assertEqual(len(b.fold(b.events(path))["open"]), 0)
            path.write_text("{ not json\n" + "".join(
                json.dumps({"kind": "bet", "id": f"b{i}", "stake": 100}) + "\n" for i in range(3)))
            self.assertEqual(len(b.fold(b.events(path))["open"]), 3)

    def test_bet_refuses_once_the_cap_is_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = load("ace_book_cap2", "ace-book.py")
            b.BETS, b.BANK = Path(tmp) / "bets.jsonl", Path(tmp) / "bankroll.json"
            for i in range(self.m.MAX_OPEN_BETS - 1):
                b.append({"kind": "bet", "id": f"b{i}", "sport": "nfl", "event_id": str(i), "stake": 100})
            self.assertIsNone(b.refusal("nfl", "99", "X ML"))
            b.append({"kind": "bet", "id": "last", "sport": "nfl", "event_id": "98", "stake": 100})
            self.assertIn(f"the cap is {self.m.MAX_OPEN_BETS}", b.refusal("nfl", "99", "X ML"))
        src = (SCRIPTS / "ace-judge.py").read_text()
        body = src.split("def record(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("b.refusal(", body, "record() must ask the book before a bet")


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
        # The bar is expected value at the estimate, so a bet without one
        # claims to clear it with nothing on record to check.
        (self.m.AGENT / "state" / "bankroll.json").write_text(json.dumps({"bankroll": 10000.0}))
        class A:
            pass
        a = A()
        a.number, a.why, a.my_pct, a.stake = "1", "SP scratched", None, None
        self.assertEqual(self.m.record(a, "bet"), 1)
        # With one, it is judged on what a bet needs next - the news it cites -
        # not sent back for the estimate it now has.
        a.my_pct = 71.0
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.m.record(a, "bet"), 1)
        self.assertNotIn("--my-pct", err.getvalue())
        self.assertIn("--event", err.getvalue())

    def test_a_bet_is_refused_for_bet_reasons_not_pass_reasons(self):
        # Guarding the pass rule on status is what keeps the advice correct.
        # Told to "pass it with --my-pct", an agent holding a real edge would
        # do the one thing it should not - and being given the wrong next step
        # is a failure this repo has already paid for twice today.
        import contextlib, io
        class A:
            pass
        a = A()
        a.number, a.why, a.my_pct, a.stake = "1", "SP scratched", None, None
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


class EveryGameIsEstimatedBlindFirst(unittest.TestCase):
    """2026-10-01, from an outside review. "At least 3 estimates a cycle" let
    Ace choose which games to estimate - so the sample said as much about
    what he found interesting as about whether he can forecast - and every
    estimate was made after reading the line, which measures anchoring, not
    knowledge. Now every game in the next 30 hours gets a home-win chance from
    data/blind.json, a sheet with no prices on it, before any price is shown.
    (Replaces ACycleOwesEstimates.)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agent = self.root / "agents/ace"
        (self.agent / "state").mkdir(parents=True)
        (self.agent / "data").mkdir(parents=True)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.j = load("ace_judge_blind", "ace-judge.py")
        self.start = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%MZ")
        (self.agent / "data/blind.json").write_text(json.dumps({"games": [
            {"n": n, "sport": "cfb", "event_id": str(400 + n), "match": f"A{n} @ H{n}",
             "starts_utc": self.start, "home": {"abbr": f"H{n}"}, "away": {"abbr": f"A{n}"}}
            for n in (1, 2, 3)]}))
        (self.agent / "data/candidates.json").write_text(json.dumps({"candidates": [
            {"selection": "H1 ML", "match": "A1 @ H1", "price": -150, "novig_pct": 57.0, "sport": "cfb"}]}))

    def run_j(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "ace-judge.py"), *args], capture_output=True,
                              text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))

    def test_prices_are_refused_until_every_game_has_one(self):
        r = self.run_j("list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("blind estimates first: 3 game(s)", r.stderr)
        self.assertEqual(self.run_j("blind", "1:55,2:41").returncode, 0)
        r = self.run_j("pass", "1", "--my-pct", "50", "--why", "x")
        self.assertIn("(3)", r.stderr, "names the one still owed")
        self.assertEqual(self.run_j("rest", "--why", "x").returncode, 1)
        self.assertEqual(self.run_j("blind", "3:62").returncode, 0)
        self.assertIn("H1 ML", self.run_j("list").stdout)

    def test_a_bad_entry_records_nothing(self):
        for bad, why in (("1:55,4:50", "there is no game 4"), ("1:0", "between 1 and 99"),
                         ("1:55,two", "is not number:percent")):
            r = self.run_j("blind", bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn(why, r.stderr)
        self.assertFalse((self.agent / "state/blind.jsonl").exists())

    def test_what_is_recorded(self):
        self.run_j("blind", "1:55,2:41,3:62")
        rows = [json.loads(l) for l in (self.agent / "state/blind.jsonl").read_text().splitlines()]
        self.assertEqual([(r["home"], r["home_pct"], r["event_id"]) for r in rows],
                         [("H1", 55.0, "401"), ("H2", 41.0, "402"), ("H3", 62.0, "403")])
        self.assertEqual(rows[0]["key"], f"cfb|A1 @ H1|{self.start}", "ace-clv's game key, for grading")

    def test_estimates_from_an_earlier_cycle_do_not_count(self):
        self.run_j("blind", "1:55,2:41,3:62")
        (self.agent / "state/.cycle-started").write_text(str(int(time.time()) + 5))
        self.assertEqual(self.j.blind_missing(), [1, 2, 3])

    def test_no_sheet_owes_nothing(self):
        (self.agent / "data/blind.json").unlink()
        self.assertEqual(self.j.blind_missing(), [])
        self.assertEqual(self.run_j("list").returncode, 0)

    def test_the_verifier_and_the_signoff_ask_one_function(self):
        av = load("ace_verify_blind", "ace-verify.py")
        problems, notes = [], []
        av.check_blind(problems, notes, None)
        self.assertIn("3 of 3 game(s) on data/blind.json have no blind estimate", problems[0])
        self.run_j("blind", "1:55,2:41,3:62")
        problems, notes = [], []
        av.check_blind(problems, notes, None)
        self.assertEqual((problems, notes), ([], ["blind: all 3 game(s) estimated"]))
        for f in ("ace-verify.py", "signoff.py"):
            self.assertIn("ace_judge.blind_missing(", (SCRIPTS / f).read_text(), f)

    def test_the_sheet_has_no_price_on_it(self):
        f = load("ace_fetch_blind", "ace-fetch.py")
        ctx = self.root / "ctx"
        ctx.mkdir()
        now = datetime.now(timezone.utc)
        def game(name, hours, status="STATUS_SCHEDULED", home=-150, away=130):
            (ctx / f"nfl-{name}.json").write_text(json.dumps({
                "sport": "nfl", "event_id": name, "short": name, "status": status,
                "start_utc": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%MZ"),
                "home": {"abbr": "H", "name": "Home Team", "record": "3-1"}, "away": {"abbr": "A", "record": "1-3"},
                "odds": {"moneyline_home": home, "moneyline_away": away, "novig_home_pct": 58.0,
                         "novig_away_pct": 42.0, "spread": -3.5, "over_under": 44.5, "details": "H -3.5"},
                "against_the_spread": [{"team": "H", "records": [["ATS", "3-1"]]}],
                "last_five": [{"team": "H", "result": "W", "score": "24-10", "opponent": "X"}]}))
        game("SOON", 3)
        game("TOMORROW", 26)
        game("BLOWOUT", 4, home=-5000, away=1800)
        game("TOOFAR", 40)
        game("STARTED", -1, status="STATUS_IN_PROGRESS")
        sheet = f.build_blind("2026-10-01", ctx, "afternoon", now)
        self.assertEqual([g["match"] for g in sheet["games"]], ["SOON", "BLOWOUT", "TOMORROW"],
                         "30 hours ahead, lopsided kept, started and far-off games out")
        self.assertEqual([g["n"] for g in sheet["games"]], [1, 2, 3])
        text = json.dumps(sheet["games"])
        for priced in ("moneyline", "novig", "spread", "over_under", "-3.5", "44.5", "ATS", "-150"):
            self.assertNotIn(priced, text)
        self.assertEqual(sheet["games"][0]["last_five"], {"H": ["W 24-10 v X"]})

    def test_the_header_says_so(self):
        header = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()
        self.assertIn('ace-judge.py blind "1:55,2:41,3:62,4:50"', header)
        self.assertIn("**every** game the HOME team's chance", header)
        self.assertRegex(header, r"the verifier fails a cycle that comes home without them")


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
        self.assertIn("layPath((PROPS_PLOT.x + 2.5) * T,", self.html)


class ClipHasAStudio(unittest.TestCase):
    """2026-09-28: Clip, the clipping agent, and Spotter, its research
    assistant, got a building. Like the props hall it is not in PLOTS, so
    the checks that keep the hall off the house plots are made for it too -
    otherwise the next agent is built on top of it the day it is added."""

    def setUp(self):
        self.html = (ROOT / "mission-control-api" / "public" / "village.html").read_text()

    def studio(self):
        m = re.search(r"CLIP_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html)
        return float(m.group(1)), float(m.group(2))

    def test_it_stands_on_nobody_elses_ground(self):
        sx, sy = self.studio()
        plots = [(float(a), float(b)) for a, b in
                 re.findall(r"\{ x:\s*([\d.]+),\s*y:\s*([\d.]+),\s*flip", self.html)]
        self.assertEqual(len(plots), 8)
        m = re.search(r"PROPS_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html)
        hall = (float(m.group(1)), float(m.group(2)), 5.0)
        for (px, py, pw) in [(x, y, 4.0) for x, y in plots] + [hall]:
            apart = (sx + 4.5 <= px or px + pw <= sx or sy + 4.4 <= py or py + 4.3 <= sy)
            self.assertTrue(apart, f"the studio overlaps the building at {px},{py}")

    def test_it_has_a_path_and_a_door_that_opens_its_panel(self):
        self.assertIn("layPath((CLIP_PLOT.x + 2.25) * T,", self.html)
        self.assertIn("doors.push({ name: CLIP_DOOR", self.html)
        body = self.html.split("function openPanel(", 1)[1]
        before = body.split("(DATA.agents || []).find", 1)[0]
        self.assertIn("openClipPanel()", before, "the studio is handled before the agent lookup")

    def test_clip_and_spotter_are_named(self):
        self.assertIn("[['Clip', ", self.html)
        self.assertIn("['Spotter', ", self.html)
        self.assertIn("if (n.label) n.label.setPosition", self.html, "the tags follow them")

    def test_the_api_runs_clipper_without_a_shell_and_holds_no_rules(self):
        js = (ROOT / "mission-control-api" / "clip.js").read_text()
        self.assertIn("execFile(python(), ['-m', 'clipper', ...args]", js)
        self.assertNotIn(".exec(", js)
        self.assertNotIn("better-sqlite3", js, "state comes from `clipper deck`, not a second reader")
        server = (ROOT / "mission-control-api" / "server.js").read_text()
        self.assertIn("clipRoutes.register(app);", server)

    def test_approving_happens_on_the_card_through_data_attributes(self):
        # One listener, ids in data attributes - never ids spliced into
        # onclick strings, the rule every other panel here follows.
        for kind in ("approve", "reject", "edit", "redraft", "copytiktok", "posted"):
            self.assertIn(f'data-clip="{kind}"', self.html)
        self.assertIn("$('clipClips').onclick", self.html)
        panel = self.html.split("function drawPost(", 1)[1].split("async function clipAct(", 1)[0]
        self.assertNotIn("onclick=", panel)
        js = (ROOT / "mission-control-api" / "clip.js").read_text()
        for route in ("approve", "reject", "edit", "redraft", "posted"):
            self.assertIn(f"app.post('/api/clip/{route}/", js)

    def test_approve_answers_before_the_upload(self):
        # 2026-10-07: the route waited for the YouTube upload, Safari gave up
        # first ("Could not reach the API: Load failed") and nothing was recorded.
        js = (ROOT / "mission-control-api" / "clip.js").read_text()
        route = js.split("app.post('/api/clip/approve/", 1)[1].split("app.post(", 1)[0]
        self.assertIn("['approve', req.params.id, '--later'", route)
        self.assertIn("spawn(python(), ['-m', 'clipper', 'publish']", route)
        self.assertIn("detached: true", route)
        self.assertIn(".unref()", route, "the reply must not wait for the upload")
        self.assertIn("const { execFile, spawn } = require('child_process');", js)

    def test_copying_a_caption_works_without_https(self):
        # The Deck is plain http over Tailscale, where navigator.clipboard
        # does not exist - relying on it would make the button do nothing.
        body = self.html.split("function copyText(", 1)[1].split("\n}", 1)[0]
        self.assertIn("execCommand('copy')", body)
        self.assertNotIn("navigator.clipboard", body)

    def test_the_panel_loads_on_open_and_is_not_redrawn_by_the_village_loop(self):
        pull = self.html.split("async function pull(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("drawClip", pull)
        self.assertNotIn("loadClip", pull)


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
        # sign and fence since 2026-09-29: a signpost stood on the end of
        # Belfort's name plate ("BELFOR-") and a fence in front of his bull,
        # both found by screenshot because neither kind was checked here.
        sizes = {"tree": (0.65, 2.4, 40), "bush": (0.50, 0.80, 8),
                 "rock": (0.40, 0.65, 6), "sign": (0.28, 0.85, 6),
                 "fence": (0.50, 0.80, 6)}
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
        m = re.search(r"CLIP_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html)
        x, y = float(m.group(1)), float(m.group(2))
        yield ("Clip's studio", x, y, 144 / T, 124 / T, y * T + 124,
               max(96, len("Clip") * 10 + 22) / T)
        m = re.search(r"STATUE = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html)
        x, y = float(m.group(1)), float(m.group(2))
        yield ("the statue", x - 2, y - 196 / T, 4.0, 196 / T, y * T, 0.0)

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
        self.assertEqual(sorted(k for k, *_ in found), ["bush", "fence", "rock", "sign", "tree"])
        for kind, pts, *_ in found:
            # three fence pieces since 2026-10-07: two stood across Ace's and Scout's paths
            self.assertGreaterEqual(len(pts), 3 if kind == "fence" else 5, f"only {len(pts)} {kind} found")

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


class NothingStandsOnAPath(NothingGrowsThroughAWall):
    """Owner, 2026-10-07: "make sure there are no gates or objects blocking
    paths". There were several. A path ran down the column NEAREST its door by
    rounding - up to a tile beside it - so the garden fence, gated at the door,
    crossed it like a shut gate. A rock and a barrel stood on Paul's path, a
    fence and a bush on Scout's, a market stall on Clip's way into the square,
    and three tree trunks in the edge of the high street. The two buildings
    south of the lane face away from it, and their paths ran under them into
    the back wall.

    Found by measuring every image and body in the real page against the
    paths it drew (window.__paved); this keeps it so for all eight plots, the
    ones not built on yet included."""

    WIDTH = {"tree": 62, "bush": 30, "rock": 24, "sign": 18, "fence": 32, "lamp": 16,
             "bed": 36, "stall": 64, "crate": 22}

    def placed(self):
        """[(kind, x, y)] in tiles, for every hand-placed thing in the file."""
        # a fence is drawn at (tx + .5, ty + .4); everything else where it is listed
        out = [(k, x + .5, y + .4) if k == "fence" else (k, x, y) for k, pts, *_ in self.scenery() for x, y in pts]
        for kind in ("lamp", "bed", "stall"):
            m = re.search(r"(\[\[[^\n]*\]\])\s*\.forEach\(\(\[tx\s*,\s*ty\]\) => place\('" + kind, self.html)
            out += [(kind, float(a), float(b)) for a, b in re.findall(r"\[\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\]", m.group(1))]
        m = re.search(r"(\[\[[^\n]*\]\])\s*\.forEach\(\(\[tx,ty\], i\) => place\(i % 2 \? 'barrel' : 'crate'", self.html)
        out += [("crate", float(a), float(b)) for a, b in re.findall(r"\[\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\]", m.group(1))]
        return out

    def paths(self):
        """{(x, y)} path tiles to keep clear, by the rule village.html lays them."""
        T, ROAD = self.T, 13
        jsround = lambda v: math.floor(v + 0.5)
        tiles = set()

        def lay(door_x, dy, x0, x1, side=None):
            dx = math.floor(door_x / T)
            if dy <= ROAD:
                tiles.update((dx, y) for y in range(dy, ROAD + 1))
                return
            sx = math.ceil(x1 / T) if side == "right" else math.floor(x0 / T) - 1
            tiles.update((sx, y) for y in range(ROAD + 1, dy + 1))
            tiles.update((x, dy) for x in range(min(sx, dx), max(sx, dx) + 1))

        plots = re.findall(r"\{ x:\s*([\d.]+),\s*y:\s*([\d.]+),\s*flip: \w+,?\s*(?:side: '(\w+)')?", self.html)
        self.assertEqual(len(plots), 8)
        for x, y, side in plots:
            x, y = float(x), float(y)
            lay((x + 2) * T, jsround(y + 3.7), x * T, x * T + 128, side or None)
        hx, hy = (float(v) for v in re.search(r"PROPS_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html).groups())
        lay((hx + 2.5) * T, jsround(hy + 3.9), hx * T, hx * T + 160, "right")
        cx, cy = (float(v) for v in re.search(r"CLIP_PLOT = \{ x: ([\d.]+), y: ([\d.]+) \}", self.html).groups())
        lay((cx + 2.25) * T, jsround(cy + 3.9), cx * T, cx * T + 144)
        tiles.update((x, y) for x in range(38) for y in (ROAD, ROAD + 1))      # the high street
        # The square is meant to be furnished; only the road across it and
        # Clip's way in are kept clear.
        clip_col = math.floor((cx + 2.25) * T / T)
        return {(x, y) for x, y in tiles
                if not (16 <= x <= 21 and 11 <= y <= 17) or y in (ROAD, ROAD + 1) or (x == clip_col and y < ROAD)}

    def blocked(self):
        T, bad, tiles = self.T, [], self.paths()
        for kind, sx, sy in self.placed():
            half = self.WIDTH[kind] / 2
            for tx, ty in tiles:
                if (sx * T + half - 3 > tx * T and sx * T - half + 3 < (tx + 1) * T
                        and sy * T > ty * T + 4 and sy * T - 10 < (ty + 1) * T):
                    bad.append(f"{kind} at ({sx:g},{sy:g}) on path tile ({tx},{ty})")
        return bad

    def test_it_reads_everything_it_should(self):
        kinds = collections.Counter(k for k, *_ in self.placed())
        for kind, least in (("tree", 15), ("bush", 10), ("rock", 5), ("fence", 3), ("lamp", 5),
                            ("bed", 4), ("stall", 2), ("crate", 4), ("sign", 5)):
            self.assertGreaterEqual(kinds[kind], least, f"only {kinds[kind]} {kind} found")
        self.assertGreater(len(self.paths()), 120)

    def test_it_can_fail(self):
        real, self.html = self.html, self.html.replace("[13,12],[27.5,12]", "[13,12],[26,12]", 1)
        try:
            self.assertIn("rock at (26,12) on path tile (25,11)", self.blocked())
        finally:
            self.html = real

    def test_nothing_stands_on_a_path(self):
        self.assertEqual(self.blocked(), [])

    def test_gates_open_onto_the_path_and_south_paths_go_round(self):
        self.assertIn("const pathCol = doorX => Math.floor(doorX / T);", self.html)
        self.assertIn("gate = (pathCol(bx + HW/2) + .5) * T;", self.html)
        self.assertIn("this.add.image(fx, gy, 'fence')", self.html)
        self.assertIn("if (row < MAP_H && col >= 0 && col < MAP_W && PAVED[row][col]) return;", self.html)
        self.assertIn("const sx = side === 'right' ? Math.ceil(x1 / T) : Math.floor(x0 / T) - 1;", self.html)
        self.assertIn("const propLeft = plot.side ? plot.side === 'right' : plot.flip;", self.html)
        self.assertIn("const bedRight = plot.side ? plot.side === 'left' : plot.flip;", self.html)
        self.assertNotIn("Math.round(pl.x + 2)", self.html, "the rounded column is the shut-gate bug")


class EachHouseLooksLikeItsTrade(unittest.TestCase):
    """2026-09-29: Belfort got an exchange, Ace an arena (not a second props
    hall), Emily a studio, Scout an observatory; Fury lost his house, and the
    square's notice board became a statue of Iron Man carrying the ledger."""

    def setUp(self):
        self.html = (ROOT / "mission-control-api" / "public" / "village.html").read_text()

    def themed(self):
        return {n: (prop, int(h)) for n, prop, h in
                re.findall(r"(\w+):\s*\{ prop: '(\w+)',\s*h: (\d+)", self.html)}

    def test_every_themed_house_and_prop_is_drawn(self):
        themed = self.themed()
        self.assertEqual(sorted(themed), ["ace", "belfort", "emily", "paul", "scout"])
        for name, (prop, h) in themed.items():
            self.assertIn(f"themed('{name}',", self.html, f"{name} has no house drawn")
            self.assertIn(f"generateTexture('{prop}',", self.html, f"{name}'s {prop} is never drawn")
            self.assertGreaterEqual(h, 116, f"{name}'s house is shorter than its own door")
        self.assertGreater(themed["belfort"][1], 2 * 116 - 10, "a Wall Street tower, not a house")

    def test_buildings_stand_on_their_bottom_edge(self):
        # Different heights, one ground line: placed by the bottom, every door,
        # path and garden stays put and a taller building rises further.
        self.assertRegex(self.html, r"this\.add\.image\(bx, by \+ HH, theme \? `house-\$\{a\.name\}`[^;]*\.setOrigin\(0, 1\)")
        self.assertIn("gg.generateTexture(`house-${name}`, 128, THEMED[name].h)", self.html)

    def test_each_resident_dresses_for_the_house(self):
        # Belfort in a suit, Ace in a jersey... and every look actually walks:
        # a look without its animations would stand frozen or, worse, play the
        # plain villager's walk and change clothes mid-stride.
        outfits = re.search(r"const OUTFITS = \{(.*?)\n\};", self.html, re.S).group(1)
        names = re.findall(r"^  (\w+): \{", outfits, re.M)
        for who in list(self.themed()) + ["clip", "spotter"]:
            self.assertIn(who, names, f"{who} has no outfit")
        self.assertIn("hero(`npc-${who}-${f}-${fr}`, f, fr, o)", self.html)
        self.assertIn("key: walkAnim(`npc-${who}`, f)", self.html)
        update = self.html.split("function update(", 1)[1]
        for f in ("side", "up", "down"):
            self.assertIn(f"n.s.anims.play(walkAnim(n.look, '{f}')", update)
        self.assertNotIn("n.s.anims.play('walk-", update, "an NPC playing the plain walk")
        self.assertIn("n.s.setTexture(`${n.look}-down-0`)", update)

    def test_every_resident_wears_a_name_tag_clear_of_the_signs(self):
        # Owner, 2026-10-07: "Label all the villagers by their name." Before,
        # only Clip and Spotter had one. A tag must also never sit on a house's
        # own name plate - "EMILY Emily" was the first draft.
        body = self.html.split("function create()", 1)[1].split("// ---- player ----", 1)[0]
        self.assertIn("label: nameTag(this, npc.x, npc.y - 22, a.name.charAt(0).toUpperCase() + a.name.slice(1))", body)
        self.assertIn("const label = nameTag(this, hx, hy - 22, who);", body)
        self.assertEqual(body.count("npcs.push("), body.count("nameTag(this,"), "a resident without a tag")
        self.assertIn("minY: doorY + TAG_CLEAR", body)
        self.assertIn("y: doorY - 48, maxY: doorY - 24", body, "a south resident stays above its sign")
        self.assertIn("minY: by + SH + TAG_CLEAR", body)
        update = self.html.split("function update(", 1)[1]
        self.assertIn("n.ty = Math.min(n.maxY ?? Infinity, Math.max(n.minY ?? -Infinity,", update)
        self.assertIn("if (n.label) n.label.setPosition(", update)

    def test_a_themed_house_is_never_mirrored(self):
        # a flipped plot mirrors a plain house; a ticker, "+150" or a painting
        # would read backwards
        self.assertIn("if (plot.flip && !theme) house.setFlipX(true)", self.html)

    def test_fury_has_no_house_and_no_path_to_one(self):
        self.assertRegex(self.html, r"const NO_HOUSE = new Set\(\['fury'\]\)")
        body = self.html.split("function create()", 1)[1]
        self.assertIn("const agents = housed();", body)
        self.assertIn("Math.min(housed().length, PLOTS.length)", self.html,
                      "garden paths come from the same list as the houses")
        self.assertNotIn("DATA.agents || []).length, PLOTS.length", self.html)

    def test_the_statue_is_the_town_hall(self):
        self.assertIn("generateTexture('ironman',", self.html)
        self.assertIn("this.add.text(statue.x, statue.y - 50, 'TOWN HALL'", self.html)
        self.assertNotIn("boardText.setText(", self.html, "the plaque's name is never overwritten")
        self.assertIn("doors.push({ name: HALL_DOOR, x: statue.x, y: statue.y + 14 })", self.html)
        self.assertRegex(self.html, r"boardText = this\.add\.text\(statue\.x,")
        for gone in ("'board'", "'well'"):
            self.assertNotIn(gone, self.html, f"{gone} is drawn or placed but no longer used")


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
        self.assertIn("directed(prompt", body)
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

    def test_a_tote_scan_does_not_walk_into_a_handbag_brand(self):
        # "tote bag coach" was the fourth phrase of the first real 'tote bag'
        # scan, and the screen let it through. Coach is also a word - a
        # sports coach - so it is flagged for the owner, like 'frozen'. The
        # fashion houses are not words and are refused outright.
        for phrase in ("tote bag coach", "gift for coach"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.m.risky(phrase)[0], "check", phrase)
        for phrase in ("marc jacobs tote bag", "the tote bag marc jacobs",
                       "kate spade tote", "baggu tote", "michael kors tote",
                       "hermes birkin tote", "longchamp le pliage"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.m.risky(phrase)[0], "blocked", phrase)
        for phrase in ("coaching tote", "tote bag for school", "canvas tote bag",
                       "prairie tote", "diorama tote"):
            with self.subTest(phrase=phrase):
                self.assertIsNone(self.m.risky(phrase)[0], phrase)

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
                    "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25,
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
                                  "price": 3.99, "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25,
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
            json.dumps({"seed": "water bottle sticker", "scanned_at": datetime.now(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "rows": [{"phrase": "water bottle stickers", "supply": 1,
                                  "heat": 0.1, "pull": 0.1, "price": 3.99,
                                  "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25,
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
                 "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25, "pull_n": 23, "score": 0.0030},
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
                "price": price, "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25,
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
                                  "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25,
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
                "price": price, "match": match, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25,
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
                      "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25,
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
                      "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25,
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
        self.assertIn("no drafts to take in", r.stdout)
        self.assertIn("proposals.json", r.stdout,
                      "both sources are named, not just one")

    def test_either_file_is_read_and_only_the_phrase_decides(self):
        # AGENTS.md mentioned drafts.txt five times. Scout read it and wrote
        # proposals.json - the third time it has been told to write one file
        # and written another. The filename was never the rule; the
        # measurement is, and it rides along inside either file.
        self.drafts.write_text(
            "canvas tote bag | From The Text File | tote | a line\n")
        (self.state / "proposals.json").write_text(json.dumps({"proposals": [
            {"title": "No Phrase Here", "product": "all-over-print canvas tote bag",
             "angle": "torn paper"},
            {"title": "Has A Phrase", "product": "all-over-print canvas tote bag",
             "phrase": "canvas tote bag", "angle": "meadow"}]}))
        r = self.run_it("intake")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 filed, 1 refused", r.stdout)
        self.assertIn("drafts.txt line 1", r.stdout)
        self.assertIn("proposals.json row 2", r.stdout)
        self.assertIn("proposals.json row 1", r.stderr)
        self.assertIn("no phrase", r.stderr)
        titles = {row["title"] for row in self.filed()}
        self.assertEqual(titles, {"From The Text File", "Has A Phrase"})

    def test_a_phrase_inside_an_evidence_block_counts(self):
        (self.state / "proposals.json").write_text(json.dumps({"proposals": [
            {"title": "From Evidence", "product": "tote",
             "evidence": {"phrase": "canvas tote bag"}}]}))
        r = self.run_it("intake")
        self.assertIn("1 filed, 0 refused", r.stdout)

    def test_the_filed_rows_are_not_wiped_by_the_clear(self):
        # proposals.json is BOTH a source here and where propose appends.
        # Emptying it at the end of intake wiped the rows just filed into
        # it, and merge then found nothing. Found by running the chain.
        (self.state / "proposals.json").write_text(json.dumps({"proposals": [
            {"title": "Survives", "product": "tote",
             "phrase": "canvas tote bag"}]}))
        self.run_it("intake")
        self.assertEqual([r["title"] for r in self.filed()], ["Survives"])
        m = self.run_it("merge")
        self.assertEqual(m.returncode, 0, m.stderr)
        log = json.loads((self.state / "ideas.json").read_text())["ideas"]
        self.assertEqual([i["title"] for i in log], ["Survives"])

    def test_the_placeholder_phrase_is_refused(self):
        # The recovery lines print '<phrase> | ...'. Pasted back unedited,
        # that must not file an idea against a phrase called '<phrase>'.
        self.drafts.write_text("<phrase> | Pasted Unedited | tote | oops\n")
        r = self.run_it("intake")
        self.assertIn("0 filed, 1 refused", r.stdout)
        self.assertIn("no phrase", r.stderr)

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
        self.assertIn("Do not run any script", head)
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
                 "pull": 0.02, "price": 22.62, "match": 1.0, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25,
                 "heat_n": 25, "pull_n": 23, "score": 0.0024},
                {"phrase": "canvas tote bag embroidered", "supply": 18149,
                 "heat": 0.062, "pull": 0.035, "price": 23.84, "match": 1.0,
                 "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25, "pull_n": 23, "score": 0.0145}],
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


class AnAllOverPrintHasNoBackground(unittest.TestCase):
    """The first AOP tote was refused, and the refusal was the bug.

    Emily built "Minimal Wave Line All-over Tote" against blueprint 1389,
    "Tote Bag (AOP)". The art was correct - a wave pattern edge to edge,
    which is the entire point of the product. draft refused it:

        knockout: only 36% of the border is one colour - there is no flat
        background here. That is what a photograph looks like.

    True, and the reason the art was right. The whole of knockout's verdict -
    border agreement, percent removed, percent left - asks about a background
    the file is not supposed to have. That check was written for apparel,
    where a design is cut out and placed ON a garment. On an all-over print
    the file IS the surface.

    Two things had to change and a third had to NOT change:

      the cutout is skipped - there is nothing to cut out;
      the check asks a different question - palette, not background;
      the check still runs, because the failure it guards is unchanged. A
      model asked for a print file will hand back a photograph of the
      product, and did, with a sticker on a desk.

    And upstream of all of it, the art direction: PRINT_DIRECTION asks for
    "a solid plain background in one even colour, filling the frame", which
    printed edge to edge is a blob in the middle of a field.
    """

    def setUp(self):
        self.ko = load("knockout4", "knockout.py")
        self.ep = load("emily_printify_ao", "emily-printify.py")
        self.ea = load("emily_assets_ao", "emily-assets.py")
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

    def wave(self, w=200, h=200):
        """An all-over pattern: two colours, edge to edge, no background."""
        return self.image(w, h, lambda x, y:
                          (245, 238, 228) if math.sin(x / 20.0 + y / 30.0) > 0
                          else (38, 58, 78))

    def photo(self, w=240, h=180):
        """The same wood-grain desk the sticker failure produced."""
        rnd = random.Random(7)
        rows = [(150 + rnd.randrange(60), 100 + rnd.randrange(50),
                 50 + rnd.randrange(40)) for _ in range(h)]
        return self.image(w, h, lambda x, y: tuple(
            max(0, min(255, c + ((x * 7 + y * 13) % 17) - 8)) for c in rows[y]))

    def grainy(self, w=220, h=220):
        """The same pattern as it actually comes back: soft edges and grain.

        self.wave() is two exact colours, which no image model has ever
        returned. This is the honest version - anti-aliased boundaries and a
        few levels of noise over the whole frame.
        """
        rnd = random.Random(11)
        a, b = (245, 238, 228), (38, 58, 78)

        def px(x, y):
            t = max(0.0, min(1.0, (math.sin(x / 22.0 + y / 31.0) + 0.03) / 0.06))
            n = rnd.randrange(-3, 4)
            return tuple(max(0, min(255, int(a[i] * t + b[i] * (1 - t)) + n))
                         for i in range(3))
        return self.image(w, h, px)

    # --- which products ----------------------------------------------------

    def test_printify_says_which_blueprints_are_all_over(self):
        # Real blueprint titles, copied from the catalogue rather than
        # imagined. "Aoplite Mug" is here because a substring match on "aop"
        # would call it one.
        for title, want in [("Tote Bag (AOP)", True),
                            ("Shoulder Tote Bag (AOP)", True),
                            ("All-Over Print Tote", True),
                            ("All Over Print Tee", True),
                            ("Cotton Tote Bag", False),
                            ("Kiss-Cut Stickers", False),
                            ("Aoplite Mug", False),
                            ("Unisex Heavy Blend Hooded Sweatshirt", False)]:
            self.assertIs(self.ep.is_all_over({"blueprint_title": title}), want,
                          title)

    def test_an_entry_with_no_title_is_not_assumed_all_over(self):
        # The safe way round. A missed AOP costs one regeneration; a wrongly
        # assumed one prints a white rectangle onto a garment.
        self.assertFalse(self.ep.is_all_over({}))
        self.assertFalse(self.ep.is_all_over(None))

    def test_an_all_over_print_is_not_cut_out(self):
        self.assertFalse(self.ep.needs_cutout({"blueprint_title": "Tote Bag (AOP)"}))
        self.assertTrue(self.ep.needs_cutout({"blueprint_title": "Cotton Tote Bag"}))

    def test_an_explicit_cutout_flag_still_wins(self):
        # `pick --no-cutout` records a decision a person made. Nothing
        # inferred from a title may overrule it.
        self.assertTrue(self.ep.needs_cutout(
            {"blueprint_title": "Tote Bag (AOP)", "cutout": True}))

    # --- what is measured instead ------------------------------------------

    def test_a_photograph_is_still_refused_on_an_all_over_product(self):
        # The point of the whole exercise. Skipping the check would have been
        # the easy fix and would have put the desk back on the tote.
        w, h, px = self.photo()
        ok, facts, problem = self.ko.all_over_verdict(w, h, px)
        self.assertFalse(ok, facts)
        self.assertIn("photograph", problem)

    def test_an_edge_to_edge_pattern_passes(self):
        w, h, px = self.wave()
        ok, facts, problem = self.ko.all_over_verdict(w, h, px)
        self.assertTrue(ok, f"{facts}\n{problem}")

    def test_the_old_verdict_would_have_refused_that_same_pattern(self):
        # The premise, measured rather than remembered: this is why a second
        # verdict exists at all. If the background check ever starts passing
        # edge-to-edge art, all_over_verdict is redundant and should go.
        w, h, px = self.wave()
        ok, facts, _problem = self.ko.verdict(w, h, px)
        self.assertFalse(ok, facts)

    def test_flat_art_committed_in_this_repo_passes(self):
        # A real file, not a generator. hall.png is drawn art with gradients
        # and dithering - the nearest thing here to what the image model
        # returns, and the sample the threshold was set against.
        w, h, px = self.ko.decode(ROOT / "hall.png")
        cover, _distinct = self.ko.palette_coverage(w, h, px)
        self.assertGreater(cover, self.ko.PALETTE_MIN_PCT,
                           f"real art measured {cover:.0f}%")

    def test_coverage_separates_them_and_a_colour_count_does_not(self):
        # Why the measurement is coverage. Counted distinct, the desk has 92
        # quantised colours and hall.png 66 - the wrong way round, and a
        # threshold on that number would refuse the art and pass the photo.
        pw, ph, ppx = self.photo()
        aw, ah, apx = self.ko.decode(ROOT / "hall.png")
        photo_cover, photo_n = self.ko.palette_coverage(pw, ph, ppx)
        art_cover, art_n = self.ko.palette_coverage(aw, ah, apx)
        self.assertGreater(art_cover, photo_cover + 20.0)
        self.assertGreaterEqual(photo_n, art_n,
                                "if this ever flips, a count would work and "
                                "this comment is wrong")

    def test_grain_and_soft_edges_do_not_cost_the_measurement(self):
        """Why the colours are quantised before they are counted.

        What the image model returns is not two flat colours. Every boundary
        is anti-aliased and the whole frame carries a little grain, so counted
        EXACTLY this pattern has 483 colours and its commonest eight cover
        57% - a hair above the threshold, and falling as the art gets softer.
        Quantised to 16 levels a channel it is 35 colours and 99%, which is
        what it actually looks like.
        """
        w, h, px = self.grainy()
        cover, _n = self.ko.palette_coverage(w, h, px)
        self.assertGreater(cover, 90.0,
                           f"soft-edged flat art measured {cover:.0f}%")

    def test_an_empty_image_does_not_divide_by_zero(self):
        self.assertEqual(self.ko.palette_coverage(0, 0, bytearray()), (0.0, 0))

    # --- the command line ---------------------------------------------------

    def test_check_all_over_passes_a_pattern_and_writes_nothing(self):
        src = self.d / "wave.png"
        w, h, px = self.wave()
        self.ko.encode(src, w, h, px)
        before = sorted(p.name for p in self.d.iterdir())
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(src), "--check", "--all-over"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), before)

    def test_check_all_over_refuses_a_photograph(self):
        src = self.d / "photo.png"
        w, h, px = self.photo()
        self.ko.encode(src, w, h, px)
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(src), "--check", "--all-over"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("photograph", r.stderr)

    def test_all_over_without_check_is_a_usage_error_not_a_cutout(self):
        # Two verdicts and a write is how a file gets knocked out under one
        # rule and judged under the other.
        src, dst = self.d / "wave.png", self.d / "out.png"
        w, h, px = self.wave()
        self.ko.encode(src, w, h, px)
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"),
                            str(src), str(dst), "--all-over"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse(dst.exists())

    # --- where it is wired in ------------------------------------------------

    def test_draft_passes_all_over_to_the_check(self):
        # The hand-off. A function that computes the right thing and a
        # function that is CALLED with it are different facts, and this repo
        # has paid for that difference four times.
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("is_all_over(cat)", body)
        window = body[body.index('"--check"'):body.index("if needs_cutout(cat):")]
        self.assertIn("--all-over", window,
                      "the flag must be added to the check, not somewhere later")

    def test_the_check_still_runs_for_every_product(self):
        # Unchanged and asserted again, because the tempting fix was to skip
        # the check for AOP entirely.
        src = (SCRIPTS / "emily-printify.py").read_text()
        body = src.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(body.index('"--check"'), body.index("if needs_cutout(cat):"))

    # --- the art direction ---------------------------------------------------

    def test_an_all_over_product_is_asked_for_edge_to_edge_art(self):
        out = self.ea.ALL_OVER_DIRECTION.lower()
        self.assertIn("edge to edge", out)
        self.assertNotIn("plain background", out)
        for forbidden in ("photograph", "mockup", "desk", "tote"):
            self.assertIn(forbidden, out,
                          f"the direction must rule out a {forbidden}")

    def test_the_two_directions_disagree_about_the_background(self):
        # If they ever stop disagreeing, one of them is doing nothing.
        self.assertIn("plain background", self.ea.PRINT_DIRECTION.lower())
        self.assertNotIn("edge to edge", self.ea.PRINT_DIRECTION.lower())

    def test_the_prompt_asks_printify_which_direction_to_use(self):
        # One fact, one place: the same is_all_over() the cutout uses. A
        # second opinion here would drift silently - the art would just
        # quietly get worse and nobody would know why.
        body = (SCRIPTS / "emily-assets.py").read_text()
        body = body.split("def all_over(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("is_all_over", body)
        self.assertIn("emily-printify.py", body)

    def test_the_product_reaches_the_direction(self):
        root = self.d / "root"
        (root / "agents" / "emily" / "state").mkdir(parents=True)
        (root / "agents" / "emily" / "state" / "printify-catalog.json").write_text(
            json.dumps({"tote": {"blueprint_title": "Tote Bag (AOP)",
                                 "variant_ids": [1]},
                        "sticker": {"blueprint_title": "Kiss-Cut Stickers",
                                    "variant_ids": [1]}}))
        old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(root)
        try:
            self.assertIn("edge to edge", self.ea.directed("waves", "tote"))
            self.assertIn("plain background", self.ea.directed("a leaf", "sticker"))
            self.assertIn("plain background", self.ea.directed("a leaf", ""))
        finally:
            if old is None:
                os.environ.pop("ECOSYSTEM_ROOT", None)
            else:
                os.environ["ECOSYSTEM_ROOT"] = old

    def test_a_missing_catalogue_does_not_stop_the_art(self):
        old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(self.d / "nothing-here")
        try:
            self.assertFalse(self.ea.all_over("tote"))
        finally:
            if old is None:
                os.environ.pop("ECOSYSTEM_ROOT", None)
            else:
                os.environ["ECOSYSTEM_ROOT"] = old

    def test_the_product_reaches_directed_inside_generate(self):
        # Parsed, passed to generate(), and then dropped on the floor one line
        # later is still dropped. Asserted on the request body itself.
        body = (SCRIPTS / "emily-assets.py").read_text()
        body = body.split("def generate(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("directed(prompt, product)", body)

    def test_new_build_hands_the_product_to_the_asset_script(self):
        # The other hand-off. compose() has taken `product` for a while; the
        # direction is chosen in a different process, so it needs the word on
        # the command line too.
        body = (SCRIPTS / "emily-new-build.py").read_text()
        body = body.split("def generate_artwork(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("--product", body)
        src = (SCRIPTS / "emily-assets.py").read_text()
        self.assertIn('ap.add_argument("--product"', src)
        gen = src.split("def main(", 1)[1]
        self.assertIn("generate(a.out, prompt, key, model, a.product)", gen,
                      "parsed and never passed is not passed")


class TheArtCoversTheFacesOfAFoldedSheet(unittest.TestCase):
    """A wildflower field printed on the bottom of the bag.

    The all-over tote is one tall sheet: the front face on top, the back
    face below it printed upside down, and the strip between them folded
    under as the bottom of the bag. draft placed every file the same way -
    one image, centred, scale 1 - so a 1408x768 field landed as a band
    across the middle, on the one part nobody sees, with both faces blank.

    Nothing told the model what shape to draw, and nothing told Printify
    where to put what came back.
    """

    # Printify's documented shape for a variant's print area. The numbers
    # are illustrative - the real ones are recorded by `layout` on the
    # droplet and are not asserted anywhere here.
    PAYLOAD = {"variants": [
        {"id": 101, "placeholders": [{"position": "front", "decoration_method": "dtf",
                                      "height": 8400, "width": 4050}]},
        {"id": 102, "placeholders": [{"position": "front", "height": 10200, "width": 4950}]},
        {"id": 103, "placeholders": [{"position": "front", "height": 10200, "width": 4950}]},
        {"id": 104, "placeholders": [{"position": "back", "height": 10, "width": 10}]},
    ]}

    def setUp(self):
        self.ep = load("emily_printify_fold", "emily-printify.py")
        self.entry = {"variant_ids": [101, 102, 103], "folded": True,
                      "print_sizes": {"101": [4050, 8400], "102": [4950, 10200],
                                      "103": [4950, 10200]}}

    def imgs(self, image, entry=None, vids=(101,)):
        return self.ep.print_areas("IMG", image, list(vids),
                                   entry or self.entry)[0]["placeholders"][0]["images"]

    # --- reading Printify -------------------------------------------------

    def test_front_sizes_reads_each_variants_front_area(self):
        self.assertEqual(self.ep.front_sizes(self.PAYLOAD),
                         {101: (4050, 8400), 102: (4950, 10200), 103: (4950, 10200)})

    def test_a_variant_with_no_front_area_is_left_out_not_invented(self):
        self.assertNotIn(104, self.ep.front_sizes(self.PAYLOAD))
        self.assertEqual(self.ep.front_sizes({}), {})

    # --- the placement ----------------------------------------------------

    def test_a_folded_sheet_gets_one_image_per_face(self):
        front, back = self.imgs((1024, 1024))
        self.assertEqual((front["y"], front["angle"]), (0.25, 0))
        self.assertEqual((back["y"], back["angle"]), (0.75, 180),
                         "the back face is printed upside down")

    def test_wide_art_is_scaled_until_it_covers_the_face_height(self):
        # The bug: scale 1 on a 1408x768 file left most of the face blank.
        (img, _), area = self.imgs((1408, 768)), (4050, 8400)
        tall = img["scale"] * area[0] * 768 / 1408
        self.assertGreaterEqual(tall, area[1] / 2 - 1,
                                "the image must be at least as tall as one face")
        self.assertAlmostEqual(img["scale"], 1.9012, places=3)

    def test_tall_art_is_scaled_until_it_covers_the_width(self):
        img = self.imgs((500, 2000))[0]
        self.assertAlmostEqual(img["scale"], 1.0, places=3)

    def test_it_covers_and_does_not_overshoot(self):
        # Cover, not "make it huge": the smaller side must fit exactly, or the
        # crop throws away art for nothing.
        img = self.imgs((1408, 768))[0]
        w_frac = img["scale"]
        h = img["scale"] * 4050 * 768 / 1408
        self.assertTrue(abs(w_frac - 1) < 1e-3 or abs(h - 4200) < 5,
                        "one side should fit the face exactly")

    def test_a_flat_sheet_is_one_centred_image_over_all_of_it(self):
        entry = dict(self.entry, folded=False)
        imgs = self.imgs((1024, 1024), entry)
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0]["y"], 0.5)
        self.assertAlmostEqual(imgs[0]["scale"], 8400 / 4050, places=3)

    def test_each_print_size_gets_its_own_numbers(self):
        # A 13" and an 18" tote are different sheets.
        areas = self.ep.print_areas("IMG", (1408, 768), [101, 102, 103], self.entry)
        self.assertEqual(sorted(sorted(a["variant_ids"]) for a in areas),
                         [[101], [102, 103]])
        self.assertEqual(sum(len(a["variant_ids"]) for a in areas), 3)

    def test_a_variant_with_no_recorded_size_is_refused_not_guessed(self):
        with self.assertRaises(KeyError):
            self.ep.print_areas("IMG", (1024, 1024), [101, 555], self.entry)

    def test_everything_else_is_placed_exactly_as_before(self):
        # Stickers and hoodies drafted correctly with this; it must not move.
        areas = self.ep.print_areas("IMG", (1408, 768), [7, 8], {"variant_ids": [7, 8]})
        self.assertEqual(areas, [{"variant_ids": [7, 8], "placeholders": [{
            "position": "front",
            "images": [{"id": "IMG", "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}]}]}])

    # --- the shape asked for ---------------------------------------------

    def test_the_model_is_asked_for_the_faces_shape(self):
        self.assertEqual(self.ep.aspect_for(self.entry), "1:1")
        self.assertEqual(self.ep.aspect_for(dict(self.entry, folded=False)), "9:16")
        self.assertEqual(self.ep.aspect_for({"print_sizes": {"1": [3000, 2000]}}), "3:2")

    def test_nothing_recorded_means_nothing_asked(self):
        self.assertIsNone(self.ep.aspect_for({}))
        self.assertIsNone(self.ep.aspect_for(None))

    def test_the_shape_reaches_the_request(self):
        src = (SCRIPTS / "emily-assets.py").read_text()
        gen = src.split("def generate(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("shape(product)", gen)
        self.assertIn('"image_config"', gen)
        self.assertIn('"aspect_ratio"', gen)
        shp = src.split("def shape(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("aspect_for", shp, "one place decides the shape")

    # --- wiring -----------------------------------------------------------

    def test_draft_uses_the_layout(self):
        body = (SCRIPTS / "emily-printify.py").read_text()
        body = body.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("print_areas(image_id", body)
        self.assertIn('"print_areas": areas', body)
        self.assertNotIn('"scale": 1, "angle": 0', body,
                         "the old fixed placement must not survive in draft")

    def test_an_unmeasured_all_over_product_is_refused_before_upload(self):
        body = (SCRIPTS / "emily-printify.py").read_text()
        body = body.split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        guard = body.index('if is_all_over(cat) and not cat.get("print_sizes"):')
        self.assertLess(guard, body.index("/uploads/images.json"))
        self.assertIn("layout --product", body[guard:guard + 600])

    def test_layout_records_sizes_and_the_fold(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cat = {"tote": {"blueprint_id": 1389, "provider_id": 10,
                        "blueprint_title": "Tote Bag (AOP)",
                        "variant_ids": [101, 102], "variant_titles": ["13", "16"]}}
        path = Path(tmp.name) / "cat.json"
        path.write_text(json.dumps(cat))
        self.ep.CATALOG = path
        self.ep.call = lambda *_a, **_k: self.PAYLOAD
        a = types.SimpleNamespace(product="tote", folded=True, flat=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.ep.cmd_layout(a), 0)
        got = json.loads(path.read_text())["tote"]
        self.assertEqual(got["print_sizes"], {"101": [4050, 8400], "102": [4950, 10200]})
        self.assertTrue(got["folded"])
        a = types.SimpleNamespace(product="tote", folded=False, flat=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.ep.cmd_layout(a)
        self.assertNotIn("folded", json.loads(path.read_text())["tote"])

    def test_png_size_reads_the_header(self):
        ko = load("knockout_sz_t", "knockout.py")
        self.assertEqual(ko.size(ROOT / "hall.png"), (460, 330))


class AReportInTheWrongFolderIsNamed(unittest.TestCase):
    """Belfort left 2026-09-17-close.md and two siblings in its top folder.

    report_name in data/_meta.json is a bare filename, and both agents'
    instructions said "use that string" without saying where. A model given a
    filename writes it into the directory it is standing in. The verifier
    looked only in reports/, so it said "no report" - true, and no help - and
    git showed the real one as untracked litter for a week.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = self.root / "agents" / "belfort"
        for sub in ("reports", "state", "data"):
            (self.agent / sub).mkdir(parents=True)
        shutil.copy(ROOT / "agents" / "belfort" / "state" / "portfolio.seed.json",
                    self.agent / "state" / "portfolio.json")
        self.name, _ = et_time.expected_report(self.agent, "belfort")

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "belfort-verify.py"),
                               "0", "-1", "0"], capture_output=True, text=True, env=env)

    def test_strays_finds_the_top_folder(self):
        (self.agent / "2026-09-17-close.md").write_text("x")
        self.assertEqual(et_time.strays(self.agent, "2026-09-17-open.md"),
                         ["2026-09-17-close.md"])

    def test_strays_still_finds_the_wrong_slot(self):
        (self.agent / "reports" / "2026-09-17-close.md").write_text("x")
        self.assertEqual(et_time.strays(self.agent, "2026-09-17-open.md"),
                         ["reports/2026-09-17-close.md"])

    def test_strays_ignores_other_days(self):
        (self.agent / "2026-09-16-close.md").write_text("x")
        (self.agent / "reports" / "2026-09-16-open.md").write_text("x")
        self.assertEqual(et_time.strays(self.agent, "2026-09-17-open.md"), [])

    def test_the_verifier_names_the_misfiled_report(self):
        (self.agent / self.name).write_text("word " * 80)
        r = self.verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"written to belfort/{self.name}", r.stdout)
        self.assertIn("reports go in reports/", r.stdout)

    def test_the_right_folder_still_passes_the_report_check(self):
        (self.agent / "reports" / self.name).write_text("word " * 80)
        r = self.verify()
        self.assertNotIn(f"no reports/{self.name}", r.stdout)

    def test_both_verifiers_ask_et_time(self):
        # One search, two callers. The copy each verifier had is gone.
        for f in ("ace-verify.py", "belfort-verify.py"):
            src = (SCRIPTS / f).read_text()
            self.assertIn("et_time.strays(AGENT, report.name)", src, f)
            self.assertNotIn('(AGENT / "reports").glob(', src, f)

    def test_both_agents_are_told_the_folder(self):
        for a in ("ace", "belfort"):
            src = (ROOT / "agents" / a / f"_{a}-agents-header.md").read_text()
            self.assertIn("reports/<report_name>", src, a)


class AnInjuredPlayerIsFlaggedNotAdjusted(unittest.TestCase):
    """Josh Jacobs was forecast for a game he was ruled out of.

    ESPN's roster puts a player in "offense" whatever his status, and the
    forecaster took that group as "might take the field". On the real Packers
    roster the week of Falcons, Jacobs and Jayden Reed were in it and both
    carried {"status": "Out"}. Jacobs's rows were written, and `best` ranked
    him third, with nothing saying he would not play.

    The number itself is left alone - it is P(clears the bar | he plays), and
    calibration grades exactly that. The fix is that the reader is told.

    The fixture is the real roster, trimmed to eight athletes.
    """

    def setUp(self):
        self.m = load("props_forecast_inj", "props-forecast.py")
        self.roster = json.loads((FIXTURES / "espn-nfl-roster-gb.json").read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.m.STORE = Path(self.tmp.name) / "forecasts.json"

    def tearDown(self):
        self.tmp.cleanup()

    def athlete(self, name):
        for g in self.roster["athletes"]:
            for a in g["items"]:
                if a["displayName"] == name:
                    return a
        raise KeyError(name)

    # --- reading ESPN -----------------------------------------------------

    def offense(self):
        return [a for g in self.roster["athletes"] if g["position"] == "offense"
                for a in g["items"]]

    def test_every_designation_in_the_real_roster_is_read_back(self):
        # Round trip, not a restated value: whatever ESPN's newest entry says
        # is what comes back. The fixture can be refreshed without this test
        # knowing who is hurt this week.
        seen = 0
        for g in self.roster["athletes"]:
            for a in g["items"]:
                raw = a.get("injuries") or []
                got = self.m.injury_of(a)
                if not raw:
                    self.assertIsNone(got, a["displayName"])
                    continue
                newest = max(raw, key=lambda i: i.get("date") or "")
                self.assertEqual(got, {"status": newest["status"],
                                       "date": newest["date"][:10]}, a["displayName"])
                seen += 1
        self.assertGreater(seen, 0, "the fixture must hold at least one injury")

    def test_the_fixture_still_shows_the_bug(self):
        # The premise, measured: someone in the ACTIVE offense group is
        # designated. If a refresh loses that, this fixture stops testing
        # the thing it was captured for.
        self.assertTrue(any(a.get("injuries") for a in self.offense()))
        self.assertTrue(any(not a.get("injuries") for a in self.offense()))

    def test_the_latest_designation_wins(self):
        # The list is a history. Questionable on Monday, Out on Wednesday.
        a = {"injuries": [{"status": "Out", "date": "2026-09-23T20:13Z"},
                          {"status": "Questionable", "date": "2026-09-20T01:00Z"}]}
        self.assertEqual(self.m.injury_of(a)["status"], "Out")
        a["injuries"].reverse()
        self.assertEqual(self.m.injury_of(a)["status"], "Out", "order must not matter")

    def test_nothing_usable_is_no_designation(self):
        for a in ({}, None, {"injuries": []}, {"injuries": [{"status": " "}]},
                  {"injuries": ["Out"]}):
            self.assertIsNone(self.m.injury_of(a), a)

    def test_the_players_the_roster_reader_returns_carry_it(self):
        summary = {"header": {"competitions": [{"competitors": [
            {"team": {"id": "9", "abbreviation": "GB"}}]}]}}
        self.m.get = lambda url: self.roster if "/roster" in url else summary
        got = {p[1]: p[5] for p in self.m.players_in("401872948")}
        wanted = [a for a in self.offense()
                  if (a.get("position") or {}).get("abbreviation") in self.m.POSITIONS]
        self.assertTrue(wanted)
        for a in wanted:
            self.assertEqual(got[a["displayName"]], self.m.injury_of(a), a["displayName"])
        self.assertTrue(any(got[a["displayName"]] for a in wanted),
                        "an injured active player must come back flagged")
        # The groups that were already excluded stay excluded.
        for g in self.roster["athletes"]:
            if g["position"] != "offense":
                for a in g["items"]:
                    self.assertNotIn(a["displayName"], got)

    # --- what it does to a forecast ---------------------------------------

    def row_args(self):
        sample = [55.0, 62.0, 71.0, 48.0, 90.0, 33.0, 66.0, 77.0, 41.0, 58.0]
        return ("1", "A @ B", None, "2", "A Receiver", "WR", "AAA",
                {"receiving_yards": sample}, ["2026"], None)

    def test_the_percentage_is_not_touched(self):
        well = self.m.rows_for_player(*self.row_args())
        hurt = self.m.rows_for_player(*self.row_args(),
                                      {"status": "Out", "date": "2026-09-23"})
        self.assertEqual(well[0]["probabilities"], hurt[0]["probabilities"])
        self.assertEqual(well[0]["interval"], hurt[0]["interval"])

    def test_the_designation_is_saved_on_the_row(self):
        # So a later calibration can split graded rows by it, and so `best`,
        # which reads the file rather than ESPN, can show it.
        hurt = self.m.rows_for_player(*self.row_args(),
                                      {"status": "Out", "date": "2026-09-23"})
        self.assertEqual(hurt[0]["injury"]["status"], "Out")
        self.assertIsNone(self.m.rows_for_player(*self.row_args())[0]["injury"])

    def test_the_warning_says_what_and_when(self):
        note = self.m.injury_note({"status": "Questionable", "date": "2026-09-22"})
        self.assertIn("⚠", note)
        self.assertIn("QUESTIONABLE", note)
        self.assertIn("09-22", note)
        self.assertEqual(self.m.injury_note(None), "")

    def test_best_shows_the_flag_beside_the_pick(self):
        row = self.m.rows_for_player(*self.row_args(),
                                     {"status": "Out", "date": "2026-09-23"})[0]
        self.m.save({"forecasts": [row]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.cmd_best("1")
        line = [l for l in out.getvalue().splitlines() if "A Receiver" in l][0]
        self.assertIn("⚠ OUT", line)
        self.assertIn("row is not a pick", out.getvalue())

    def test_best_says_nothing_when_nobody_is_hurt(self):
        row = self.m.rows_for_player(*self.row_args())[0]
        self.m.save({"forecasts": [row]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.cmd_best("1")
        self.assertNotIn("⚠", out.getvalue())

    # --- wiring -----------------------------------------------------------

    def test_forecast_hands_the_designation_to_the_row(self):
        body = (SCRIPTS / "props-forecast.py").read_text()
        body = body.split("def cmd_forecast(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("samples, seasons, avail, injury, ", body)
        self.assertIn("injury_note(injury)", body)
        self.assertIn("carry an injury designation", body)


class ARuledOutPlayerGivesUpHisSlot(unittest.TestCase):
    """Flagging Josh Jacobs was not enough: he still took a Packers slot.

    The six skill slots per side are ranked by volume, and Jacobs - Out for
    Falcons week - had the most, so he held one, was forecast, and `best`
    ranked him third while the player who would get his touches was not in
    the file. A player ruled out is now set aside before the ranking, the
    next one by volume moves up, and rows an earlier run wrote for him are
    removed. Questionable and Doubtful keep their slot and their warning.
    """

    OUT = {"status": "Out", "date": "2026-09-13"}
    MAYBE = {"status": "Questionable", "date": "2026-09-22"}

    def setUp(self):
        self.m = load("props_forecast_bench", "props-forecast.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.m.STORE = Path(self.tmp.name) / "forecasts.json"
        self.ranked = []
        sample = [55.0, 62.0, 71.0, 48.0, 90.0, 33.0, 66.0, 77.0, 41.0, 58.0]
        self.m.get = lambda url: {"header": {"competitions": [{"competitors": []}]}}
        self.m.team_games = lambda tid: 3
        self.m.samples_for = lambda aid, now=None: (
            {"receiving_yards": sample}, ["2026"], 3)

        def rank(cands, now=None):
            self.ranked = [c[1] for c in cands]
            return cands
        self.m.top_by_usage = rank

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, roster):
        self.m.players_in = lambda eid: roster
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.cmd_forecast("7")
        return out.getvalue()

    def cand(self, aid, name, injury=None):
        return (str(aid), name, "WR", "GB", "9", injury)

    def written(self):
        return {f["player"] for f in self.m.load()["forecasts"]}

    def test_only_a_real_ruling_counts(self):
        self.assertTrue(self.m.ruled_out(self.OUT))
        self.assertTrue(self.m.ruled_out({"status": "Injured Reserve"}))
        for keep in (self.MAYBE, {"status": "Doubtful"}, {"status": "Day-To-Day"},
                     {"status": "Something New"}, None, {}):
            self.assertFalse(self.m.ruled_out(keep), keep)

    def test_he_is_removed_before_the_ranking_not_after(self):
        # After would leave his slot empty instead of passing it on.
        self.run_with([self.cand(1, "Josh Jacobs", self.OUT),
                       self.cand(2, "Next Man")])
        self.assertEqual(self.ranked, ["Next Man"])

    def test_he_is_named_not_silently_dropped(self):
        out = self.run_with([self.cand(1, "Josh Jacobs", self.OUT),
                             self.cand(2, "Next Man")])
        line = [l for l in out.splitlines() if "Josh Jacobs" in l][0]
        self.assertIn("SKIPPED - OUT", line)
        self.assertIn("slot goes to the next player", line)
        self.assertIn("1 ruled out", out)

    def test_an_earlier_runs_rows_for_him_are_removed(self):
        self.run_with([self.cand(1, "Josh Jacobs"), self.cand(2, "Next Man")])
        self.assertIn("Josh Jacobs", self.written())
        self.run_with([self.cand(1, "Josh Jacobs", self.OUT), self.cand(2, "Next Man")])
        self.assertNotIn("Josh Jacobs", self.written())
        self.assertIn("Next Man", self.written())

    def test_only_this_games_rows_are_removed(self):
        self.run_with([self.cand(1, "Josh Jacobs")])
        data = self.m.load()
        for f in data["forecasts"]:
            f["event_id"] = "6"                       # last week's game
        self.m.save(data)
        self.run_with([self.cand(1, "Josh Jacobs", self.OUT)])
        self.assertEqual({f["event_id"] for f in self.m.load()["forecasts"]
                          if f["player"] == "Josh Jacobs"}, {"6"},
                         "last week's rows are graded history and must stay")

    def test_a_questionable_player_keeps_his_slot_and_his_warning(self):
        out = self.run_with([self.cand(1, "Maybe Plays", self.MAYBE)])
        self.assertEqual(self.ranked, ["Maybe Plays"])
        self.assertIn("Maybe Plays", self.written())
        self.assertIn("⚠ QUESTIONABLE", out)


class ThisWeeksGamesAreOneClickAway(unittest.TestCase):
    """The Props Hall lists the week's NFL games, and a click forecasts one.

    Finding a game used to mean pasting a one-line Python command that read
    Ace's cached slate - a slate that only holds what Ace's fetcher saw last,
    and that needs a team pair typed into it. `props-forecast.py week` asks
    ESPN for the current week directly, so ESPN access stays in one script,
    and the Deck runs `forecast` for whichever game is clicked.

    The scoreboard fixture is ESPN's real week-3 payload, trimmed to four
    games, one of them the neutral-site BAL VS DAL.
    """

    def setUp(self):
        self.m = load("props_forecast_week", "props-forecast.py")
        self.board = json.loads((FIXTURES / "espn-nfl-scoreboard-week.json").read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.m.STORE = Path(self.tmp.name) / "forecasts.json"

    def tearDown(self):
        self.tmp.cleanup()

    # --- the clock --------------------------------------------------------

    def test_a_thursday_night_kickoff_is_thursday(self):
        # 00:15 UTC on the 25th is 20:15 on the 24th in Green Bay. A page
        # doing its own arithmetic files this game under Friday.
        et = et_time.to_eastern("2026-09-25T00:15Z")
        self.assertEqual((et.day, et.hour, et.minute), (24, 20, 15))

    def test_unreadable_times_are_none_not_a_crash(self):
        for bad in ("", "soon", None, 42):
            self.assertIsNone(et_time.to_eastern(bad))

    # --- the week ---------------------------------------------------------

    def test_every_game_comes_back_with_its_id(self):
        games = self.m.week_games(self.board)
        self.assertEqual(sorted(g["event_id"] for g in games),
                         sorted(e["id"] for e in self.board["events"]))

    def test_the_eastern_time_matches_espns_own_label(self):
        # Checked against a DIFFERENT field of the same payload: ESPN writes
        # its own Eastern label, "9/24 - 8:15 PM EDT", beside the UTC date.
        # Two independent routes to one answer, and no time restated here.
        detail = {e["id"]: e["competitions"][0]["status"]["type"]["shortDetail"]
                  for e in self.board["events"]}
        for g in self.m.week_games(self.board):
            date_part = g["day_et"].split(" ", 1)[1]
            self.assertTrue(detail[g["event_id"]].startswith(f"{date_part} - {g['time_et']}"),
                            f"{g['fixture']}: {g['day_et']} {g['time_et']} vs {detail[g['event_id']]}")

    def test_the_fixture_is_written_as_espn_wrote_it(self):
        # A neutral-site game is "BAL VS DAL". Rewriting it as "@" would name
        # a home side the game does not have.
        short = {e["id"]: e["shortName"] for e in self.board["events"]}
        for g in self.m.week_games(self.board):
            self.assertEqual(g["fixture"], short[g["event_id"]])
        self.assertTrue(any(" VS " in g["fixture"] for g in self.m.week_games(self.board)),
                        "the fixture must still hold the neutral-site game")

    def test_games_are_in_kickoff_order(self):
        # ESPN happens to send them in order; reversed here, because a sort
        # nobody can see working is a sort nobody would notice was deleted.
        board = dict(self.board, events=list(reversed(self.board["events"])))
        kick = [g["kickoff_utc"] for g in self.m.week_games(board)]
        self.assertEqual(kick, sorted(kick))
        self.assertNotEqual([e["date"] for e in board["events"]], kick)

    def test_team_names_ride_along_for_the_search_box(self):
        names = {c["team"]["displayName"] for e in self.board["events"]
                 for c in e["competitions"][0]["competitors"]}
        got = {n for g in self.m.week_games(self.board) for n in (g["away_name"], g["home_name"])}
        self.assertEqual(got, names)

    def test_an_empty_board_is_an_empty_week(self):
        self.assertEqual(self.m.week_games({}), [])
        self.assertEqual(self.m.week_games(None), [])

    def test_the_json_says_which_games_are_already_forecast(self):
        first = self.board["events"][0]["id"]
        self.m.save({"forecasts": [{"event_id": first}, {"event_id": first}]})
        self.m.get = lambda url: self.board
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.cmd_week(as_json=True), 0)
        d = json.loads(out.getvalue())
        rows = {g["event_id"]: g["forecast_rows"] for g in d["games"]}
        self.assertEqual(rows[first], 2)
        self.assertEqual(sum(rows.values()), 2)
        self.assertEqual(d["week"], self.board["week"]["number"])


class TheDeckRunsTheForecastForAClickedGame(unittest.TestCase):
    """props.js: list the week, start one run, report on it.

    Driven through the real module with a fake Express app and a stub
    props-forecast.py, so nothing reaches ESPN and nothing needs a server.
    """

    API = ROOT / "mission-control-api"
    NODE = shutil.which("node")
    DEPS = (API / "node_modules" / "better-sqlite3").is_dir()

    STUB = (
        "import json, sys, time\n"
        "cmd = sys.argv[1]\n"
        "if cmd == 'week':\n"
        "    print(json.dumps({'week': 3, 'games': [{'event_id': '401872948', 'fixture': 'ATL @ GB'}]}))\n"
        "elif cmd == 'forecast' and sys.argv[2] == '11111':\n"
        "    print('partial', flush=True); print('boom', file=sys.stderr); sys.exit(1)\n"
        "elif cmd == 'forecast':\n"
        "    time.sleep(0.6); print('ran ' + sys.argv[2])\n"
    )

    def setUp(self):
        if not (self.NODE and self.DEPS):
            self.skipTest("node or the Deck's node_modules are not installed")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "scripts").mkdir()
        (root / "scripts" / "props-forecast.py").write_text(self.STUB)
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, steps):
        """Run JS against the registered routes. `call(method, path, body)`
        resolves to {status, body}; `wait()` resolves when the job settles."""
        js = (
            f"const props = require({json.dumps(str(self.API / 'props.js'))});"
            "const routes = {};"
            "const app = {get:(p,f)=>routes['GET '+p]=f, post:(p,f)=>routes['POST '+p]=f};"
            "props.register(app);"
            "function call(m, p, body){ return new Promise(ok => {"
            "  const res = {code:200, status(c){this.code=c;return this;},"
            # Copied at the moment of sending, as Express serialises it -
            # a reference would show the job as it is later, not as sent.
            "               json(b){ok({status:this.code, body:JSON.parse(JSON.stringify(b))});}};"
            "  Promise.resolve(routes[m+' '+p]({body:body||{}}, res)); }); }"
            "async function wait(){ for(let i=0;i<100;i++){"
            "  const j=(await call('GET','/api/props/job')).body.job;"
            "  if(j && j.state!=='running') return j; await new Promise(r=>setTimeout(r,100)); } }"
            "(async () => { const out = {};" + steps +
            " process.stdout.write(JSON.stringify(out)); })();"
        )
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True,
                           env=env, cwd=str(self.API), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_week_is_the_scripts_answer(self):
        out = self.drive("out.w = await call('GET', '/api/props/week');")
        self.assertTrue(out["w"]["body"]["available"])
        self.assertEqual(out["w"]["body"]["games"][0]["fixture"], "ATL @ GB")

    def test_anything_but_digits_is_refused(self):
        out = self.drive(
            "out.a = await call('POST', '/api/props/forecast', {event_id: '1; rm -rf /'});"
            "out.b = await call('POST', '/api/props/forecast', {});"
            "out.c = await call('POST', '/api/props/forecast', {event_id: '12'});"
            "out.d = await call('POST', '/api/props/forecast', {event_id: '401872948; rm -rf /'});"
            "out.e = await call('POST', '/api/props/forecast', {event_id: 'x401872948'});"
            "out.j = await call('GET', '/api/props/job');")
        for k in ("a", "b", "c", "d", "e"):
            self.assertEqual(out[k]["status"], 400, k)
        self.assertIsNone(out["j"]["body"]["job"], "nothing may have started")

    def test_a_run_starts_and_finishes(self):
        out = self.drive(
            "out.s = await call('POST', '/api/props/forecast', {event_id: '401872948', fixture: 'ATL @ GB'});"
            "out.done = await wait();")
        self.assertEqual(out["s"]["status"], 202)
        self.assertEqual(out["s"]["body"]["job"]["state"], "running")
        self.assertEqual(out["done"]["state"], "done")
        self.assertIn("ran 401872948", out["done"]["log"])

    def test_one_run_at_a_time(self):
        # Two runs write the same forecasts.json; the later save would drop
        # the earlier run's rows.
        out = self.drive(
            "out.a = await call('POST', '/api/props/forecast', {event_id: '401872948'});"
            "out.b = await call('POST', '/api/props/forecast', {event_id: '401872953'});"
            "out.done = await wait();"
            "out.c = await call('POST', '/api/props/forecast', {event_id: '401872953'});"
            "await wait();")
        self.assertEqual(out["b"]["status"], 409)
        self.assertEqual(out["done"]["event_id"], "401872948")
        self.assertEqual(out["c"]["status"], 202, "a finished run must not block the next")

    def test_a_failed_run_says_so_with_what_the_script_said(self):
        out = self.drive(
            "await call('POST', '/api/props/forecast', {event_id: '11111'});"
            "out.done = await wait();")
        self.assertEqual(out["done"]["state"], "failed")
        self.assertEqual(out["done"]["code"], 1)
        self.assertIn("boom", out["done"]["log"])

    def test_the_process_gets_an_argument_list_not_a_shell(self):
        src = (self.API / "props.js").read_text()
        self.assertIn("execFile('python3', [SCRIPT, ...args]", src)
        self.assertNotIn("exec(", src.replace("execFile(", ""))


class ThePropsHallIsNotRebuiltUnderYourFingers(unittest.TestCase):
    """The village re-renders the open panel every six seconds.

    Harmless for a panel that only shows numbers. The Props Hall has a search
    box and a live run status, and the rebuild wiped the box mid-word and
    blanked the status - found by driving the page in a browser, where the
    status line read "Forecasting…", then nothing, then "Forecasting…".
    """

    def test_the_six_second_pull_leaves_the_hall_alone(self):
        src = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        pull = src.split("async function pull(", 1)[1].split("\nfunction ", 1)[0]
        # Since 2026-09-25 no panel is redrawn by the pull - every house works
        # like the hall, at the owner's request.
        self.assertNotIn("openPanel(", pull)
        self.assertNotIn("Panel(", pull.split("NO PANEL IS REDRAWN HERE", 1)[1])

    def test_game_buttons_carry_data_not_code(self):
        # esc() does not escape quotes, so a fixture inside an inline onclick
        # string would be one apostrophe from breaking the page.
        src = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        body = src.split("function drawWeek(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('data-event="', body)
        self.assertNotIn("onclick", body)

    def card(self, row):
        """forecastCard() from the page, rendered by node - the real function,
        lifted out with the two helpers it calls."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        src = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        esc = re.search(r"const esc = .*\n", src).group(0)
        # A top-level function ends at the first "}" in column 0.
        fns = "".join(re.search(rf"^function {n}\(.*?^\}}\n", src, re.S | re.M).group(0)
                      for n in ("fcTile", "forecastCard"))
        js = esc + fns + f"process.stdout.write(forecastCard({json.dumps(row)}));"
        r = subprocess.run([node, "-e", js], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    ROW = {"player": "Nico Collins", "team": "HOU", "market": "receptions", "unit": "rec",
           "probabilities": {"3": 86.7, "4": 66.1}, "interval": {"3": [81, 92], "4": [54, 77]},
           "sample_mean": 4.8, "p25": 3, "p75": 7, "sample_games": 17, "actual": None}

    def test_the_cards_show_the_injury_flag(self):
        html = self.card(dict(self.ROW, injury={"status": "Questionable", "date": "2026-09-23"}))
        self.assertIn("\u26a0 QUESTIONABLE 09-23", html)

    def test_a_healthy_card_carries_no_flag(self):
        self.assertNotIn("\u26a0", self.card(dict(self.ROW, injury=None)))

    def test_the_card_shows_the_books_line_as_written(self):
        html = self.card(dict(self.ROW, line={"book": "DraftKings", "line": [4.5],
                                              "shown": "DraftKings 4.5"}))
        self.assertIn('class="fcline">DraftKings 4.5<', html)
        self.assertNotIn("fcline", self.card(dict(self.ROW, line=None)))


class TheBooksLineIsShownAndNeverUsed(unittest.TestCase):
    """DraftKings' line beside each forecast - and nothing more.

    ESPN carries one sportsbook's player props for every game: 1,072 for
    LAC @ BUF, each with its current and opening line but no price. The line
    is where the book expects a 50/50 split, which makes it the one number
    worth reading beside a percentage. The owner asked to SEE it and was
    explicit that it must not change a percentage, so that is the first thing
    tested here.

    Fixtures: ESPN's real odds listing and real propBets page for LAC @ BUF,
    trimmed to three players plus the look-alike props the parser must skip.
    """

    def setUp(self):
        self.m = load("props_forecast_lines", "props-forecast.py")
        self.odds = json.loads((FIXTURES / "espn-nfl-odds.json").read_text())
        self.props = json.loads((FIXTURES / "espn-nfl-propbets.json").read_text())

    def aid(self, it):
        return re.search(r"/athletes/(\d+)", it["athlete"]["$ref"]).group(1)

    # --- the one rule ---------------------------------------------------

    def test_a_line_changes_no_percentage(self):
        sample = [255.0, 198.0, 241.0, 310.0, 226.0, 187.0, 264.0, 233.0, 219.0, 280.0]
        args = ("1", "A @ B", None, "2", "A Passer", "QB", "AAA",
                {"passing_yards": sample}, ["2026"], None, None)
        bare = self.m.rows_for_player(*args)
        for value in ([241.5], [150.5], [400.5], [225.5, 229.5]):
            lined = self.m.rows_for_player(*args, {"passing_yards": {
                "book": "DraftKings", "line": value, "open": value, "shown": "x"}})
            for key in ("probabilities", "interval", "sample_mean", "p25", "p75",
                        "bandwidth", "coarse", "seed"):
                self.assertEqual(bare[0][key], lined[0][key], f"{key} moved with line {value}")
            self.assertEqual(lined[0]["line"]["line"], value)

    def test_nothing_that_computes_a_percentage_mentions_a_line(self):
        src = (SCRIPTS / "props-forecast.py").read_text()
        for fn in ("bandwidth", "point_prob", "smoothed_probs", "interval",
                   "percentile", "is_coarse", "samples_for", "strongest"):
            body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
            self.assertNotIn("line", re.sub(r'""".*?"""|#.*', "", body, flags=re.S)
                             .replace("newline", ""), fn)

    # --- reading ESPN ---------------------------------------------------

    def test_every_full_game_line_in_the_fixture_is_read(self):
        # Round trip against the payload, not restated numbers.
        book_of = {spec["book"]: m for m, spec in self.m.MARKETS.items() if spec.get("book")}
        got = self.m.lines_from(self.props["items"], "DraftKings")
        want = {}
        for it in self.props["items"]:
            market = book_of.get(it["type"]["name"])
            if market:
                want.setdefault((self.aid(it), market), set()).add(it["current"]["target"]["value"])
        self.assertTrue(want)
        self.assertEqual({k: sorted(v) for k, v in want.items()},
                         {k: v["line"] for k, v in got.items()})

    def test_look_alike_props_are_not_read_as_full_game_lines(self):
        names = {it["type"]["name"] for it in self.props["items"]}
        decoys = {"1st Half Total Passing Yards", "Passing Yards Milestones",
                  "Anytime Touchdown Scorer", "Total Pass Completions (incl. overtime)"}
        self.assertTrue(decoys <= names, "the fixture must still hold the decoys")
        read = self.m.lines_from(self.props["items"], "DraftKings")
        self.assertTrue(all(m in self.m.MARKETS for _a, m in read))
        self.assertEqual(len(read), len({(a, m) for a, m in read}))

    def test_the_over_and_under_copies_collapse_to_one_line(self):
        pairs = collections.Counter((self.aid(i), i["type"]["name"]) for i in self.props["items"]
                                    if i["type"]["name"].startswith("Total"))
        self.assertTrue(any(n == 2 for n in pairs.values()), "ESPN sends each side")
        for v in self.m.lines_from(self.props["items"], "DraftKings").values():
            self.assertEqual(len(v["line"]), 1, v)

    def test_a_split_line_is_kept_as_it_was_offered(self):
        def side(v):
            return {"type": {"name": "Total Passing Yards (incl. overtime)"},
                    "athlete": {"$ref": "http://x/athletes/7?lang=en"},
                    "current": {"target": {"value": v}}, "open": {"target": {"value": v}}}
        got = self.m.lines_from([side(225.5), side(229.5)], "DraftKings")[("7", "passing_yards")]
        self.assertEqual(got["line"], [225.5, 229.5])
        self.assertEqual(got["shown"], "DraftKings o225.5/u229.5")

    def test_the_book_is_named_from_espn_not_assumed(self):
        pages = {"odds": self.odds, "props": dict(self.props, pageCount=1)}
        self.m.get = lambda url: pages["props"] if "propBets" in url else pages["odds"]
        lines, note = self.m.book_lines("401872953")
        self.assertIsNone(note)
        self.assertEqual({v["book"] for v in lines.values()},
                         {self.odds["items"][0]["provider"]["name"]})

    def test_every_page_is_read(self):
        # 1,072 props arrive as two pages of 1,000. Reading one loses the rest.
        asked = []
        first = dict(self.props, pageCount=2, items=self.props["items"][:10])
        second = dict(self.props, pageCount=2, items=self.props["items"][10:])

        def get(url):
            asked.append(url)
            if "propBets" not in url:
                return self.odds
            return second if "page=2" in url else first
        self.m.get = get
        lines, _ = self.m.book_lines("401872953")
        self.assertEqual(sum("propBets" in u for u in asked), 2)
        self.assertEqual(lines, self.m.lines_from(self.props["items"], lines and
                                                  next(iter(lines.values()))["book"]))
        self.assertTrue(all(u.startswith("https://") for u in asked))

    def test_no_lines_is_a_note_not_a_failure(self):
        def down(url):
            raise OSError("ESPN is down")
        self.m.get = down
        self.assertEqual(self.m.book_lines("1")[0], {})
        self.m.get = lambda url: {"items": [{"provider": {"name": "X"}}]}
        lines, note = self.m.book_lines("1")
        self.assertEqual(lines, {})
        self.assertIn("no book has posted", note)

    # --- where it shows ------------------------------------------------

    def test_forecast_passes_each_player_only_his_own_lines(self):
        body = (SCRIPTS / "props-forecast.py").read_text()
        body = body.split("def cmd_forecast(", 1)[1].split("\ndef ", 1)[0]
        self.assertEqual(body.count("book_lines(event_id)"), 1, "fetched once per run")
        self.assertIn("if a == aid", body)
        self.assertIn("avail, injury, mine)", body)

    def test_best_shows_it(self):
        row = self.m.rows_for_player("1", "A @ B", None, "2", "A Receiver", "WR", "AAA",
                                     {"receptions": [3.0, 5.0, 4.0, 6.0, 2.0, 5.0, 4.0, 3.0, 7.0, 4.0]},
                                     ["2026"], None, None,
                                     {"receptions": {"book": "DraftKings", "line": [4.5],
                                                     "open": [4.5], "shown": "DraftKings 4.5"}})[0]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.m.STORE = Path(tmp.name) / "f.json"
        self.m.save({"forecasts": [row]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.cmd_best("1")
        self.assertIn("DraftKings 4.5", out.getvalue())


class TheStandingFocusAndTheCountAreCode(unittest.TestCase):
    """Scout said "4 pending" on a morning when the log held none.

    Its instructions told it to count pending ideas and stop at five, and the
    example summary under that rule read "proposed 0 - 6 pending, nothing new
    that is not a rewording". Its line that morning was nearly that example,
    word for word, and it proposed nothing on the strength of a count it had
    got wrong.

    So code counts. The cycle does not wake the model at all when there is
    nothing for it to do, and when it does, the message ends in the facts:
    the count, the focus, and every phrase the gate would accept.

    The owner also asked for totes only "until we find another market". That
    is a standing focus, kept in one file, enforced at the intake - a sticker
    draft is refused by code whatever the model writes - and shown on every
    run and in the Deck.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents" / "scout" / "state"
        (self.state / "scans").mkdir(parents=True)
        (self.root / "scripts").symlink_to(SCRIPTS)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.scan("canvas tote bag", now, [("canvas tote bag", 0.012),
                                           ("canvas tote bag with zipper", 0.0)])
        self.scan("laptop sticker", now, [("laptop sticker", 0.022)])
        self.ideas([])
        self.old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.si = load("scout_ideas_focus", "scout-ideas.py")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ECOSYSTEM_ROOT", None)
        else:
            os.environ["ECOSYSTEM_ROOT"] = self.old
        self.tmp.cleanup()

    def scan(self, seed, when, rows):
        doc = {"seed": seed, "scanned_at": when, "excluded": [], "rows": [
            {"phrase": p, "supply": 1000, "heat": 1, "pull": 0.01, "price": 20.0,
             "match": 0.9, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 100, "heat_n": 10, "pull_n": 10,
             "score": sc, "tags": []} for p, sc in rows]}
        (self.state / "scans" / (seed.replace(" ", "-") + ".json")).write_text(json.dumps(doc))

    def ideas(self, statuses):
        (self.state / "ideas.json").write_text(json.dumps({"ideas": [
            {"id": n + 1, "title": f"Idea {n + 1}", "product": "tote", "status": st,
             "evidence": {"phrase": "canvas tote bag"}} for n, st in enumerate(statuses)]}))

    def run_py(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), *args],
                              capture_output=True, text=True,
                              env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))

    # --- the count ------------------------------------------------------

    def test_pending_is_counted_from_the_log(self):
        self.ideas(["approved", "rejected", "pending", "approved"])
        self.assertEqual(self.si.pending_count(), 1)
        self.ideas([])
        self.assertEqual(self.si.pending_count(), 0)

    def test_the_run_is_held_at_the_limit_and_not_below_it(self):
        self.ideas(["pending"] * (self.si.PENDING_HOLD - 1))
        self.assertEqual(self.si.should_run(), (True, None))
        self.ideas(["pending"] * self.si.PENDING_HOLD)
        ok, why = self.si.should_run()
        self.assertFalse(ok)
        self.assertIn(str(self.si.PENDING_HOLD), why)

    def test_nothing_to_propose_into_holds_the_run(self):
        for f in (self.state / "scans").iterdir():
            f.unlink()
        ok, why = self.si.should_run()
        self.assertFalse(ok)
        self.assertIn("market-scan.py", why)

    def test_the_instructions_no_longer_ask_scout_to_count(self):
        header = (ROOT / "agents" / "scout" / "_scout-agents-header.md").read_text()
        self.assertNotIn("Count what is still", header)
        self.assertNotIn("6 pending", header, "the example it copied is gone")
        self.assertIn("FACTS, COUNTED BY CODE", header)

    # --- the focus --------------------------------------------------------

    def test_a_focus_is_set_from_plain_words_and_cleared(self):
        r = self.run_py("focus", "tote", "bags")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.si.focus(), "tote")
        self.run_py("focus", "--clear")
        self.assertIsNone(self.si.focus())

    def test_a_word_that_is_not_a_product_is_refused(self):
        r = self.run_py("focus", "rocketship")
        self.assertEqual(r.returncode, 2)
        self.assertIsNone(self.si.focus())

    def test_the_intake_refuses_another_product_while_focused(self):
        self.run_py("focus", "tote")
        (self.state / "drafts.txt").write_text(
            "canvas tote bag | Map Tote | tote | x\n"
            "laptop sticker | Retro Sticker | sticker | y\n")
        r = self.run_py("intake")
        self.assertIn("1 filed, 1 refused", r.stdout)
        self.assertIn("the focus is tote", r.stderr)
        filed = self.si._rows_of(json.loads((self.state / "proposals.json").read_text()))
        self.assertEqual([p["title"] for p in filed], ["Map Tote"])

    def test_unfocused_takes_any_product(self):
        (self.state / "drafts.txt").write_text(
            "canvas tote bag | Map Tote | tote | x\n"
            "laptop sticker | Retro Sticker | sticker | y\n")
        self.assertIn("2 filed, 0 refused", self.run_py("intake").stdout)

    def test_the_phrases_handed_over_are_exactly_what_the_gate_accepts(self):
        # Zero-score phrases are out; the sticker is out under a tote focus.
        self.assertEqual({p for p, _ in self.si.proposable()},
                         {"canvas tote bag", "laptop sticker"})
        self.run_py("focus", "tote")
        self.assertEqual([p for p, _ in self.si.proposable()], ["canvas tote bag"])
        for phrase, _row in self.si.proposable():
            _r, _s, problem = self.si.measured(phrase)
            self.assertIsNone(problem)

    def test_the_brief_carries_the_real_count_and_the_focus(self):
        self.ideas(["pending", "pending", "approved"])
        self.run_py("focus", "tote")
        out = self.run_py("brief").stdout
        self.assertIn("waiting on the user's verdict: 2", out)
        self.assertIn("FOCUS: tote", out)
        self.assertIn("canvas tote bag", out)
        self.assertNotIn("laptop sticker", out)
        self.assertNotIn("with zipper", out)

    # --- the real wrapper, with a stand-in model --------------------------

    FAKE = (
        "#!/bin/bash\n"
        "for ((i=1;i<=$#;i++)); do [ \"${!i}\" = \"--message\" ] && j=$((i+1)) && "
        "printf '%s' \"${!j}\" > \"$ECOSYSTEM_ROOT/received.txt\"; done\n"
        "sleep 1\n"
        "echo 'proposed 1' > \"$ECOSYSTEM_ROOT/agents/scout/state/last-run.txt\"\n"
        "printf 'canvas tote bag | Map Tote | tote | x\\nlaptop sticker | S | sticker | y\\n' "
        "> \"$ECOSYSTEM_ROOT/agents/scout/state/drafts.txt\"\n"
    )

    def cycle(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "openclaw"
        fake.write_text(self.FAKE)
        fake.chmod(0o755)
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root),
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
        return subprocess.run(["bash", str(SCRIPTS / "scout-cycle.sh")],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_a_held_run_never_wakes_the_model(self):
        self.ideas(["pending"] * self.si.PENDING_HOLD)
        r = self.cycle()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.root / "received.txt").exists(), "the model was woken")
        line = (self.state / "last-run.txt").read_text()
        self.assertIn("held by code", line)
        self.assertIn(line.strip(), (self.root / "agents" / "scout" / "MEMORY.md").read_text())

    def test_a_focused_run_gets_the_facts_and_code_enforces_the_focus(self):
        self.run_py("focus", "tote")
        r = self.cycle()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        msg = (self.root / "received.txt").read_text()
        self.assertIn("must be for: tote", msg)
        self.assertIn("FACTS, COUNTED BY CODE", msg)
        self.assertIn("waiting on the user's verdict: 0", msg)
        self.assertIn("state/drafts.txt", msg)
        self.assertNotIn("proposals.json", msg, "the old instruction is back")
        self.assertIn("standing focus: tote", r.stdout)
        self.assertEqual([i["title"] for i in json.loads((self.state / "ideas.json").read_text())["ideas"]],
                         ["Map Tote"], "the sticker draft must have been refused")

    def test_words_on_the_command_line_are_still_for_one_run_only(self):
        self.assertIsNone(self.si.focus())
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "openclaw").write_text(self.FAKE)
        (bin_dir / "openclaw").chmod(0o755)
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root),
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
        subprocess.run(["bash", str(SCRIPTS / "scout-cycle.sh"), "sticker"],
                       capture_output=True, text=True, env=env, timeout=60)
        self.assertIn("must be for: sticker", (self.root / "received.txt").read_text())
        self.assertIsNone(self.si.focus(), "a one-run focus was persisted")


class ScoutsHouseShowsTheFocus(unittest.TestCase):
    """A focus nobody can see is a focus nobody clears."""

    def test_the_api_reads_the_one_file(self):
        src = (ROOT / "mission-control-api" / "scout.js").read_text()
        self.assertIn("'focus.txt'", src)
        self.assertEqual(src.count("focus: readFocus()"), 2, "both answers carry it")

    def render(self, focus):
        """drawIdeas() from the page, run by node against a one-element DOM."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        src = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        esc = re.search(r"const esc = .*\n", src).group(0)
        fn = re.search(r"^function drawIdeas\(.*?^\}\n", src, re.S | re.M).group(0)
        js = (esc + "const host = {innerHTML: ''};"
              "const document = {getElementById: () => host};"
              f"let IDEAS = [], openIdea = null, SCOUT_FOCUS = {json.dumps(focus)};"
              + fn + "drawIdeas(); process.stdout.write(host.innerHTML);")
        r = subprocess.run([node, "-e", js], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_the_house_shows_it_with_how_to_clear_it(self):
        html = self.render("tote")
        self.assertIn("Focus: tote only.", html)
        self.assertIn("focus --clear", html)

    def test_no_focus_no_banner(self):
        self.assertNotIn("Focus:", self.render(None))


class TheGateJudgesAScanTheWayCompareDoes(unittest.TestCase):
    """"tote bag pattern" topped the first real 'tote bag' scan at $4.00.

    The scan flagged it on screen - "PRICED LIKE A DIFFERENT PRODUCT", about
    $21 for the rest - and saved it anyway, because a saved scan keeps every
    row and is re-judged on read. compare re-judged it. The evidence gate did
    not: it read the rows as saved, so the phrase would have gone to Scout as
    the best tote market measured, and every idea on it would have been a
    sewing pattern's market.

    One verdict now, market-scan's, used by both. A trademark CHECK stays the
    owner's call at the gate, as it is everywhere else.

    The rows are the nine the real scan saved, numbers as printed.
    """

    ROWS = [("tote bag pattern", 73534, 4.00, 0.96, 0.0370),
            ("tote bag for school", 78015, 16.95, 1.00, 0.0025),
            ("tote bag women", 162369, 120.00, 0.96, 0.0022),
            ("tote bag etsy", 2778, 9.70, 0.68, 0.0011),
            ("tote bag", 1111895, 11.65, 1.00, 0.0009),
            ("tote bag coach", 4628, 21.99, 0.80, 0.0007),
            ("tote bag black", 186557, 30.00, 0.80, 0.0005),
            ("tote bag sale", 3285, 25.00, 0.92, 0.0005),
            ("tote bag designer", 15922, 150.00, 1.00, 0.0003)]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        scans = self.root / "agents" / "scout" / "state" / "scans"
        scans.mkdir(parents=True)
        self.doc = {"seed": "tote bag", "excluded": [],
                    "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "rows": [{"phrase": p, "supply": s, "heat": 0.01, "pull": 0.02, "price": pr,
                              "match": m, "selling": 6, "sales_n": 10, "reviews": 30, "returned": 25, "heat_n": 25, "pull_n": 25,
                              "score": sc, "tags": []} for p, s, pr, m, sc in self.ROWS]}
        (scans / "tote-bag.json").write_text(json.dumps(self.doc))
        self.old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.si = load("scout_ideas_gate", "scout-ideas.py")
        self.ms = load("market_scan_gate", "market-scan.py")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ECOSYSTEM_ROOT", None)
        else:
            os.environ["ECOSYSTEM_ROOT"] = self.old
        self.tmp.cleanup()

    def test_the_sewing_pattern_market_is_refused_at_the_gate(self):
        row, _scan, problem = self.si.measured("tote bag pattern")
        self.assertIsNone(row)
        self.assertIn("priced like a different product", problem)

    def test_it_is_not_handed_to_scout(self):
        handed = [p for p, _ in self.si.proposable()]
        self.assertNotIn("tote bag pattern", handed)
        self.assertIn("tote bag for school", handed)

    def test_a_trademark_check_is_still_the_owners_call(self):
        kind, why = self.ms.verdicts(self.doc)["tote bag coach"]
        self.assertEqual(kind, "check")
        self.assertIn("your call", why)
        row, _scan, problem = self.si.measured("tote bag coach")
        self.assertIsNone(problem)
        self.assertEqual(row["phrase"], "tote bag coach")

    def test_compare_and_the_gate_agree(self):
        # Every row compare keeps, the gate accepts; every row the gate
        # refuses, compare drops. The only rows they treat differently are
        # the CHECK ones, on purpose.
        kept = {r["phrase"] for r in self.ms.usable_rows(self.doc)}
        verdict = self.ms.verdicts(self.doc)
        for p, *_ in self.ROWS:
            refused = self.si.measured(p)[2] is not None
            if p in kept:
                self.assertFalse(refused, p)
            elif verdict[p][0] != "check":
                self.assertTrue(refused, p)

    def test_usable_rows_still_drops_what_it_dropped(self):
        kept = {r["phrase"] for r in self.ms.usable_rows(self.doc)}
        self.assertNotIn("tote bag pattern", kept)
        self.assertNotIn("tote bag coach", kept)
        self.assertIn("tote bag", kept)


class EmilyHasNoDailyBuildCount(unittest.TestCase):
    """"Let's get rid of the 3 creation cap limit on Emily" (2026-09-30).

    Builds were refused after the third of an Eastern day. What bounds a build
    now is money - the task queue's daily_total_spend_cap, which answers 429 -
    not a count. Checked against the queue as it would be on a busy day: five
    builds already started today, and the sixth still goes in.
    """

    def run_build(self, answers):
        nb = load("emily_new_build_nocap", "emily-new-build.py")
        calls = []

        def call(method, path, body=None):
            calls.append((method, path))
            return answers(method, path)
        nb.call = call
        nb.generate_artwork = lambda *a, **k: (True, "stub - no artwork, no spend in a test")
        argv = sys.argv
        sys.argv = ["emily-new-build.py", "Sixth Tote Today", "--product", "tote"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = nb.main()
        finally:
            sys.argv = argv
        return code, calls, out.getvalue()

    def test_a_sixth_build_in_a_day_is_queued(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        today = [{"id": n, "created_at": now, "status": "done",
                  "payload": json.dumps({"idea": f"Tote {n}"})} for n in range(1, 6)]

        def answers(method, path):
            if method == "GET":
                return 200, today
            return 201, {"id": 99}
        code, calls, said = self.run_build(answers)
        self.assertEqual(code, 0, said)
        self.assertIn(("POST", "/tasks"), calls)
        self.assertIn("queued task #99", said)

    def test_the_spend_cap_still_says_no(self):
        code, calls, said = self.run_build(
            lambda m, p: (429, {"error": "daily_total_spend_cap 10 would be exceeded"}))
        self.assertEqual(code, 1)
        self.assertIn("spend cap hit", said)

    def test_approving_an_idea_has_no_cap_to_get_round(self):
        src = (SCRIPTS / "scout-review.py").read_text()
        self.assertNotIn("CAP_REACHED", src)
        self.assertNotIn("--force", src)
        self.assertFalse((SCRIPTS / "emily-cap.py").exists())


class TheBriefingValuesBelfortsBookAsBelfortDoes(unittest.TestCase):
    """"Belfort $1,593.29 (-84.07%)" in the briefing; "+0.95%" on the Deck.

    fury-collect copied each open position as symbol, shares and cost basis,
    then valued it by looking for a price under "price", "last" or "entry" -
    none of which it had copied - so all five positions counted as nothing and
    Belfort's value was its cash. The book's total is already on disk:
    belfort-trade.py `mark` writes it every cycle, from the code that owns
    the book. The briefing reads that now.

    The portfolio here is written by the real belfort-trade.py buy and mark,
    not typed into the test.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        b = self.root / "agents" / "belfort"
        (b / "state").mkdir(parents=True)
        (b / "data").mkdir(parents=True)
        (b / "state" / "portfolio.json").write_text(
            (ROOT / "agents" / "belfort" / "state" / "portfolio.seed.json").read_text())
        self.quotes = b / "data" / "quotes.json"
        self.fc = load("fury_collect_money", "fury-collect.py")

    def tearDown(self):
        self.tmp.cleanup()

    def trade(self, *args):
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), *args],
                           capture_output=True, text=True,
                           env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def price(self, **px):
        self.quotes.write_text(json.dumps({"quotes": {k: {"price": v} for k, v in px.items()},
                                           "benchmarks": {"QQQ": rising_qqq()}}))

    def book(self):
        """Buy at one set of prices, mark at another - the real shape."""
        (self.quotes.parent / "news.json").write_text(json.dumps(fresh_news("CRWD", "MRVL")))
        self.price(CRWD=241.88, MRVL=241.48)
        self.trade("buy", "CRWD", "4", "--headline", "h-CRWD")
        self.trade("buy", "MRVL", "9", "--headline", "h-MRVL")
        self.price(CRWD=261.30, MRVL=254.10)
        self.trade("mark")
        return json.loads((self.root / "agents" / "belfort" / "state" / "portfolio.json").read_text())

    def test_the_briefing_value_is_the_books_own_value(self):
        p = self.book()
        state = self.fc.state_summary(self.root / "agents" / "belfort")
        value, _pnl, pct = self.fc.agent_money(state)
        self.assertAlmostEqual(value, p["market_value"], places=2)
        self.assertGreater(value, p["cash"] + 1000, "the positions must count for something")
        self.assertGreater(pct, -10, "a small gain must not read as a large loss")

    def test_without_the_book_total_the_positions_are_still_valued(self):
        # An older portfolio.json, marked before market_value was written at
        # book level, still carries each position's own market value.
        p = self.book()
        state = self.fc.state_summary(self.root / "agents" / "belfort")
        state.pop("market_value")
        value, _pnl, _pct = self.fc.agent_money(state)
        self.assertAlmostEqual(value, p["market_value"], places=1)

    def test_the_books_own_total_wins_over_a_recount(self):
        # When the two disagree - a position with no mark of its own - the
        # figure the book's owner wrote is the answer, not a sum redone here.
        state = {"cash": 1000.0, "starting_cash": 10000, "market_value": 10250.0,
                 "positions": [{"symbol": "X", "shares": 10, "cost_basis": 900.0}]}
        self.assertEqual(self.fc.agent_money(state)[0], 10250.0)

    def test_a_position_with_only_a_cost_basis_is_valued_at_cost_not_zero(self):
        state = {"cash": 1000.0, "starting_cash": 10000,
                 "positions": [{"symbol": "X", "shares": 10, "cost_basis": 900.0}]}
        self.assertEqual(self.fc.agent_money(state)[0], 10000.0)

    def test_the_briefing_line(self):
        self.book()
        report = {"agents": {"belfort": {"state": self.fc.state_summary(
            self.root / "agents" / "belfort")}}, "generated_utc": "now"}
        text = self.fc.build_briefing(report, "2026-09-24")
        line = [l for l in text.splitlines() if l.startswith("- **Belfort**")][0]
        self.assertNotIn("(-", line, line)


class TheShopBannerIsCroppedByCode(unittest.TestCase):
    """Etsy's big banner is 4:1; the image model's widest shape is 21:9.

    Asked for "4:1" in words, a model returns whatever it likes. So it is
    asked for 21:9 and code keeps the centre 4:1 band. And a banner is not a
    print file: the print direction ("plain background") would be wrong on
    it, so generate() takes the banner's own direction - no text, no bags.
    """

    def setUp(self):
        self.eb = load("emily_banner", "emily-banner.py")
        self.ea = load("emily_assets_banner", "emily-assets.py")

    def test_a_wide_image_keeps_full_height(self):
        self.assertEqual(self.eb.crop_box(1536, 672), (0, 144, 1536, 384))

    def test_a_squarer_image_trims_top_and_bottom(self):
        x, y, w, h = self.eb.crop_box(1024, 1024)
        self.assertEqual((w, h), (1024, 256))
        self.assertEqual((x, y), (0, 384))

    def test_an_image_wider_than_four_to_one_trims_the_sides(self):
        x, y, w, h = self.eb.crop_box(2000, 300)
        self.assertEqual((w, h), (1200, 300))
        self.assertEqual(x, 400)

    def test_the_crop_takes_exactly_that_band(self):
        # Each pixel holds its own (x, y), so every one can be checked. Both
        # shapes: one trims the sides, one trims top and bottom.
        for w, h in ((20, 3), (8, 6)):
            px = bytearray()
            for y in range(h):
                for x in range(w):
                    px += bytes((x, y, 0, 255))
            x0, y0, cw, ch = self.eb.crop_box(w, h)
            out = self.eb.crop(px, w, (x0, y0, cw, ch))
            self.assertEqual(len(out), cw * ch * 4)
            got = [(out[i], out[i + 1]) for i in range(0, len(out), 4)]
            want = [(x, y) for y in range(y0, y0 + ch) for x in range(x0, x0 + cw)]
            self.assertEqual(got, want, f"{w}x{h}")

    def request_for(self, **kw):
        sent = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"choices": [{"message": {}}]}'

        def urlopen(req, timeout=None):
            sent["body"] = json.loads(req.data)
            return Resp()
        old = self.ea.urllib.request.urlopen
        self.ea.urllib.request.urlopen = urlopen
        try:
            with self.assertRaises(RuntimeError):      # no image in the reply
                self.ea.generate("/dev/null", "a prompt", "k", model="m", **kw)
        finally:
            self.ea.urllib.request.urlopen = old
        return sent["body"]

    def test_the_banner_gets_its_own_direction_and_shape(self):
        body = self.request_for(direction="NO text, no bags.", aspect="21:9")
        text = body["messages"][0]["content"]
        self.assertIn("NO text, no bags.", text)
        self.assertNotIn(self.ea.PRINT_DIRECTION, text)
        self.assertEqual(body["image_config"], {"aspect_ratio": "21:9"})

    def test_product_art_is_unchanged(self):
        body = self.request_for()
        self.assertIn(self.ea.PRINT_DIRECTION, body["messages"][0]["content"])
        self.assertNotIn("image_config", body)

    def test_the_banner_asks_for_no_words_and_no_products(self):
        for must in ("NO text", "no bags", "no products", "no logo"):
            self.assertIn(must.lower(), self.eb.DIRECTION.lower())

    def test_the_deck_serves_only_plain_filenames_from_the_shop_folder(self):
        src = (ROOT / "mission-control-api" / "emily.js").read_text()
        route = src.split("app.get('/api/emily/shop/:name'", 1)[1].split("app.get(", 1)[0]
        self.assertIn("IMAGE_RE.test(name", route)
        self.assertIn("path.join(SHOP, name)", route)


class ABuildThatStoppedDoesNotSayBuilding(unittest.TestCase):
    """"It's been building for like 20 minutes."

    The gallery took a build's status from its build.json, and called a
    folder with none "in_progress". The dispatcher stops an Emily run at ten
    minutes and marks the task FAILED in the queue - which the gallery never
    asked - so the card said "building..." for as long as anyone looked. Now
    Emily's own record wins where she wrote one, and the queue answers where
    she did not.
    """

    API = ROOT / "mission-control-api"
    NODE = shutil.which("node")

    def status(self, build, task):
        if not (self.NODE and (self.API / "node_modules" / "better-sqlite3").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        js = (f"const m=require({json.dumps(str(self.API / 'emily.js'))});"
              f"process.stdout.write(JSON.stringify(m.statusOf({json.dumps(build)},"
              f"{json.dumps(task)})))")
        r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_run_the_dispatcher_stopped_reads_failed(self):
        self.assertEqual(self.status({}, {"status": "failed"}), "failed")

    def test_a_running_task_reads_in_progress(self):
        self.assertEqual(self.status({}, {"status": "in_progress"}), "in_progress")

    def test_a_task_not_yet_started_reads_queued(self):
        for st in ("pending", "assigned"):
            self.assertEqual(self.status({}, {"status": st}), "queued")

    def test_a_folder_nothing_is_working_on_says_so(self):
        self.assertEqual(self.status({}, None), "no_record")

    def test_emilys_own_record_wins(self):
        self.assertEqual(self.status({"status": "ready_for_review"}, {"status": "failed"}),
                         "ready_for_review")

    def test_the_task_is_found_by_its_build_dedupe_key(self):
        # emily-new-build.py gives every build task this key; the Deck joins on it.
        nb = (SCRIPTS / "emily-new-build.py").read_text()
        self.assertIn('"dedupe_key": f"emily-build-{slug}"', nb)
        js = (self.API / "emily.js").read_text()
        self.assertIn("`emily-build-${slug}`", js)

    def words(self, status, task):
        if not (self.NODE and (self.API / "node_modules" / "better-sqlite3").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        js = (f"const m=require({json.dumps(str(self.API / 'emily.js'))});"
              f"process.stdout.write(JSON.stringify(m.statusWords({json.dumps(status)},"
              f"{json.dumps(task)})))")
        r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_failed_build_says_why_in_the_dispatchers_words(self):
        w = self.words("failed", {"result": "agent exited with code -9: timed out after 600s"})
        self.assertEqual(w["label"], "failed")
        self.assertTrue(w["warn"])
        self.assertIn("timed out after 600s", w["note"])

    def test_a_running_build_says_for_how_long(self):
        started = (datetime.now(timezone.utc) - timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(self.words("in_progress", {"started_at": started})["label"], "building 12 min")

    def test_both_pages_print_the_apis_words_not_their_own(self):
        # The Deck kept its own copy of the labels after the village was fixed,
        # and went on saying "building..." about a failed run.
        for page in ("village.html", "dashboard.html"):
            src = (ROOT / "mission-control-api" / "public" / page).read_text()
            pills = src.split("function buildPills(", 1)[1].split("\nfunction ", 1)[0]
            self.assertIn("b.status_words", pills, page)
            self.assertNotIn("'building…'", pills, page)
            why = src.split("function blockedReason(", 1)[1].split("\nfunction ", 1)[0]
            self.assertIn("b.status_words.note", why, page)

    def test_images_are_versioned_so_a_redraw_can_use_the_cache(self):
        js = (self.API / "emily.js").read_text()
        self.assertIn("?v=${Math.round(st.mtimeMs)}", js)
        route = js.split("app.get('/api/emily/builds/:slug/file/:name'", 1)[1].split("app.", 1)[0]
        self.assertIn("req.query.v ? 'max-age=31536000, immutable' : 'no-cache'", route)
        for page in ("village.html", "dashboard.html"):
            src = (ROOT / "mission-control-api" / "public" / page).read_text()
            self.assertIn("cover.url ||", src, page)
            self.assertIn("lbFiles[i].url ||", src, page)

    def test_the_server_hands_emily_the_queue(self):
        src = (ROOT / "mission-control-api" / "server.js").read_text()
        self.assertIn("emilyRoutes.register(app, db)", src)


class AComparisonIsDrawnAsTheProduct(unittest.TestCase):
    """Compare mode drew every model with the plain-background print direction
    and no shape, whatever the product - so a tote comparison compared
    pictures that would never be printed. The product now travels to
    generate(), the one place a request is built."""

    def test_compare_passes_the_product_to_generate(self):
        ea = load("emily_assets_cmp", "emily-assets.py")
        seen = []

        def fake(path, prompt, key, model=None, product="", direction=None, aspect=None):
            seen.append((model, product))
            Path(path).write_bytes(b"x")
            return 1, {}
        ea.generate = fake
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with contextlib.redirect_stdout(io.StringIO()):
            ea.compare("waves", "m/one,m/two", "key", Path(tmp.name) / "cmp", "tote")
        self.assertEqual(seen, [("m/one", "tote"), ("m/two", "tote")])

    def test_the_command_line_hands_it_over(self):
        src = (SCRIPTS / "emily-assets.py").read_text()
        self.assertIn("compare(a.prompt, a.compare, key, Path(a.out), a.product)", src)


class TheStoreReportIsSubtractionNotMemory(unittest.TestCase):
    """Nothing in the system looked at the shop's own listings.

    store-report.py snapshots them once a day through the one Etsy client,
    and the change since yesterday is a subtraction between two files. A
    number Etsy did not send is shown as missing, never as zero - a listing
    "with 0 views" and one Etsy said nothing about are different findings.
    """

    NOW = 1790265600          # 2026-09-24 16:00 UTC

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.sr = load("store_report_t", "store-report.py")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ECOSYSTEM_ROOT", None)
        else:
            os.environ["ECOSYSTEM_ROOT"] = self.old
        self.tmp.cleanup()

    def row(self, i, views, favs, age=5, title=None):
        return {"listing_id": i, "title": title or f"Tote {i}", "views": views, "favs": favs,
                "age_days": age, "price": 24.99, "url": None}

    def snap(self, day, rows):
        return {"day": day, "listings": rows}

    # --- reading Etsy -----------------------------------------------------

    def test_the_owners_shop_is_the_exact_name_not_the_first_result(self):
        results = [{"shop_id": 9, "shop_name": "VintageLoomTreasuresShop"},
                   {"shop_id": 555, "shop_name": "VintageLoomTreasures"}]
        self.assertEqual(self.sr.pick_shop(results, "vintageloomtreasures")["shop_id"], 555)
        self.assertIsNone(self.sr.pick_shop(results, "VintageLoom"))

    def test_a_listing_is_read_with_the_shared_arithmetic(self):
        created = int(time.time() - 4 * 86400)
        r = self.sr.snapshot_row({"listing_id": 1, "title": "Wave Tote", "views": 31,
                                  "num_favorers": 3, "original_creation_timestamp": created,
                                  "price": {"amount": 2499, "divisor": 100, "currency_code": "USD"}})
        self.assertEqual((r["views"], r["favs"], r["price"]), (31, 3, 24.99))
        self.assertAlmostEqual(r["age_days"], 4.0, delta=0.2)

    def test_a_count_etsy_did_not_send_is_missing_not_zero(self):
        r = self.sr.snapshot_row({"listing_id": 1, "title": "x"})
        self.assertIsNone(r["views"])
        self.assertIsNone(r["favs"])
        s = self.sr.summary(self.snap("d", [r]))
        self.assertIn("?", "\n".join(self.sr.lines(s)))

    def test_every_page_is_read(self):
        pages = {0: [{"listing_id": n} for n in range(100)], 100: [{"listing_id": 100}]}
        asked = []

        def call(path, key):
            asked.append(path)
            off = int(re.search(r"offset=(\d+)", path).group(1))
            return {"count": 101, "results": pages[off]}, {}, None
        self.sr._ep = lambda: types.SimpleNamespace(api_key=lambda: ("k:s", None), call=call)
        self.sr.STORE.mkdir(parents=True)
        self.sr.SHOP.write_text(json.dumps({"shop_id": 5, "shop_name": "S"}))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.sr.cmd_fetch([]), 0)
        saved = json.loads((self.sr.STORE / f"{et_time.day()}.json").read_text())
        self.assertEqual(len(saved["listings"]), 101)
        self.assertEqual(len(asked), 2)

    # --- the sums --------------------------------------------------------

    def test_since_yesterday_is_the_difference_per_listing(self):
        s = self.sr.summary(self.snap("2026-09-24", [self.row(1, 31, 3), self.row(2, 5, 1)]),
                            self.snap("2026-09-23", [self.row(1, 20, 2), self.row(2, 4, 1)]))
        by = {r["title"]: r for r in s["rows"]}
        self.assertEqual((by["Tote 1"]["views_gained"], by["Tote 1"]["favs_gained"]), (11, 1))
        self.assertEqual((s["views_gained"], s["favs_gained"]), (12, 1))
        self.assertEqual(s["rows"][0]["title"], "Tote 1", "the biggest gain leads")

    def test_a_new_listing_is_new_not_a_gain_from_zero(self):
        s = self.sr.summary(self.snap("b", [self.row(1, 5, 0), self.row(9, 40, 0)]),
                            self.snap("a", [self.row(1, 5, 0)]))
        new = [r for r in s["rows"] if r["title"] == "Tote 9"][0]
        self.assertTrue(new["new"])
        self.assertIsNone(new["views_gained"])
        self.assertEqual(s["views_gained"], 0)

    def test_the_first_snapshot_claims_no_change(self):
        s = self.sr.summary(self.snap("a", [self.row(1, 5, 0)]))
        self.assertIsNone(s["views_gained"])
        self.assertFalse(s["rows"][0]["new"])

    def test_old_listings_with_no_views_are_named(self):
        s = self.sr.summary(self.snap("a", [self.row(1, 0, 0, age=10, title="Hoodie"),
                                            self.row(2, 0, 0, age=2, title="Just Listed")]))
        self.assertEqual(s["quiet"], ["Hoodie"])

    def test_most_favourited_is_by_favourites(self):
        s = self.sr.summary(self.snap("a", [self.row(1, 90, 1), self.row(2, 10, 4), self.row(3, 5, 0)]))
        self.assertEqual([r["title"] for r in s["most_saved"]], ["Tote 2", "Tote 1"])

    # --- where it shows --------------------------------------------------

    def test_the_briefing_prints_the_reports_own_lines(self):
        self.sr.STORE.mkdir(parents=True)
        (self.sr.STORE / "2026-09-24.json").write_text(json.dumps(
            self.snap("2026-09-24", [self.row(1, 31, 3, title="Navy Wave Tote")])))
        fc = load("fury_collect_store", "fury-collect.py")
        text = fc.build_briefing({"agents": {}, "generated_utc": "now"}, "2026-09-24")
        self.assertIn("## The store", text)
        self.assertIn("Navy Wave Tote", text)

    def test_no_snapshot_says_how_to_start(self):
        self.assertIn("store-report.py setup", self.sr.lines(None)[0])

    def test_the_fetch_runs_before_the_briefing_and_cannot_break_it(self):
        unit = (ROOT / "deploy" / "fury-cycle.service").read_text()
        pre = [l for l in unit.splitlines() if l.startswith("ExecStartPre=")]
        self.assertTrue(any("store-report.py fetch" in l and l.startswith("ExecStartPre=-")
                            for l in pre), pre)

    def test_snapshots_are_not_tracked(self):
        r = subprocess.run(["git", "check-ignore", "-q", "agents/emily/state/store/2026-09-24.json"],
                           cwd=ROOT)
        self.assertEqual(r.returncode, 0)


class EveryListingGetsAPinAndABoard(unittest.TestCase):
    """store-report.py pins: a Pinterest pin for each listing, written from
    the listing's own title, description and tags, on a board named for a
    phrase shoppers search. Pinterest cuts titles at 100 characters and
    descriptions at 500; the cut happens here, where it can be seen."""

    def setUp(self):
        self.sr = load("store_report_pins", "store-report.py")

    def row(self, title, desc="", tags=()):
        return {"title": title, "description": desc, "tags": list(tags),
                "url": "https://www.etsy.com/listing/1"}

    def test_boards_follow_the_listings_words(self):
        cases = {
            "Read Local Library Tote Bag, Vintage Bookshop Typography": "Bookish Tote Bags & Library Gifts",
            "Navy Wave Print Tote Bag | Modern Seaside Pattern": "Coastal & Nautical Tote Bags",
            "Abstract Botanical Pattern Tote Bag | Colorful Leaf Print": "Vintage Botanical Tote Bags",
            "Town Landmark Pattern Tote": "Heritage Pattern & Map Tote Bags",
            "Plain Canvas Everyday Tote Bag": "Vintage-Inspired Tote Bags",
            "Crisp Air Hiking Sticker": "Vintage Stickers & Gifts",
            "Vintage Compass Logo Hoodie": "Vintage Stickers & Gifts",
        }
        for title, board in cases.items():
            self.assertEqual(self.sr.board_for(self.row(title))[0], board, title)

    def test_tags_count_toward_the_board(self):
        r = self.row("Everyday Pattern Tote Bag", tags=["beach tote"])
        self.assertEqual(self.sr.board_for(r)[0], "Coastal & Nautical Tote Bags")

    def test_the_pin_title_is_the_part_before_the_bar_and_fits(self):
        pin = self.sr.pin_for(self.row("Navy Wave Print Tote Bag | Modern Seaside Pattern"))
        self.assertEqual(pin["title"], "Navy Wave Print Tote Bag")
        self.assertLessEqual(len(self.sr.pin_for(self.row("x" * 300))["title"]), 100)

    def test_the_description_is_a_sentence_then_keywords_then_the_shop(self):
        pin = self.sr.pin_for(self.row("Navy Wave Print Tote Bag", "Navy waves on cream. Made to carry.",
                                       ["beach tote", "wave print tote bag", "coastal gift"]))
        self.assertTrue(pin["description"].startswith("Navy waves on cream."))
        self.assertIn("Beach tote, coastal gift.", pin["description"])
        self.assertNotIn("wave print tote bag", pin["description"].lower().split(".")[1],
                         "a tag the title already says is not repeated")
        self.assertTrue(pin["description"].endswith("VintageLoom Treasures on Etsy."))
        long = self.sr.pin_for(self.row("T", "y " * 400, ["t" * 19] * 13))
        self.assertLessEqual(len(long["description"]), 500)

    def test_no_description_still_reads_as_sentences(self):
        pin = self.sr.pin_for(self.row("Plain Canvas Tote Bag"))
        self.assertEqual(pin["description"], "Plain Canvas Tote Bag. VintageLoom Treasures on Etsy.")

    def test_the_snapshot_keeps_what_a_pin_needs(self):
        r = self.sr.snapshot_row({"listing_id": 1, "title": "x", "tags": ["a", " ", "b"],
                                  "description": "d" * 900})
        self.assertEqual(r["tags"], ["a", "b"])
        self.assertEqual(len(r["description"]), 600)


class TheBudgetIsTheMonthsRealBill(unittest.TestCase):
    """budget.py: OpenRouter's own count for the day against the owner's cap.

    The owner set the cap at $1 of AI a day (2026-09-25), after a $25-a-month
    all-in budget left nine cents a day for AI. Each test is a way that number
    could be read wrong and nobody notice until the card statement.
    """

    @classmethod
    def setUpClass(cls):
        cls.b = load("budget", "budget.py")

    SEPT = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)

    def test_the_cap_is_todays_spend_not_the_months(self):
        # $12 spent across the month is fine; $1.05 spent today is not.
        a = self.b.assess({"usage_daily": 0.40, "usage_monthly": 12.0}, 1.0, now=self.SEPT)
        self.assertEqual((a["state"], a["ai_left_today"]), ("ok", 0.6))
        a = self.b.assess({"usage_daily": 1.05, "usage_monthly": 1.05}, 1.0, now=self.SEPT)
        self.assertEqual(a["state"], "over")
        a = self.b.assess({"usage_daily": 1.0}, 1.0, now=self.SEPT)
        self.assertEqual(a["state"], "over", "exactly the cap is spent, not a cent left")

    def test_the_month_at_most_counts_every_day_and_the_droplet(self):
        a = self.b.assess({"usage_daily": 0.1}, 1.0, fixed={"droplet": 12.0}, now=self.SEPT)
        self.assertEqual(a["month_at_most"], 42.0)
        a = self.b.assess({"usage_daily": 0.1}, 1.0, fixed={"droplet": 12.0},
                          now=datetime(2026, 2, 14, tzinfo=timezone.utc))
        self.assertEqual(a["month_at_most"], 40.0, "February has 28 days")

    def test_a_missing_count_is_unknown_not_zero(self):
        # Reading a missing usage_daily as $0 would report a quiet day in the
        # middle of a runaway loop.
        for data in ({}, {"usage_daily": None}, {"usage_daily": "0.10"},
                     {"usage_daily": True}, {"usage_monthly": 3.0}):
            a = self.b.assess(data, 1.0, now=self.SEPT)
            self.assertEqual(a["state"], "unknown", data)
            self.assertIsNone(a["ai_left_today"])

    def test_only_a_daily_cap_counts_as_the_hard_stop(self):
        hs = self.b.hard_stop
        self.assertIn("none", hs(None, None, 1.0))
        self.assertIn("lifetime", hs(1.0, None, 1.0))
        self.assertIn("not a daily cap", hs(30.0, "monthly", 1.0))
        self.assertIn("not a daily cap", hs(7.0, "weekly", 1.0))
        self.assertIn("above", hs(2.0, "daily", 1.0))
        self.assertEqual(hs(1.0, "daily", 1.0), "$1.00 daily")

    def test_the_cap_is_read_from_limits_json(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "limits.json"
            f.write_text(json.dumps({"daily_ai_budget": 1}))
            self.assertEqual(self.b.daily_budget(f), 1.0)
            for bad in ({}, {"daily_ai_budget": 0}, {"daily_ai_budget": "lots"},
                        {"monthly_budget": 25}):
                f.write_text(json.dumps(bad))
                self.assertIsNone(self.b.daily_budget(f), bad)
        # And the tracked file carries the owner's number, so a fresh
        # checkout is not silently uncapped.
        self.assertEqual(self.b.daily_budget(ROOT / "tasks" / "limits.json"), 1.5)  # owner, 2026-10-04

    def run_main(self, data, argv, key="placeholder-test-token-7c1f"):
        """main() with OpenRouter replaced by `data`; returns (code, stdout)."""
        pf = self.b.preflight
        saved = (pf.openrouter_key, pf.key_status, self.b.daily_budget, sys.argv)
        pf.openrouter_key = lambda: key
        pf.key_status = lambda k: data if not isinstance(data, Exception) else (_ for _ in ()).throw(data)
        self.b.daily_budget = lambda path=None: 1.0
        sys.argv = ["budget.py"] + argv
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = self.b.main()
        finally:
            pf.openrouter_key, pf.key_status, self.b.daily_budget, sys.argv = saved
        return code, out.getvalue()

    def test_check_exits_over_only_when_the_allowance_is_spent(self):
        self.assertEqual(self.run_main({"usage_daily": 1.2, "usage_monthly": 3.0}, ["check"])[0],
                         self.b.OVER)
        self.assertEqual(self.run_main({"usage_daily": 0.3, "usage_monthly": 20.0}, ["check"])[0], 0)
        # A day whose count is missing is not a day that is over.
        self.assertEqual(self.run_main({"usage_monthly": 20.0}, ["check"])[0], 0)
        # Unreadable is its own answer: a network blip must not read as
        # "over" and silence the GM, nor as "fine".
        code, out = self.run_main(OSError("offline"), ["check", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["state"], "unreadable")

    def test_the_key_never_reaches_the_output(self):
        key = "placeholder-test-token-7c1f"
        for argv in (["--json"], [], ["check"]):
            _, out = self.run_main({"usage_daily": 0.2, "limit": 1, "limit_reset": "daily",
                                    "label": "ecosystem"}, argv, key=key)
            self.assertNotIn(key, out, argv)
            self.assertNotIn("7c1f", out, argv)

    def test_preflight_and_budget_share_one_key_reader(self):
        src = (SCRIPTS / "budget.py").read_text()
        self.assertIn("preflight.key_status(", src)
        self.assertNotIn("openrouter.ai/api/v1/key", src)


class KnowledgeIsProposedAndOnlyTheOwnerMakesItTrue(unittest.TestCase):
    """knowledge.py: the shared store every agent is briefed from.

    The propose-only month in code: anyone may add, what they add waits, and
    agents are briefed from accepted entries alone.
    """

    @classmethod
    def setUpClass(cls):
        cls.k = load("knowledge", "knowledge.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = self.k.connect(Path(self.tmp.name) / "k.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def add(self, **kw):
        base = dict(dept="etsy", kind="lesson", title="t", body="b",
                    evidence="", confidence="observed", source="scout")
        base.update(kw)
        return self.k.add(self.con, **base)

    def test_what_an_agent_adds_is_not_what_an_agent_is_told(self):
        i = self.add(title="Proposed only")
        self.assertEqual(self.k.get(self.con, i)["status"], "proposed")
        self.assertNotIn("Proposed only", self.k.brief(self.con, "etsy"))
        self.k.accept(self.con, i)
        self.assertIn("Proposed only", self.k.brief(self.con, "etsy"))

    def test_measured_needs_evidence(self):
        with self.assertRaises(self.k.Refused):
            self.add(confidence="measured")
        self.add(confidence="measured", evidence="knockout.py --check")

    def test_confidence_is_a_word_not_a_percentage(self):
        for bad in ("87%", "high", ""):
            with self.assertRaises(self.k.Refused, msg=bad):
                self.add(confidence=bad, title=f"c {bad}")

    def test_unknown_departments_and_kinds_are_refused(self):
        with self.assertRaises(self.k.Refused):
            self.add(dept="shoes")
        with self.assertRaises(self.k.Refused):
            self.add(kind="vibe")

    def test_entries_stay_short(self):
        with self.assertRaises(self.k.Refused):
            self.add(title="x" * (self.k.TITLE_MAX + 1))
        with self.assertRaises(self.k.Refused):
            self.add(body="x" * (self.k.BODY_MAX + 1))
        self.add(title="x" * self.k.TITLE_MAX, body="x" * self.k.BODY_MAX)

    def test_one_open_entry_per_title_per_department(self):
        i = self.add(title="Totes sell in autumn")
        with self.assertRaises(self.k.Refused):
            self.add(title="  totes SELL in   autumn ")
        self.add(title="Totes sell in autumn", dept="trading")
        self.k.retire(self.con, i, "wrong")
        self.add(title="Totes sell in autumn")

    def test_retiring_needs_a_reason_and_keeps_the_entry(self):
        i = self.add()
        with self.assertRaises(self.k.Refused):
            self.k.retire(self.con, i, "   ")
        self.k.retire(self.con, i, "superseded by #9")
        e = self.k.get(self.con, i)
        self.assertEqual((e["status"], e["why_retired"]), ("retired", "superseded by #9"))

    def test_review_dates_come_due_and_renew(self):
        t0 = datetime(2026, 9, 25, tzinfo=timezone.utc)
        i = self.add(kind="fact")
        self.k.accept(self.con, i, now=t0)
        self.assertEqual(self.k.due(self.con, now=t0 + timedelta(days=29)), [])
        self.assertEqual([e["id"] for e in self.k.due(self.con, now=t0 + timedelta(days=31))], [i])
        self.assertIn("past review", self.k.brief(self.con, "etsy", now=t0 + timedelta(days=31)))
        self.k.renew(self.con, i, now=t0 + timedelta(days=31))
        self.assertEqual(self.k.due(self.con, now=t0 + timedelta(days=45)), [])
        with self.assertRaises(self.k.Refused):
            self.k.renew(self.con, self.add(title="still proposed"))

    def test_accepting_twice_is_refused(self):
        i = self.add()
        self.k.accept(self.con, i)
        with self.assertRaises(self.k.Refused):
            self.k.accept(self.con, i)

    def test_a_brief_is_its_department_plus_all_decisions_first(self):
        ids = [self.add(title="etsy lesson"),
               self.add(title="shared rule", dept="all", kind="decision"),
               self.add(title="trading only", dept="trading")]
        for i in ids:
            self.k.accept(self.con, i)
        text = self.k.brief(self.con, "etsy")
        self.assertNotIn("trading only", text)
        self.assertLess(text.index("shared rule"), text.index("etsy lesson"))

    def test_the_gms_brief_is_every_department_labelled(self):
        for dept in ("etsy", "trading", "all"):
            self.k.accept(self.con, self.add(title=f"{dept} entry", dept=dept))
        text = self.k.brief(self.con, self.k.EVERY)
        for dept in ("etsy", "trading", "all"):
            self.assertIn(f"[{dept} lesson, observed] {dept} entry", text)
        self.assertNotIn("trading entry", self.k.brief(self.con, "etsy"))

    def test_a_brief_is_capped_and_says_what_it_left_out(self):
        for n in range(12):
            self.k.accept(self.con, self.add(title=f"entry {n}", body="x" * 400))
        text = self.k.brief(self.con, "etsy", max_chars=1500)
        self.assertLessEqual(len(text.rsplit("\n", 1)[0]), 1500)
        self.assertIn("did not fit", text)

    def test_seeding_twice_adds_nothing_and_every_seed_is_proposed(self):
        added, _ = self.k.seed(self.con)
        self.assertEqual(added, len(self.k.SEEDS))
        self.assertEqual(self.k.seed(self.con), (0, len(self.k.SEEDS)))
        self.assertEqual({e["status"] for e in self.k.entries(self.con)}, {"proposed"})

    def test_every_working_agent_has_a_department(self):
        # An agent with no department is briefed from nothing but `all`.
        placed = {a for members in self.k.DEPARTMENTS.values() for a in members}
        for d in sorted((ROOT / "agents").iterdir()):
            if not (d / f"_{d.name}-agents-header.md").is_file() or (d / "RETIRED").exists():
                continue
            self.assertIn(d.name, placed, f"{d.name} is in no department")

    def test_the_cli_exits_2_with_the_reason(self):
        env = dict(os.environ, KNOWLEDGE_DB=str(Path(self.tmp.name) / "cli.db"))
        r = subprocess.run([sys.executable, str(SCRIPTS / "knowledge.py"), "add",
                            "--dept", "etsy", "--kind", "lesson", "--title", "x",
                            "--body", "y", "--confidence", "measured", "--source", "scout"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("measured needs --evidence", r.stderr)


class TheTownHallRunsTheScriptsItShows(unittest.TestCase):
    """townhall.js: the Deck's budget and knowledge routes.

    Driven through the real module with stub scripts, so nothing reaches
    OpenRouter and the store's rules are the stub's answer, not the page's.
    """

    API = ROOT / "mission-control-api"
    NODE = shutil.which("node")
    DEPS = (API / "node_modules" / "better-sqlite3").is_dir()

    KSTUB = (
        "import json, sys\n"
        "open(sys.argv[0] + '.calls', 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        "    print(json.dumps({'entries': [{'id': 1, 'status': 'proposed'}], 'due': []}))\n"
        "elif sys.argv[2] == '7':\n"
        "    print('refused: #7 is accepted, not proposed', file=sys.stderr); sys.exit(2)\n"
        "else:\n"
        "    print(sys.argv[1] + 'ed #' + sys.argv[2])\n"
    )
    BSTUB = "import json\nprint(json.dumps({'state': 'unreadable', 'error': 'no key'}))\nraise SystemExit(1)\n"

    def setUp(self):
        if not (self.NODE and self.DEPS):
            self.skipTest("node or the Deck's node_modules are not installed")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "scripts").mkdir()
        (root / "scripts" / "knowledge.py").write_text(self.KSTUB)
        (root / "scripts" / "budget.py").write_text(self.BSTUB)
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def calls(self):
        f = self.root / "scripts" / "knowledge.py.calls"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def drive(self, steps):
        js = (
            f"const hall = require({json.dumps(str(self.API / 'townhall.js'))});"
            "const routes = {};"
            "const app = {get:(p,f)=>routes['GET '+p]=f, post:(p,f)=>routes['POST '+p]=f};"
            "hall.register(app);"
            "function call(m, p, params, body){ return new Promise(ok => {"
            "  const res = {code:200, status(c){this.code=c;return this;},"
            "               json(b){ok({status:this.code, body:JSON.parse(JSON.stringify(b))});}};"
            "  Promise.resolve(routes[m+' '+p]({params:params||{}, body:body||{}}, res)); }); }"
            "(async () => { const out = {};" + steps +
            " process.stdout.write(JSON.stringify(out)); })();"
        )
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True,
                           env=env, cwd=str(self.API), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_an_unreadable_budget_still_arrives_as_json(self):
        # budget.py exits 1 when OpenRouter is unreachable; the page must
        # still get its answer to say so, not a blank panel.
        out = self.drive("out.t = await call('GET', '/api/townhall');")
        self.assertEqual(out["t"]["body"]["budget"], {"state": "unreadable", "error": "no key"},
                         "the script's own reason, not a fallback's")
        self.assertEqual(out["t"]["body"]["knowledge"]["entries"][0]["id"], 1)

    def test_a_refusal_comes_back_with_the_scripts_reason(self):
        out = self.drive("out.r = await call('POST', '/api/knowledge/:id/accept', {id: '7'});")
        self.assertEqual(out["r"]["status"], 400)
        self.assertEqual(out["r"]["body"]["error"], "#7 is accepted, not proposed")

    def test_ids_are_digits_and_retiring_needs_a_reason(self):
        out = self.drive(
            "out.a = await call('POST', '/api/knowledge/:id/accept', {id: '1; rm -rf /'});"
            "out.b = await call('POST', '/api/knowledge/:id/retire', {id: '3'}, {why: '  '});"
            "out.c = await call('POST', '/api/knowledge/:id/retire', {id: '3'}, {why: 'wrong'});")
        self.assertEqual(out["a"]["status"], 400)
        self.assertEqual(out["b"]["status"], 400)
        self.assertEqual(out["c"]["status"], 200)
        self.assertEqual(self.calls(), [["retire", "3", "--why", "wrong"]],
                         "only the valid request may reach the script")

    def test_the_hall_panel_is_not_rebuilt_by_the_pull(self):
        src = (self.API / "public" / "village.html").read_text()
        pull = src.split("async function pull(", 1)[1].split("\nfunction ", 1)[0]
        for rebuild in ("openPanel(", "openHallPanel(", "drawHall(", "loadHall("):
            self.assertNotIn(rebuild, pull)


class GMHarness:
    """The GM with every outside part replaced: budget, briefing, ideas,
    model call and key. Shared by the GM test classes."""

    @classmethod
    def setUpClass(cls):
        cls.gm = load("gm", "gm.py")
        cls.fury = load("fury_collect", "fury-collect.py")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.days = t / "days"
        self.brief_text = None
        self.ideas = []
        self.con = self.gm.knowledge.connect(t / "k.db")
        self.now = datetime(2026, 9, 26, 13, 50, tzinfo=timezone.utc)   # 08:50 Central

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    OK_BUDGET = {"state": "ok", "cap": 1.0, "ai_today": 0.49, "ai_left_today": 0.51,
                 "ai_month": 12.49, "hard_stop": "$1.00 daily"}

    FACTS = ["Needs you: 3 tasks pending in the queue",
             "The money: Belfort $10,095.12 (+0.95%)",
             "Budget: $0.49 of AI spent today of the $1.00 daily cap, $0.51 left."]

    def prop(self, **kw):
        p = {"title": "Clear the queue", "dept": "ops", "who": "owner",
             "action": "Look at the pending tasks", "why": "3 tasks are pending", "facts": ["F1"]}
        p.update(kw)
        return p

    REPORT = {"generated_utc": "2026-09-26 13:31:02",
              "agents": {"emily": {"last_memory_line": "- built 3 totes; library tote failed"}},
              "failures": [], "queue": {"pending": 3}}

    def briefing(self):
        """A briefing written by Fury's own code, so the parser is tested
        against the format Fury really writes. Scout's count is pinned so the
        checkout's own ideas.json cannot change the facts."""
        with unittest.mock.patch.object(self.fury.scout, "pending_count", return_value=0):
            self.brief_text = self.fury.build_briefing(self.REPORT, "2026-09-26")
        return self.brief_text

    def run_gm(self, reply=None, budget=None, call=None, **kw):
        calls = []

        def fake(msgs, key, slug):
            calls.append(msgs)
            if isinstance(reply, Exception):
                raise reply
            return (reply if isinstance(reply, str) else json.dumps(reply or {})), \
                {"cost": 0.0123, "prompt_tokens": 900, "completion_tokens": 400}
        kw.setdefault("ideas", lambda: list(self.ideas))
        code, doc = self.gm.run(now=kw.pop("now", self.now), days=self.days, con=self.con,
                                read_budget=(budget if callable(budget) else (lambda: budget or self.OK_BUDGET)),
                                call=call or fake, key="placeholder",
                                briefing=kw.pop("briefing", lambda now: self.brief_text or ""), **kw)
        return code, doc, calls


class TheGMProposesOnlyWhatTheFactsSupport(GMHarness, unittest.TestCase):
    """gm.py: one model call a morning, checked by code before anyone sees it.

    Propose-only for the first month (the owner's decision, 2026-09-25). The
    GM reads facts code compiled, and code refuses any proposal that leans on
    a fact it was not given or a number that is in none of the facts it cites.
    """

    # --- the checks on a reply -------------------------------------------

    def test_a_proposal_must_cite_facts_it_was_given(self):
        ok, why = self.gm.check_proposal(self.prop(), self.FACTS)
        self.assertIsNotNone(ok, why)
        for cited in ([], ["F4"], ["F0"], ["F1", "F9"], ["1"], "F1"):
            ok, why = self.gm.check_proposal(self.prop(facts=cited), self.FACTS)
            self.assertIsNone(ok, cited)

    def test_a_number_in_the_why_must_be_in_the_cited_facts(self):
        # 4 is in no fact: the model counted, or invented.
        ok, why = self.gm.check_proposal(self.prop(why="4 tasks are pending"), self.FACTS)
        self.assertIsNone(ok)
        self.assertIn("4", why)
        # 0.95 is a real fact, but not one this proposal cites.
        ok, _ = self.gm.check_proposal(self.prop(why="Belfort is up 0.95%"), self.FACTS)
        self.assertIsNone(ok)
        ok, _ = self.gm.check_proposal(self.prop(why="Belfort is up 0.95%", facts=["F2"]), self.FACTS)
        self.assertIsNotNone(ok)

    def test_numbers_are_compared_as_values_and_references_are_not_claims(self):
        n = self.gm.numbers
        self.assertEqual(n("$1 cap"), n("$1.00 cap"))
        self.assertEqual(n("$10,095.12"), {10095.12})
        self.assertEqual(n("per F3 and knowledge #9"), set())
        ok, _ = self.gm.check_proposal(
            self.prop(why="Only $0.51 of the $1 cap is left (F3, see #9)", facts=["F3"]), self.FACTS)
        self.assertIsNotNone(ok)

    def test_departments_and_people_must_be_real(self):
        self.assertIsNone(self.gm.check_proposal(self.prop(dept="marketing"), self.FACTS)[0])
        ok, why = self.gm.check_proposal(self.prop(who="timmy"), self.FACTS)
        self.assertIsNone(ok)
        self.assertIn("unknown person", why, "a retired agent is not on the team at all")
        etsy = ["Needs you: 3 Scout ideas awaiting your review"]
        self.assertIsNotNone(self.gm.check_proposal(
            self.prop(who="Scout", dept="etsy", task="run_scout"), etsy)[0])

    def test_at_most_five_proposals_and_the_rest_are_dropped_with_a_reason(self):
        doc = {"summary": "s", "proposals": [self.prop(title=f"p{i}") for i in range(7)]}
        _, kept, dropped, _ = self.gm.check_reply(doc, self.FACTS)
        self.assertEqual([p["n"] for p in kept], [1, 2, 3, 4, 5])
        self.assertEqual(len(dropped), 2)
        self.assertIn("limit", dropped[0]["reason"])

    def test_a_dropped_proposal_does_not_use_up_a_place(self):
        doc = {"proposals": [self.prop(facts=[])] + [self.prop(title=f"p{i}") for i in range(5)]}
        _, kept, dropped, _ = self.gm.check_reply(doc, self.FACTS)
        self.assertEqual([p["n"] for p in kept], [1, 2, 3, 4, 5],
                         "numbered as the owner sees them, not by the model's order")
        self.assertEqual(dropped[0]["reason"], "cites no facts")

    def test_a_fenced_reply_is_read_and_prose_is_refused(self):
        self.assertEqual(self.gm.parse_reply('```json\n{"summary": "x"}\n```'), {"summary": "x"})
        for bad in ("", "I think you should...", "[1, 2]"):
            with self.assertRaises(ValueError):
                self.gm.parse_reply(bad)

    # --- the morning -----------------------------------------------------

    def test_fury_briefing_becomes_facts_with_their_sections(self):
        facts = self.gm.briefing_facts(self.briefing())
        self.assertIn("Needs you: 3 tasks pending in the queue", facts)
        self.assertTrue(any(f.startswith("What happened: Emily") for f in facts), facts)
        self.assertFalse(any("**" in f for f in facts))

    def test_the_facts_are_read_when_the_gm_runs_not_at_fury_time(self):
        # 2026-09-25: Fury's 08:30 briefing said 3 Scout ideas were waiting.
        # The owner approved them in the Command Center; the GM ran at 11:00,
        # read the 08:30 file and told the owner to review them anyway.
        fury, pending = self.fury, {"n": 3}
        with unittest.mock.patch.object(self.gm.fury, "collect", return_value=dict(self.REPORT)), \
             unittest.mock.patch.object(self.gm.fury.scout, "pending_count",
                                        side_effect=lambda: pending["n"]):
            at_fury_time = self.gm.briefing_facts(self.gm.live_briefing(self.now))
            self.assertIn("Needs you: 3 Scout ideas awaiting your review", at_fury_time)
            pending["n"] = 0                         # approved before the GM ran
            _, doc, _ = self.run_gm({"summary": "s"}, briefing=None)
        self.assertFalse(any("Scout idea" in f for f in doc["facts"]), doc["facts"])

    def test_it_stays_asleep_when_the_budget_says_so(self):
        def boom(*a):
            raise AssertionError("the model must not be called")
        for b in ({"state": "over", "ai_left_today": 0.0},
                  # budget.py's verdict is the word that counts, whatever
                  # else the numbers seem to say.
                  dict(self.OK_BUDGET, state="over"),
                  {"state": "unknown", "ai_left_today": None},
                  dict(self.OK_BUDGET, ai_left_today=0.05)):
            code, doc, _ = self.run_gm(budget=b, call=boom, force=True)
            self.assertEqual((code, doc["status"]), (0, "skipped"), b)
        def unreadable():
            raise OSError("offline")
        code, doc, _ = self.run_gm(budget=unreadable, call=boom, force=True)
        self.assertEqual(doc["status"], "skipped")
        self.assertIn("unknown budget", doc["reason"])
        self.assertEqual(self.gm.read_day(self.gm.day_path("2026-09-26", self.days))["status"],
                         "skipped", "a skipped morning is written down, not silent")

    def test_one_call_a_day_unless_forced(self):
        self.briefing()
        reply = {"summary": "quiet", "proposals": [self.prop()]}
        _, doc, calls = self.run_gm(reply)
        self.assertEqual((doc["status"], len(calls)), ("ran", 1))
        _, _, calls = self.run_gm(reply)
        self.assertEqual(len(calls), 0, "a second run the same day must not pay again")
        _, _, calls = self.run_gm(reply, force=True)
        self.assertEqual(len(calls), 1)

    def test_the_day_is_eastern(self):
        # 01:30 UTC on the 27th is still the evening of the 26th in Eastern.
        _, doc, _ = self.run_gm({"summary": "x"}, now=datetime(2026, 9, 27, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(doc["day"], "2026-09-26")

    def test_a_failed_call_or_a_bad_reply_fails_loudly(self):
        code, doc, _ = self.run_gm(OSError("timed out"))
        self.assertEqual((code, doc["status"]), (1, "failed"))
        code, doc, _ = self.run_gm("Here are my thoughts...", force=True)
        self.assertEqual((code, doc["status"]), (1, "failed"))
        self.assertEqual(doc["reply_head"], "Here are my thoughts...")

    def test_what_it_ran_on_and_what_it_cost_are_kept(self):
        self.briefing()
        _, doc, calls = self.run_gm({"summary": "s", "proposals": [self.prop()]})
        self.assertEqual(doc["cost_usd"], 0.0123)
        self.assertEqual(doc["proposals"][0]["facts"], ["F1"])
        user = calls[0][1]["content"]
        for i, f in enumerate(doc["facts"], 1):
            self.assertIn(f"F{i} {f}", user)

    def test_the_cap_in_the_prompt_is_the_budgets_cap(self):
        _, _, calls = self.run_gm({"summary": "s"}, budget=dict(self.OK_BUDGET, cap=2.5))
        self.assertIn("$2.50 a day", calls[0][0]["content"])

    def test_it_proposes_no_knowledge_for_now(self):
        # Its first real morning suggested three "facts" that were stale by
        # the next day. Until it reads more than one day, it files nothing.
        self.assertEqual(self.gm.MAX_KNOWLEDGE, 0)
        _, doc, calls = self.run_gm({"summary": "s", "knowledge": [
            {"dept": "etsy", "kind": "fact", "title": "Three scout ideas are pending review",
             "body": "b", "confidence": "observed"}]})
        self.assertNotIn('"knowledge"', calls[0][0]["content"], "it is not asked for any")
        self.assertEqual(doc["knowledge"][0]["result"],
                         "not filed: the GM does not propose knowledge yet")
        self.assertEqual(self.gm.knowledge.entries(self.con), [])

    def test_text_cut_at_the_limit_says_it_was_cut(self):
        long = "word " * 120
        cut = self.gm._text(long, self.gm.TEXT_MAX)
        self.assertEqual(len(cut), self.gm.TEXT_MAX)
        self.assertTrue(cut.endswith("\u2026"))
        self.assertEqual(self.gm._text("short  text", self.gm.TEXT_MAX), "short text")

    def test_when_knowledge_is_turned_on_it_is_proposed_never_accepted(self):
        saved = self.gm.MAX_KNOWLEDGE
        self.gm.MAX_KNOWLEDGE = 3
        self.addCleanup(setattr, self.gm, "MAX_KNOWLEDGE", saved)
        ks = [{"dept": "etsy", "kind": "lesson", "title": "Library totes time out",
               "body": "b", "confidence": "observed"},
              {"dept": "etsy", "kind": "lesson", "title": "Measured, no evidence",
               "body": "b", "confidence": "measured"},
              {"dept": "shoes", "kind": "lesson", "title": "x", "body": "b"},
              {"dept": "etsy", "kind": "fact", "title": "fourth", "body": "b"}]
        _, doc, _ = self.run_gm({"summary": "s", "knowledge": ks})
        results = [k["result"] for k in doc["knowledge"]]
        self.assertTrue(results[0].startswith("proposed #"))
        self.assertTrue(results[1].startswith("refused:"))
        self.assertTrue(results[2].startswith("refused:"))
        self.assertIn("limit", results[3])
        rows = self.gm.knowledge.entries(self.con)
        self.assertEqual([(r["status"], r["source"]) for r in rows], [("proposed", "gm")])

    def test_the_owners_decisions_are_tomorrows_facts(self):
        self.briefing()
        _, doc, _ = self.run_gm({"summary": "s", "proposals": [self.prop(), self.prop(title="Other")]})
        self.gm.decide(doc["day"], 1, "decline", "not while totes-only", days=self.days)
        with self.assertRaises(self.gm.knowledge.Refused):
            self.gm.decide(doc["day"], 9, "approve", days=self.days)
        with self.assertRaises(self.gm.knowledge.Refused):
            self.gm.decide(doc["day"], 1, "maybe", days=self.days)
        tomorrow = self.now + timedelta(days=1)
        _, _, calls = self.run_gm({"summary": "s"}, now=tomorrow)
        user = calls[0][1]["content"]
        self.assertIn('declined "Clear the queue". Note: not while totes-only', user)
        self.assertIn('has not decided on "Other"', user)

    def test_the_model_is_one_openrouter_really_lists(self):
        listed = {m["id"] for m in json.loads((FIXTURES / "openrouter-models.json").read_text())["data"]}
        self.assertIn(self.gm.DEFAULT_MODEL, listed)

    def test_the_timer_is_not_mistaken_for_an_agent(self):
        # *-cycle.timer is how cost-estimate and health-check find agents.
        self.assertTrue((ROOT / "deploy" / "gm-brief.timer").is_file())
        self.assertFalse(list((ROOT / "deploy").glob("gm-cycle.*")))
        svc = (ROOT / "deploy" / "gm-brief.service").read_text()
        self.assertIn("scripts/gm.py run", svc)


class TheTownHallRecordsTheOwnersAnswer(TheTownHallRunsTheScriptsItShows):
    """townhall.js's GM route: validated, then handed to gm.py decide."""

    GSTUB = (
        "import json, sys\n"
        "open(sys.argv[0] + '.calls', 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'status': 'ran', 'day': '2026-09-26'}) if sys.argv[1] == 'show' else 'ok')\n"
    )

    def setUp(self):
        super().setUp()
        (self.root / "scripts" / "gm.py").write_text(self.GSTUB)

    def gm_calls(self):
        f = self.root / "scripts" / "gm.py.calls"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_the_hall_carries_the_gms_morning(self):
        out = self.drive("out.t = await call('GET', '/api/townhall');")
        self.assertEqual(out["t"]["body"]["gm"]["status"], "ran")

    def test_only_a_well_formed_answer_reaches_gm_py(self):
        out = self.drive(
            "out.a = await call('POST', '/api/gm/:day/:n/:verdict', {day: '2026-09-26; ls', n: '1', verdict: 'approve'});"
            "out.b = await call('POST', '/api/gm/:day/:n/:verdict', {day: '2026-09-26', n: 'x', verdict: 'approve'});"
            "out.c = await call('POST', '/api/gm/:day/:n/:verdict', {day: '2026-09-26', n: '1', verdict: 'assign'});"
            "out.d = await call('POST', '/api/gm/:day/:n/:verdict', {day: '2026-09-26', n: '2', verdict: 'decline'}, {note: 'not now'});")
        for k in "abc":
            self.assertEqual(out[k]["status"], 400, k)
        self.assertEqual(out["d"]["status"], 200)
        self.assertEqual([c for c in self.gm_calls() if c[0] == "decide"],
                         [["decide", "2026-09-26", "2", "decline", "--note", "not now"]])


class OneRuleForAPendingIdea(unittest.TestCase):
    """scout-ideas.py owns "pending"; Fury imports it.

    Fury's copy read an empty status as decided (`get("status", "pending")`
    only defaults a missing key), while Scout and the Deck read it as waiting.
    """

    def test_an_empty_or_missing_status_is_pending_everywhere(self):
        scout = load("scout_ideas_rule", "scout-ideas.py")
        fury = load("fury_rule", "fury-collect.py")
        for idea, want in (({}, True), ({"status": ""}, True), ({"status": None}, True),
                           ({"status": "pending"}, True), ({"status": "approved"}, False),
                           ({"status": "rejected"}, False)):
            self.assertEqual(scout.is_pending(idea), want, idea)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "state").mkdir()
            (Path(d) / "state" / "ideas.json").write_text(json.dumps(
                {"ideas": [{"status": ""}, {}, {"status": "approved"}]}))
            self.assertEqual(fury.state_summary(d)["ideas_pending"], 2)
        src = (SCRIPTS / "fury-collect.py").read_text()
        self.assertNotIn('"pending") == "pending"', src, "Fury restates the rule again")


class ApprovedProposalsAreSentToTheRightAgent(GMHarness, unittest.TestCase):
    """An agent proposal names one of that agent's real jobs, and approving it
    sends that job - checked again at the moment of sending.

    2026-09-25: the GM proposed "Scout: edit the listing's tags" and "Emily:
    run layout --folded" - jobs neither agent can be handed. The owner asked
    for approvals to go to the agent instead of to them, so the list of what
    can be sent became code, and the model may only pick from it.
    """

    IDEA = {"id": 7, "title": "Botanical library tote", "product": "tote", "status": "pending"}

    # F2 is about Emily, so about Etsy - the same place it sits in the
    # briefing the harness builds with Fury's own code.
    FACTS = ["Needs you: 3 tasks pending in the queue",
             "What happened: Emily — - built 3 totes; library tote failed",
             "Budget: $0.49 of AI spent today of the $1.00 daily cap, $0.51 left."]

    def setUp(self):
        super().setUp()
        self.ideas = [dict(self.IDEA)]
        self.sent = []

    def runner(self, code=0, out="queued task #41 for emily -> builds/botanical-library-tote", err=""):
        def run(cmd):
            self.sent.append(cmd)
            return code, out, err
        return run

    def morning(self, *proposals):
        self.briefing()
        _, doc, _ = self.run_gm({"summary": "s", "proposals": list(proposals)})
        self.assertEqual(doc["dropped"], [], "the test's own proposals must stand")
        return doc

    def build(self, **kw):
        return self.prop(**dict({"title": "Build the library tote", "dept": "etsy", "who": "emily",
                                 "task": "build_idea", "idea": "#7",
                                 "why": "It is waiting for review", "facts": ["F2"]}, **kw))

    def decide(self, doc, n, verdict="approve", budget=None, code=0, err="", **kw):
        return self.gm.decide(doc["day"], n, verdict, "go", days=self.days,
                              read_budget=lambda: budget or self.OK_BUDGET,
                              runner=self.runner(code=code, err=err),
                              ideas=lambda: list(self.ideas), **kw)

    # --- what may be proposed -------------------------------------------

    def test_only_an_agents_own_jobs_can_be_proposed_for_it(self):
        waiting = {7}
        cases = [
            (self.build(), None),
            (self.build(idea=7), None),
            (self.prop(who="scout", dept="etsy", task="run_scout", facts=["F2"]), None),
            (self.build(task="run_scout"), "emily can only be sent build_idea"),
            (self.build(task=None), "names no task for emily"),
            (self.build(task="edit_listing"), "emily can only be sent build_idea"),
            (self.build(idea=8), "not a Scout idea waiting"),
            (self.build(idea=None), "not a Scout idea waiting"),
            (self.prop(who="belfort", dept="trading"), "nothing can be sent to belfort"),
            (self.prop(who="owner", task="build_idea", idea=7), "the owner is not sent tasks"),
        ]
        for p, reason in cases:
            ok, why = self.gm.check_proposal(p, self.FACTS, waiting)
            if reason is None:
                self.assertIsNotNone(ok, (p, why))
            else:
                self.assertIsNone(ok, p)
                self.assertIn(reason, why, p)
        ok, _ = self.gm.check_proposal(self.build(), self.FACTS, waiting)
        self.assertEqual((ok["task"], ok["idea"]), ("build_idea", 7))

    def test_an_agent_proposal_must_rest_on_its_own_department(self):
        # 2026-09-25, from the Town Hall: "Scout: run the market search now",
        # because Ace's night cycle found no candidates (F1) and no knowledge
        # was waiting (F20). Every check passed; neither fact is about Etsy.
        facts = ["What happened: Ace — - Passed on the 2026-09-24 night cycle because "
                 "candidates.json was empty; no fresh injuries or market-moving events.",
                 "Knowledge: 0 entries proposed and waiting for the owner.",
                 "The store: 8 listing(s), 13 views, 0 favourites"]
        seen = dict(title="Scout: Run market search now for new tote ideas", dept="etsy",
                    who="scout", task="run_scout", action="Run Scout's market search now.",
                    why="Night cycle left candidates empty; there are currently 0 proposed "
                        "knowledge entries waiting.", facts=["F1", "F2"])
        ok, why = self.gm.check_proposal(seen, facts)
        self.assertIsNone(ok)
        self.assertEqual(why, "cites nothing about etsy (F1 is about betting; "
                              "F2 is about no department)")
        ok, _ = self.gm.check_proposal(dict(seen, facts=["F3"], why="13 views, 0 favourites"), facts)
        self.assertIsNotNone(ok, "the store's numbers are Etsy's")

    def test_an_agent_works_in_its_own_department(self):
        ok, why = self.gm.check_proposal(self.build(dept="ops"), self.FACTS, {7})
        self.assertIsNone(ok)
        self.assertEqual(why, "emily works in etsy, not ops")

    def test_owner_proposals_may_rest_on_anything(self):
        ok, _ = self.gm.check_proposal(self.prop(dept="etsy", facts=["F1"]), self.FACTS)
        self.assertIsNotNone(ok)

    def test_facts_are_placed_by_whole_names_and_by_the_briefings_own_sections(self):
        fd = self.gm.fact_departments
        self.assertEqual(fd("What happened: Emily — built a tote"), {"etsy"})
        self.assertEqual(fd("The money: Belfort $10,095.12 (+0.95%)"), {"trading"})
        self.assertEqual(fd("Needs you: `scout-cycle.service` failed"), {"etsy"})
        self.assertEqual(fd("Budget: $0.49 of AI spent today"), set())
        self.assertEqual(fd("Aces high and emilyish words"), set(), "whole words only")
        self.assertEqual(fd("Scout idea #7 is waiting for review: Wave tote (tote)."), {"etsy"})
        # The store marker is the heading Fury really writes, not a guess at it.
        store = [f for f in self.gm.briefing_facts(self.briefing()) if f.startswith("The store:")]
        self.assertTrue(store, "Fury's briefing has no store section to recognise")
        self.assertEqual(fd(store[0]), {"etsy"})

    def test_the_team_the_model_reads_is_the_list_code_checks(self):
        team = self.gm.roster()
        blocks, current = {}, None
        for line in team.splitlines():
            m = re.match(r"  (\w+) \(", line)
            if m:
                current = m.group(1)
            elif current:
                blocks.setdefault(current, []).append(line)
        for task, spec in self.gm.TASKS.items():
            self.assertTrue(any(f"can be sent: {task}" in l for l in blocks[spec["who"]]), task)
            for other, lines in blocks.items():
                if other != spec["who"]:
                    self.assertFalse(any(f"can be sent: {task}" in l for l in lines), (task, other))
        for members in self.gm.knowledge.DEPARTMENTS.values():
            for agent in members:
                self.assertIn(agent, self.gm.ROLES, f"{agent} has no role line")
                if not self.gm.tasks_for(agent):
                    self.assertIn(f"nothing can be sent to {agent}", team)

    def test_waiting_ideas_are_facts_with_their_numbers(self):
        doc = self.morning(self.build())
        self.assertIn("Scout idea #7 is waiting for review: Botanical library tote (tote).",
                      doc["facts"])
        self.assertEqual(doc["proposals"][0]["idea"], 7)

    def test_a_build_is_booked_at_emilys_own_estimate(self):
        self.assertEqual(self.gm.TASKS["build_idea"]["min_left"], self.gm.new_build.COST_ESTIMATE)
        src = (SCRIPTS / "emily-new-build.py").read_text()
        self.assertIn('"--cost-estimate", type=float, default=COST_ESTIMATE', src)

    # --- sending ----------------------------------------------------------

    def test_approving_sends_the_one_command_for_that_job(self):
        doc = self.morning(self.build(title="Build it; rm -rf / `x`"))
        p = self.decide(doc, 1)
        self.assertEqual(p["sent"]["status"], "sent")
        self.assertEqual(len(self.sent), 1)
        cmd = self.sent[0]
        self.assertEqual(cmd[1:4], [str(SCRIPTS / "scout-review.py"), "approve", "7"])
        self.assertEqual(cmd[4:], ["--reason", f"approved from the GM's proposal {doc['day']} #1"])
        self.assertFalse(any("rm -rf" in c for c in cmd), "model text never reaches a command")

    def test_scout_is_started_the_way_its_timer_starts_it(self):
        doc = self.morning(self.prop(who="scout", dept="etsy", task="run_scout", facts=["F2"]))
        self.decide(doc, 1)
        self.assertEqual(self.sent, [["systemctl", "start", "--no-block", "scout-cycle.service"]])

    def test_nothing_is_sent_without_room_in_todays_budget(self):
        doc = self.morning(self.build())
        short = dict(self.OK_BUDGET, ai_left_today=self.gm.new_build.COST_ESTIMATE - 0.01)
        p = self.decide(doc, 1, budget=short)
        self.assertEqual(p["sent"]["status"], "not sent")
        self.assertIn("7pm Central", p["sent"]["detail"])
        self.assertEqual(self.sent, [])
        self.assertEqual(p["decision"]["verdict"], "approve", "the approval itself stands")

    def test_an_idea_decided_since_the_morning_is_not_built(self):
        doc = self.morning(self.build())
        self.ideas = []                          # approved in Scout's house meanwhile
        p = self.decide(doc, 1)
        self.assertEqual(p["sent"]["status"], "not sent")
        self.assertIn("no longer waiting", p["sent"]["detail"])
        self.assertEqual(self.sent, [])

    def test_a_failed_send_says_why_and_can_be_tried_again(self):
        doc = self.morning(self.build())
        p = self.decide(doc, 1, code=3, err="Emily has started 3 build(s) today\nThe cap resets at midnight Eastern.")
        self.assertEqual(p["sent"]["status"], "not sent")
        self.assertIn("cap resets", p["sent"]["detail"])
        p = self.decide(doc, 1)
        self.assertEqual(p["sent"]["status"], "sent")
        self.assertEqual(len(self.sent), 2)

    def test_what_was_sent_is_not_sent_twice_or_called_back(self):
        doc = self.morning(self.build())
        self.decide(doc, 1)
        self.decide(doc, 1)
        self.assertEqual(len(self.sent), 1, "approving again must not queue a second build")
        with self.assertRaises(self.gm.knowledge.Refused):
            self.decide(doc, 1, verdict="decline")

    def test_an_owner_proposal_sends_nothing(self):
        doc = self.morning(self.prop())
        p = self.decide(doc, 1)
        self.assertNotIn("sent", p)
        self.assertEqual(self.sent, [])

    def test_tomorrow_the_gm_reads_what_happened(self):
        doc = self.morning(self.build(), self.prop(who="scout", dept="etsy", task="run_scout",
                                                   facts=["F2"], title="Run Scout"))
        self.decide(doc, 1)
        self.decide(doc, 2, budget=dict(self.OK_BUDGET, ai_left_today=0.0))
        _, _, calls = self.run_gm({"summary": "s"}, now=self.now + timedelta(days=1))
        user = calls[0][1]["content"]
        self.assertIn('approved "Build the library tote". Note: go It was sent to emily.', user)
        self.assertIn('approved "Run Scout". Note: go It was not sent: today', user)

    def test_the_deck_waits_longer_than_a_send(self):
        js = (ROOT / "mission-control-api" / "townhall.js").read_text()
        ms = int(re.search(r"DECIDE_TIMEOUT_MS = (\d+)", js).group(1))
        self.assertGreater(ms, self.gm.SEND_TIMEOUT * 1000)
        self.assertIn("DECIDE_TIMEOUT_MS));", js)


class TheListingAuditNamesWhatToFix(unittest.TestCase):
    """store-report.py audit: each listing's title, tags and description
    against Etsy's limits and what sellers measured (researched 2026-09-25).

    The shop had 13 views and 0 favourites across its listings; the first
    lever the research found free was the listings' own words.
    """

    @classmethod
    def setUpClass(cls):
        cls.sr = load("store_report_audit", "store-report.py")

    GOOD = {"listing_id": 1,
            "title": "Botanical Library Tote Bag, Book Lover Gift, Vintage Floral Canvas Tote for Readers",
            "views": 9, "favs": 1,
            "tags": ["library tote bag", "book lover gift", "botanical tote", "gift for reader",
                     "librarian gift", "vintage floral tote", "canvas book bag", "bookish tote bag",
                     "reading gift", "floral canvas tote", "teacher tote bag", "book club gift",
                     "market tote bag"],
            "description": "This botanical library tote bag is a book lover gift for readers. " + "word " * 60}

    def issues(self, **kw):
        return self.sr.audit_listing(dict(self.GOOD, **kw))

    def said(self, **kw):
        return " | ".join(m for _, m in self.issues(**kw))

    def test_a_well_made_listing_passes(self):
        self.assertEqual(self.issues(), [])

    def test_empty_tag_slots_are_etsys_limit(self):
        kinds = dict((m, k) for k, m in self.issues(tags=self.GOOD["tags"][:6]))
        msg = next(m for m in kinds if "6 of 13 tags" in m)
        self.assertEqual(kinds[msg], "LIMIT")

    def test_a_plural_or_repeat_wastes_a_slot(self):
        tags = self.GOOD["tags"][:11] + ["library tote bags", "Book Lover Gift"]
        said = self.said(tags=tags)
        self.assertIn("'library tote bag' and 'library tote bags' are one search, two slots", said)
        self.assertIn("'book lover gift' and 'Book Lover Gift' are the same tag twice", said)
        stem = self.sr._stem
        self.assertEqual((stem("bags"), stem("gifts"), stem("glass"), stem("bus")),
                         ("bag", "gift", "glass", "bus"), "ss and short words are not plurals")

    def test_one_word_tags_over_three_are_flagged(self):
        three = self.GOOD["tags"][:10] + ["tote", "library", "floral"]
        self.assertNotIn("one-word tags", self.said(tags=three))
        four = self.GOOD["tags"][:9] + ["tote", "library", "floral", "vintage"]
        self.assertIn("4 one-word tags", self.said(tags=four))

    def test_a_tag_unrelated_to_the_title_is_named(self):
        tags = self.GOOD["tags"][:12] + ["halloween decor"]
        self.assertIn("share no word with the title (halloween decor)", self.said(tags=tags))

    def test_a_short_title_or_one_without_a_tag_up_front(self):
        self.assertIn("title uses 21 of 140", self.said(title="Organizer Insert Tote"))
        buried = "A Lovely Present For Anyone Who Enjoys It - library tote bag"
        self.assertIn("first 40 characters", self.said(title=buried))

    def test_repeats_and_shouting(self):
        said = self.said(title="Tote Bag Tote Bag Tote Bag, LIBRARY GIFT, Book Lover Gift For Readers")
        self.assertIn("title repeats bag, tote", said)
        self.assertIn("title shouts (LIBRARY GIFT)", said)

    def test_a_thin_description_or_an_unrelated_opening(self):
        self.assertIn("description is 8 words",
                      self.said(description="Made for me by my production partner, Printify."))
        opening = "Made for me by my production partner. " + "word " * 80
        self.assertIn("opening shares no word with the title", self.said(description=opening))

    def test_worst_listing_first_and_one_line_for_the_briefing(self):
        weak = dict(self.GOOD, listing_id=2, title="Organizer Insert Tote", tags=["tote"])
        rows = self.sr.audit({"listings": [self.GOOD, weak]})
        self.assertEqual(rows[0]["listing_id"], 2)
        self.assertEqual(self.sr.audit_line(rows),
                         "Listing audit: 1 of 2 listings have title or tag fixes (store-report.py audit).")
        s = self.sr.summary({"day": "2026-09-25", "listings": [self.GOOD, weak]})
        self.assertIn(self.sr.audit_line(rows), self.sr.lines(s),
                      "the briefing's store section carries it, so the GM sees it")

    def test_etsys_own_limits(self):
        self.assertEqual((self.sr.TAG_SLOTS, self.sr.TAG_MAX, self.sr.TITLE_MAX), (13, 20, 140))


class ListingVideosAreMadeWithinEtsysLimits(unittest.TestCase):
    """listing-video.py: a slow zoom over each of a listing's photos, checked
    against Etsy's limits (5-15 s, 100 MB, 500 px, no sound) before it is
    handed over."""

    @classmethod
    def setUpClass(cls):
        cls.lv = load("listing_video", "listing-video.py")

    def ff(self):
        if not all(self.lv.ffmpeg()):
            self.skipTest("ffmpeg is not installed here")

    def still(self, d, name, size="800x600", color="teal"):
        p = Path(d) / name
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"color=c={color}:s={size}", "-frames:v", "1", str(p)], check=True)
        return p

    def test_the_timing_adds_up_to_the_length_asked(self):
        for n in (1, 2, 3, 4):
            for seconds in (5, 10, 15):
                each, fade, offsets = self.lv.plan(n, seconds)
                self.assertAlmostEqual(n * each - (n - 1) * fade, seconds, places=2)
                self.assertEqual(len(offsets), n - 1)
        self.assertEqual(self.lv.plan(1, 10), (10.0, 0.0, []))
        with self.assertRaises(ValueError):
            self.lv.plan(0, 10)

    def test_the_command_is_a_list_with_no_sound(self):
        cmd = self.lv.command(["a.png", "b.png", "c.png"], "out.mp4", 10)
        self.assertIsInstance(cmd, list)
        self.assertIn("-an", cmd)
        self.assertEqual(cmd.count("-i"), 3)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(graph.count("xfade="), 2)
        self.assertEqual(cmd[cmd.index("-t") + 1], "10")

    def test_a_real_video_is_square_silent_and_the_right_length(self):
        self.ff()
        with tempfile.TemporaryDirectory() as d:
            imgs = [self.still(d, "a.png"), self.still(d, "b.png", "600x900", "orange"),
                    self.still(d, "c.png", "1200x1200", "navy")]
            out = Path(d) / "v.mp4"
            ok, msg = self.lv.make(imgs, out, 10)
            self.assertTrue(ok, msg)
            secs, w, h, audio = self.lv.probe(out)
            self.assertAlmostEqual(secs, 10, delta=0.2)
            self.assertEqual((w, h, audio), (1080, 1080, False))
            self.assertEqual(self.lv.problems(out), [])
            self.assertFalse((Path(d) / "v.part.mp4").exists(), "no half-written file is left")

    def test_etsys_limits_are_checked_on_the_file(self):
        self.ff()
        with tempfile.TemporaryDirectory() as d:
            long_loud = Path(d) / "bad.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=red:s=320x320:d=20", "-f", "lavfi", "-i", "sine=d=20",
                            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(long_loud)],
                           check=True)
            said = " | ".join(self.lv.problems(long_loud))
            for want in ("20.0s long", "320x320", "sound track"):
                self.assertIn(want, said)

    def test_a_video_etsy_would_refuse_is_not_handed_over(self):
        self.ff()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "v.mp4"
            with unittest.mock.patch.object(self.lv, "problems", return_value=["20.0s long"]):
                ok, msg = self.lv.make([self.still(d, "a.png")], out, 6)
            self.assertFalse(ok)
            self.assertIn("not usable on Etsy: 20.0s long", msg)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["a.png"],
                             "neither the video nor a half-written file is left")

    def test_lengths_outside_etsys_are_refused_before_any_work(self):
        self.ff()
        for seconds in (4, 16):
            ok, msg = self.lv.make(["x.png"], "/nonexistent/v.mp4", seconds)
            self.assertFalse(ok)
            self.assertIn("5 to 15", msg)

    def test_without_ffmpeg_it_says_how_to_get_it(self):
        with unittest.mock.patch.object(self.lv, "ffmpeg", return_value=(None, None)):
            ok, msg = self.lv.make(["a.png"], "v.mp4", 10)
        self.assertFalse(ok)
        self.assertIn("apt install -y ffmpeg", msg)

    def test_photos_come_in_etsys_order_largest_size(self):
        class EP:
            def call(self, path, key):
                assert path == "/listings/42/images", path
                return {"results": [{"rank": 2, "url_fullxfull": "https://i/2.jpg"},
                                    {"rank": 1, "url_fullxfull": "https://i/1.jpg", "url_570xN": "https://i/s.jpg"},
                                    {"rank": 3, "url_570xN": "https://i/3.jpg"}]}, {}, None
        self.assertEqual(self.lv.listing_photos("42", "k", EP()),
                         ["https://i/1.jpg", "https://i/2.jpg", "https://i/3.jpg"])

    def test_only_https_photos_are_fetched(self):
        with tempfile.TemporaryDirectory() as d, \
             unittest.mock.patch.object(urllib.request, "urlopen") as op:
            self.assertEqual(self.lv.download(["file:///etc/passwd", "http://x/a.jpg"], d), [])
            op.assert_not_called()


class TheDeckOffersEachVideoForDownload(unittest.TestCase):
    """emily.js: the videos listing-video.py made, and a route Safari can play."""

    API = ROOT / "mission-control-api"
    NODE = shutil.which("node")

    def listed(self, root):
        if not (self.NODE and (self.API / "node_modules" / "better-sqlite3").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        js = (f"const m=require({json.dumps(str(self.API / 'emily.js'))});"
              "process.stdout.write(JSON.stringify(m.listVideos()))")
        r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True,
                           env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_videos_are_listed_with_their_titles(self):
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "agents" / "emily" / "shop" / "videos"
            v.mkdir(parents=True)
            (v / "4412345678.mp4").write_bytes(b"x" * 10)
            (v / "99.mp4").write_bytes(b"y")
            (v / "notes.mp4").write_bytes(b"z")
            (v / "index.json").write_text(json.dumps({"4412345678": {"title": "Library Tote", "seconds": 10}}))
            rows = {r["listing_id"]: r for r in self.listed(d)}
        self.assertEqual(set(rows), {"4412345678", "99"}, "only <listing id>.mp4 files")
        self.assertEqual((rows["4412345678"]["title"], rows["4412345678"]["bytes"]), ("Library Tote", 10))
        self.assertEqual(rows["99"]["title"], "", "a file with no index entry is still listed")

    def test_the_route_answers_ranges_so_safari_will_play_it(self):
        # Safari will not play a video from a server that ignores Range; it
        # asks for bytes 0-1 first and gives up on a 200.
        if not (self.NODE and (self.API / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "agents" / "emily" / "shop" / "videos"
            v.mkdir(parents=True)
            (v / "123.mp4").write_bytes(bytes(range(256)) * 4)
            js = (
                f"const express=require({json.dumps(str(self.API / 'node_modules' / 'express'))});"
                f"const emily=require({json.dumps(str(self.API / 'emily.js'))});"
                "const http=require('http');const app=express();emily.register(app,null);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const get=(p,h)=>new Promise(ok=>http.get({host:'127.0.0.1',port,path:p,headers:h||{}},"
                "r=>{let n=0;r.on('data',c=>n+=c.length);r.on('end',()=>ok([r.statusCode,n,"
                "r.headers['content-type']||'',r.headers['content-disposition']||'']));}));"
                "const out={range:await get('/api/emily/shop/video/123',{Range:'bytes=0-1'}),"
                "dl:await get('/api/emily/shop/video/123?dl=1'),"
                "bad:await get('/api/emily/shop/video/..%2F..%2Fsecret'),"
                "none:await get('/api/emily/shop/video/456')};"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, ECOSYSTEM_ROOT=d))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["range"][:3], [206, 2, "video/mp4"])
        self.assertEqual(out["dl"][0], 200)
        self.assertIn('filename="listing-123.mp4"', out["dl"][3])
        self.assertEqual(out["bad"][0], 400)
        self.assertEqual(out["none"][0], 404)


class AceShowsTheEdgeOnEveryJudgedGame(unittest.TestCase):
    """ace-judge.py show: the numbers behind each verdict, not just the reason.

    The owner asked (2026-09-26) for the edge on every game Ace judged. The
    ledger always stored price, no-vig chance, Ace's estimate and the edge;
    `show` printed only the reason.
    """

    @classmethod
    def setUpClass(cls):
        cls.aj = load("ace_judge_show", "ace-judge.py")

    def test_a_judged_row_shows_price_market_estimate_and_edge(self):
        line = self.aj.edge_line({"price": -180, "novig_pct": 62.4, "my_pct": 71.0,
                                  "edge_pts": 8.6, "stake": 150.0})
        self.assertEqual(line, "price -180  market 62.4%  Ace 71.0%  edge +8.6 pts  stake 150")
        self.assertIn("edge -3.0 pts", self.aj.edge_line({"price": 110, "novig_pct": 50.0,
                                                          "my_pct": 47.0, "edge_pts": -3.0}))

    def test_a_swept_row_says_it_has_no_estimate_rather_than_an_edge(self):
        line = self.aj.edge_line({"price": -250, "novig_pct": 69.8, "my_pct": None})
        self.assertEqual(line, "price -250  market 69.8%  Ace: no estimate (swept)")
        self.assertNotIn("edge", line)

    def test_show_prints_it_under_every_row(self):
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "ledger.json"
            led.write_text(json.dumps({"day": "2026-09-26", "candidates": [
                {"selection": "Oregon", "match": "UCLA @ Oregon", "price": -180, "novig_pct": 62.4,
                 "my_pct": 71.0, "edge_pts": 8.6, "status": "bet", "stake": 150, "why_not": ["x"]},
                {"selection": "Texas", "match": "Texas @ Ole Miss", "price": -250,
                 "novig_pct": 69.8, "my_pct": None, "status": "passed", "why_not": ["y"]}]}))
            out = io.StringIO()
            with unittest.mock.patch.object(self.aj, "LEDGER", led), contextlib.redirect_stdout(out):
                self.aj.cmd_show(None)
        text = out.getvalue()
        self.assertIn("Ace 71.0%  edge +8.6 pts", text)
        self.assertIn("Ace: no estimate (swept)", text)
        self.assertIn("2 rows, 1 with Ace's own estimate", text)

    def test_show_reads_an_earlier_cycles_ledger_instead_of_calling_it_empty(self):
        # load_ledger() starts a fresh ledger for a new slot - right for
        # writing, wrong for showing what Ace decided this afternoon.
        with tempfile.TemporaryDirectory() as d:
            led = Path(d) / "ledger.json"
            led.write_text(json.dumps({"day": "2026-09-20", "slot": "afternoon", "candidates": [
                {"selection": "Iowa", "match": "Iowa @ Wisconsin", "price": 120, "novig_pct": 43.9,
                 "my_pct": 46.0, "edge_pts": 2.1, "status": "passed", "why_not": ["gap"]}]}))
            out = io.StringIO()
            with unittest.mock.patch.object(self.aj, "LEDGER", led), contextlib.redirect_stdout(out):
                self.aj.cmd_show(None)
        self.assertIn("day 2026-09-20  slot afternoon", out.getvalue())
        self.assertIn("Ace 46.0%  edge +2.1 pts", out.getvalue())


class AceFocusDayJudgesEveryGameItShows(unittest.TestCase):
    """ace-focus.py: one sport and a slate Ace can finish, for one day.

    2026-09-26: the owner wanted college football. Ace's eight-game slate held
    four baseball games and four college ones; he studied three baseball games
    and swept all four college games unread. A focus day cuts the slate to
    the sport and the few games he studies, picks them by fresh news, and
    ace-judge.py refuses the sweep.
    """

    def setUp(self):
        self.focus = load("ace_focus_t", "ace-focus.py")
        self.fetch = load("ace_fetch_focus", "ace-fetch.py")
        self.judge = load("ace_judge_focus", "ace-judge.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.ctx = self.d / "context"
        self.ctx.mkdir()
        self.FOCUS = {"sport": "cfb", "games": 2, "day": "2026-09-26"}

    def tearDown(self):
        self.tmp.cleanup()

    # --- the setting ------------------------------------------------------

    def test_a_focus_is_for_its_own_eastern_day_only(self):
        f = self.d / "focus.json"
        f.write_text(json.dumps({"day": "2026-09-26", "sport": "cfb", "games": 4}))
        self.assertEqual(self.focus.today("2026-09-26", f), {"sport": "cfb", "games": 4, "day": "2026-09-26"})
        self.assertIsNone(self.focus.today("2026-09-27", f), "it stops at midnight Eastern by itself")
        for bad in ({"day": "2026-09-26", "sport": "cfb", "games": 0},
                    {"day": "2026-09-26", "sport": "", "games": 4},
                    {"day": "2026-09-26", "sport": "cfb", "games": "lots"}):
            f.write_text(json.dumps(bad))
            self.assertIsNone(self.focus.today("2026-09-26", f), bad)

    def test_set_and_clear(self):
        f = self.d / "focus.json"
        with unittest.mock.patch.object(self.focus, "FOCUS", f), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.focus.main(["today", "cfb"]), 0)
            self.assertEqual(json.loads(f.read_text())["games"], self.focus.GAMES)
            self.assertEqual(self.focus.main(["today", "curling"]), 2)
            self.assertEqual(self.focus.main(["today", "cfb", "--games", "9"]), 2)
            self.assertEqual(self.focus.main(["clear"]), 0)
        self.assertFalse(f.exists())

    # --- the slate --------------------------------------------------------

    def game(self, sport, name, hours_out, news_hours_ago=None):
        start = datetime.now(timezone.utc) + timedelta(hours=hours_out)
        inj = []
        if news_hours_ago is not None:
            when = datetime.now(timezone.utc) - timedelta(hours=news_hours_ago)
            inj = [{"team": "HOM", "player": "QB1", "position": "QB", "status": "Out",
                    "date": when.strftime("%Y-%m-%dT%H:%MZ")}]
        (self.ctx / f"{sport}-{name}.json").write_text(json.dumps({
            "sport": sport, "short": name, "match": name,
            "start_utc": start.strftime("%Y-%m-%dT%H:%MZ"), "status": "STATUS_SCHEDULED",
            "home": {"abbr": name[:3] + "H"}, "away": {"abbr": name[:3] + "A"},
            "odds": {"moneyline_home": -150, "moneyline_away": 130,
                     "novig_home_pct": 58.0, "novig_away_pct": 42.0},
            "injuries": inj}))

    def test_a_focus_slate_is_one_sport_news_first_and_no_bigger_than_asked(self):
        # Baseball starts soonest, and two quiet college games start before the
        # one with news - so each rule, not the clock, decides who is picked.
        self.game("mlb", "TOR", 0.5)
        self.game("mlb", "MIA", 0.7)
        self.game("cfb", "SOON", 1)
        self.game("cfb", "MID", 3)
        self.game("cfb", "NEWS", 6, news_hours_ago=2)
        doc = self.fetch.build_ledger("2026-09-26", self.ctx, "afternoon", self.FOCUS)
        matches = []
        for r in doc["candidates"]:
            if r["match"] not in matches:
                matches.append(r["match"])
        self.assertEqual(set(matches), {"NEWS", "SOON"}, "the news game beats a sooner quiet one")
        self.assertEqual({r["sport"] for r in doc["candidates"]}, {"cfb"})
        self.assertEqual(doc["focus"]["sport"], "cfb")
        self.assertIn("refuses `rest`", doc["focus"]["rule"])

    def test_without_a_focus_the_slate_is_what_it_was(self):
        self.game("mlb", "TOR", 1)
        self.game("cfb", "SOON", 1)
        doc = self.fetch.build_ledger("2026-09-26", self.ctx, "afternoon")
        self.assertIsNone(doc["focus"])
        self.assertEqual({r["sport"] for r in doc["candidates"]}, {"mlb", "cfb"})

    # --- judging ----------------------------------------------------------

    def judging(self, focus=True):
        agent = self.d / "ace"
        (agent / "data").mkdir(parents=True)
        (agent / "state").mkdir(parents=True)
        rows = [{"selection": "IOWA ML", "match": "IOWA @ MICH", "sport": "cfb", "price": 180,
                 "novig_pct": 34.3, "starts_utc": "2026-09-26T19:30Z"},
                {"selection": "MICH ML", "match": "IOWA @ MICH", "sport": "cfb", "price": -218,
                 "novig_pct": 65.7, "starts_utc": "2026-09-26T19:30Z"},
                {"selection": "TCU ML", "match": "TCU @ UCF", "sport": "cfb", "price": -170,
                 "novig_pct": 60.4, "starts_utc": "2026-09-26T23:00Z"},
                {"selection": "UCF ML", "match": "TCU @ UCF", "sport": "cfb", "price": 142,
                 "novig_pct": 39.6, "starts_utc": "2026-09-26T23:00Z"}]
        (agent / "data" / "candidates.json").write_text(json.dumps({"candidates": rows}))
        for name, val in (("AGENT", agent), ("CANDIDATES", agent / "data" / "candidates.json"),
                          ("LEDGER", agent / "state" / "ledger.json")):
            p = unittest.mock.patch.object(self.judge, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(self.judge, "focus_today",
                                       return_value=self.FOCUS if focus else None)
        p.start()
        self.addCleanup(p.stop)
        return agent / "state" / "ledger.json"

    def act(self, fn, **kw):
        a = types.SimpleNamespace(**dict({"number": "1", "my_pct": None, "why": "x", "stake": None}, **kw))
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = fn(a) if fn is self.judge.cmd_rest else fn(a, "passed")
        return code, out.getvalue() + err.getvalue()

    def test_no_sweep_and_no_batch_on_a_focus_day(self):
        self.judging()
        code, said = self.act(self.judge.cmd_rest, why="did not clear 8")
        self.assertEqual(code, 1)
        self.assertIn("no sweeping", said)
        code, said = self.act(self.judge.record, number="1-4", my_pct=40.0)
        self.assertEqual(code, 1)
        self.assertIn("one game at a time", said)
        code, said = self.act(self.judge.record, number="1")
        self.assertEqual(code, 1)
        self.assertIn("needs your --my-pct", said)

    def test_one_estimate_covers_both_sides_of_the_game(self):
        led = self.judging()
        code, said = self.act(self.judge.record, number="1", my_pct=38.0, why="QB back")
        self.assertEqual(code, 0, said)
        rows = {r["selection"]: r for r in json.loads(led.read_text())["candidates"]}
        self.assertEqual(set(rows), {"IOWA ML", "MICH ML"}, "only this game's two sides")
        self.assertEqual((rows["IOWA ML"]["my_pct"], rows["IOWA ML"]["edge_pts"]), (38.0, 3.7))
        self.assertEqual((rows["MICH ML"]["my_pct"], rows["MICH ML"]["edge_pts"]), (62.0, -3.7))
        self.assertIn("other side of IOWA ML", rows["MICH ML"]["why_not"][0])
        journal = led.parent / "judgements.jsonl"
        logged = [json.loads(l) for l in journal.read_text().splitlines()]
        self.assertEqual({(j["selection"], j["my_pct"]) for j in logged},
                         {("IOWA ML", 38.0), ("MICH ML", 62.0)},
                         "both sides reach the closing-line journal, in Ace's own folder")

    def test_a_side_already_judged_is_not_overwritten(self):
        led = self.judging()
        self.act(self.judge.record, number="2", my_pct=70.0, why="first")
        self.act(self.judge.record, number="1", my_pct=25.0, why="second")
        rows = {r["selection"]: r for r in json.loads(led.read_text())["candidates"]}
        self.assertEqual(rows["MICH ML"]["my_pct"], 70.0)
        self.assertEqual(rows["IOWA ML"]["my_pct"], 25.0)

    def test_on_a_normal_day_nothing_changes(self):
        led = self.judging(focus=False)
        code, _ = self.act(self.judge.cmd_rest, why="did not clear 8")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(led.read_text())["candidates"]), 4)

    def test_ace_is_told(self):
        head = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()
        self.assertIn("A focus day is different", head)


class AceIsGradedOnTheClosingLine(unittest.TestCase):
    """ace-clv.py: bets graded by closing line value, estimates by whether the
    market moved toward them (researched 2026-09-26: CLV is the most reliable
    early sign of a real edge; wins and losses take thousands of bets)."""

    def setUp(self):
        self.clv = load("ace_clv_t", "ace-clv.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.lines, self.journal = self.d / "lines.json", self.d / "judgements.jsonl"
        self.start = datetime(2026, 9, 26, 19, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, home_pct, ml_home=-218, ml_away=180):
        return {"sport": "cfb", "short": "IOWA @ MICH", "start_utc": "2026-09-26T19:30Z",
                "home": {"abbr": "MICH"}, "away": {"abbr": "IOWA"},
                "odds": {"moneyline_home": ml_home, "moneyline_away": ml_away,
                         "novig_home_pct": home_pct, "novig_away_pct": round(100 - home_pct, 1)}}

    def at(self, hours_before):
        return self.start - timedelta(hours=hours_before)

    def test_the_last_price_before_kickoff_is_kept_and_never_after(self):
        self.clv.record_lines([self.ctx(65.7)], self.at(6), self.lines)
        self.clv.record_lines([self.ctx(63.9, -200, 165)], self.at(0.4), self.lines)
        self.clv.record_lines([self.ctx(50.0, -100, 100)], self.start + timedelta(minutes=5), self.lines)
        e = json.loads(self.lines.read_text())["cfb|IOWA @ MICH|2026-09-26T19:30Z"]
        self.assertEqual(e["first"]["novig_home_pct"], 65.7)
        self.assertEqual(e["last"]["novig_home_pct"], 63.9, "a started game is never updated")
        self.assertEqual((e["home"], e["away"], e["observations"]), ("MICH", "IOWA", 2))

    def test_unpriced_games_are_not_tracked_and_old_ones_are_dropped(self):
        c = self.ctx(65.7)
        c["odds"]["novig_home_pct"] = None
        self.clv.record_lines([c], self.at(6), self.lines)
        self.assertEqual(json.loads(self.lines.read_text()), {})
        self.clv.record_lines([self.ctx(65.7)], self.at(6), self.lines)
        later = self.start + timedelta(days=self.clv.KEEP_DAYS + 1)
        self.clv.record_lines([], later, self.lines)
        self.assertEqual(json.loads(self.lines.read_text()), {})

    def entry(self, close_home_pct, close_at_hours_before=0.4):
        self.clv.record_lines([self.ctx(65.7)], self.at(6), self.lines)
        self.clv.record_lines([self.ctx(close_home_pct, -200, 165)],
                              self.at(close_at_hours_before), self.lines)
        return json.loads(self.lines.read_text())["cfb|IOWA @ MICH|2026-09-26T19:30Z"]

    def judged(self, **kw):
        j = {"judged_utc": self.at(4).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "key": "cfb|IOWA @ MICH|2026-09-26T19:30Z", "selection": "IOWA ML",
             "match": "IOWA @ MICH", "status": "passed", "price": 180, "novig_pct": 34.3,
             "my_pct": 38.0, "stake": None}
        j.update(kw)
        return j

    def test_a_bet_is_valued_at_the_closing_chance(self):
        # Took Iowa at +180 (decimal 2.8); Iowa closed at 36.1%: 0.361 x 2.8 - 1 = +1.08%.
        g = self.clv.grade(self.judged(status="bet", stake=150), self.entry(63.9), self.start)
        self.assertEqual(g["clv_pct"], 1.1)
        self.assertEqual(g["close_pct"], 36.1)
        g = self.clv.grade(self.judged(status="bet", stake=150), self.entry(70.0), self.start)
        self.assertLess(g["clv_pct"], 0, "the market moved against the bet")

    def test_an_estimate_is_graded_by_which_way_the_line_went(self):
        toward = self.clv.grade(self.judged(my_pct=38.0), self.entry(63.9), self.start)
        self.assertEqual((toward["moved"], toward["toward"]), (1.8, 1.8))
        against = self.clv.grade(self.judged(my_pct=30.0), self.entry(63.9), self.start)
        self.assertEqual(against["toward"], -1.8)
        self.assertIsNone(self.clv.grade(self.judged(my_pct=None), self.entry(63.9), self.start)["toward"])

    def test_a_close_seen_before_the_verdict_grades_nothing(self):
        g = self.clv.grade(self.judged(judged_utc=self.at(0.2).strftime("%Y-%m-%dT%H:%M:%SZ")),
                           self.entry(63.9, close_at_hours_before=0.4), self.start)
        self.assertFalse(g["later_price"])
        self.assertIsNone(g["toward"])

    def test_nothing_is_graded_before_kickoff(self):
        self.assertIsNone(self.clv.grade(self.judged(), self.entry(63.9), self.at(0.1)))

    def test_prices_and_sides(self):
        self.assertAlmostEqual(self.clv.decimal_odds(180), 2.8)
        self.assertAlmostEqual(self.clv.decimal_odds(-200), 1.5)
        self.assertIsNone(self.clv.decimal_odds(50))
        e = {"home": "MICH", "away": "IOWA"}
        self.assertEqual((self.clv.side_of("IOWA ML", e), self.clv.side_of("MICH ML", e),
                          self.clv.side_of("OSU ML", e)), ("away", "home", None))

    def test_the_latest_verdict_on_a_side_is_the_one_graded(self):
        self.clv.log_judgements([dict(selection="IOWA ML", match="IOWA @ MICH", sport="cfb",
                                      starts_utc="2026-09-26T19:30Z", status="passed",
                                      price=180, novig_pct=34.3, my_pct=30.0)],
                                self.at(5), self.journal)
        self.clv.log_judgements([dict(selection="IOWA ML", match="IOWA @ MICH", sport="cfb",
                                      starts_utc="2026-09-26T19:30Z", status="passed",
                                      price=180, novig_pct=34.3, my_pct=38.0)],
                                self.at(4), self.journal)
        self.entry(63.9)
        r = self.clv.report(self.start, self.lines, self.journal)
        self.assertEqual(len(r["graded"]), 1)
        self.assertEqual(r["graded"][0]["my_pct"], 38.0)
        self.assertEqual((r["estimates"], r["estimates_toward"]), (1, 1))

    def test_the_fetchers_game_and_the_judges_row_join(self):
        # The whole report rests on this join. A slate row goes through the
        # fetcher's build_ledger and ace-judge's CARRY copy, exactly as in a
        # cycle; the price history is keyed from the context file. If either
        # side renames a field, nothing is ever graded - silently.
        fetch = load("ace_fetch_clv", "ace-fetch.py")
        judge = load("ace_judge_clv", "ace-judge.py")
        ctxdir = self.d / "context"
        ctxdir.mkdir()
        start = datetime.now(timezone.utc) + timedelta(hours=3)
        c = self.ctx(65.7)
        c["start_utc"] = start.strftime("%Y-%m-%dT%H:%MZ")
        c["status"], c["injuries"] = "STATUS_SCHEDULED", []
        (ctxdir / "cfb-1.json").write_text(json.dumps(c))
        row = next(r for r in fetch.build_ledger("2026-09-26", ctxdir, "afternoon")["candidates"]
                   if r["selection"] == "IOWA ML")
        copied = {k: row.get(k) for k in judge.CARRY}
        copied.update(status="passed", my_pct=38.0)
        self.clv.log_judgements([copied], datetime.now(timezone.utc), self.journal)
        self.clv.record_lines([c], datetime.now(timezone.utc), self.lines)
        r = self.clv.report(start + timedelta(minutes=1), self.lines, self.journal)
        self.assertEqual(len(r["graded"]), 1, "the verdict and the price history did not join")
        self.assertEqual(r["graded"][0]["selection"], "IOWA ML")

    def test_every_verdict_is_logged_including_the_filled_in_side(self):
        src = (SCRIPTS / "ace-judge.py").read_text()
        self.assertIn("log_for_clv(done + filled)", src)
        self.assertIn('AGENT / "state" / mod.JOURNAL.name', src)
        fetch = (SCRIPTS / "ace-fetch.py").read_text()
        self.assertIn("ace_clv().record_lines(written)", fetch)


class TheWakeMessageNamesTheReport(unittest.TestCase):
    """2026-09-25: Belfort's close run was handed "2026-09-25-close.md" in
    _meta.json, edited the morning's open report instead and was failed for
    it. The exact name now goes in the wake message, from the same function
    the verifier checks against."""

    def test_the_command_prints_what_the_verifier_expects(self):
        for agent in ("belfort", "ace"):
            r = subprocess.run([sys.executable, str(SCRIPTS / "et_time.py"), "report-name", agent],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), et_time.report_name(agent))
        r = subprocess.run([sys.executable, str(SCRIPTS / "et_time.py"), "report-name", "emily"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, "an agent without slots has no such name")

    def test_both_cycles_put_it_in_the_message(self):
        for agent in ("belfort", "ace"):
            src = (SCRIPTS / f"{agent}-cycle.sh").read_text()
            self.assertIn(f'REPORT="$(python3 "$ROOT/scripts/et_time.py" report-name {agent})"', src)
            message = src.split("--message", 1)[1].split("--session-id", 1)[0]
            self.assertIn("reports/$REPORT", message)
            self.assertIn("NEW file", message)
            self.assertLess(src.index('REPORT="$('), src.index("openclaw agent"),
                            "the name is worked out before the agent is woken")


class AceBetsByTheReviewedBar(unittest.TestCase):
    """2026-10-01, from an outside review. The +3% expected-value bar was Ace's
    own estimate restated - any estimate a few points over the line cleared
    it, most easily on long shots - and "real information the market has not
    priced" was a sentence. Now the estimate is pulled 60% back to the market
    before it is measured, a bet cites a fresh injury by id, code checks the
    price has not already moved on it, and the stake is a flat $100 kept by
    ace-book.py. (Was AceBetsOnExpectedValueWithQuarterKellyStakes.)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["ACE_NO_LIVE_PRICE"] = "1"
        self.addCleanup(os.environ.pop, "ACE_NO_LIVE_PRICE", None)
        self.m = load("ace_judge_ev", "ace-judge.py")
        agent = Path(self.tmp.name) / "ace"
        (agent / "state").mkdir(parents=True)
        (agent / "data").mkdir(parents=True)
        for name, val in (("AGENT", agent), ("CANDIDATES", agent / "data" / "candidates.json"),
                          ("LEDGER", agent / "state" / "ledger.json")):
            setattr(self.m, name, val)
        self.now = datetime.now(timezone.utc)
        self.start = (self.now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%MZ")
        self.news(hours_ago=1.0)
        (agent / "state" / "bankroll.json").write_text(json.dumps(
            {"starting_bankroll": 10000.0, "bankroll": 10000.0, "cycle_count": 3}))
        self.prices_seen(before=41.5)

    def stamp(self, hours_ago):
        return (self.now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%MZ")

    def news(self, hours_ago, sharp=None):
        inj = [{"id": "iabc12", "team": "FAV", "player": "QB One", "position": "QB", "status": "Out",
                "reported_utc": self.stamp(hours_ago), "hours_old": hours_ago}]
        row = lambda sel, side, price, pct: {"selection": sel, "match": "DOG @ FAV", "price": price,
                                             "novig_pct": pct, "sport": "nfl", "event_id": "401",
                                             "side": side, "starts_utc": self.start,
                                             "sharp_pct": sharp, "fresh_injuries": inj}
        self.m.CANDIDATES.write_text(json.dumps({"asof_utc": "x", "candidates": [
            row("FAV ML", "home", -150, 58.0), row("DOG ML", "away", 150, 42.0)]}))

    def prices_seen(self, before, hours_ago=3.0):
        snap = lambda h, away: {"at": (self.now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "novig_home_pct": 100 - away, "novig_away_pct": away}
        key = f"nfl|DOG @ FAV|{self.start}"
        (self.m.AGENT / "state" / "lines.json").write_text(json.dumps({key: {
            "sport": "nfl", "match": "DOG @ FAV", "start_utc": self.start, "home": "FAV", "away": "DOG",
            "first": snap(hours_ago, before), "last": snap(0, 42.0),
            "history": [snap(hours_ago, before), snap(0, 42.0)]}}))

    def act(self, status, number="1", my_pct=None, stake=None, why="x", event=None):
        a = types.SimpleNamespace(number=number, my_pct=my_pct, stake=stake, why=why, event=event)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = self.m.record(a, status)
        return code, out.getvalue()

    def rows(self):
        return {r["selection"]: r for r in json.loads(self.m.LEDGER.read_text())["candidates"]}

    def bets(self):
        p = self.m.AGENT / "state" / "bets.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_expected_value_at_the_offered_price(self):
        self.assertEqual(self.m.ev_pct(71.0, -150), 18.3)       # 0.71 x 1.667 - 1
        self.assertEqual(self.m.ev_pct(40.0, 150), 0.0)         # 0.40 x 2.5 - 1
        self.assertEqual(self.m.ev_pct(58.0, -150), -3.3)
        self.assertIsNone(self.m.ev_pct(None, -150))
        self.assertIsNone(self.m.ev_pct(50.0, None))

    def test_quarter_kelly_and_its_cap_now_a_shadow(self):
        self.assertEqual(self.m.kelly_stake(45.0, 150, 10000), (208.33, 2.08))
        self.assertEqual(self.m.kelly_stake(70.0, 150, 10000), (300.0, 3.0))
        self.assertEqual(self.m.kelly_stake(40.0, 150, 10000), (0.0, 0.0), "no edge, no stake")

    def test_the_estimate_is_pulled_toward_the_market(self):
        # The header's own example: 50% against a 42.8% market counts as 45.7%.
        v = self.m.bar(50.0, {"novig_pct": 42.8, "price": 124})
        self.assertEqual((v["p_used"], v["gap_pts"], v["need_pts"], v["clears"]), (45.7, 2.9, 2.0, True))
        v = self.m.bar(46.0, {"novig_pct": 42.8, "price": 124})
        self.assertEqual((v["p_used"], v["clears"]), (44.1, False))
        self.assertIn("and the bar is +2", v["why"])

    def test_the_bar_is_three_percent_of_a_big_chance(self):
        self.assertEqual(self.m.bar(80.0, {"novig_pct": 70.0, "price": -250})["need_pts"], 2.1)

    def test_only_sides_the_market_gives_30_to_75(self):
        for q, price in ((25.0, 290), (80.0, -420)):
            v = self.m.bar(99.0, {"novig_pct": q, "price": price})
            self.assertFalse(v["clears"], q)
            self.assertIn("between 30% and 75%", v["why"])

    def test_pinnacle_is_the_market_when_there_is_one(self):
        v = self.m.bar(50.0, {"novig_pct": 42.0, "sharp_pct": 47.0, "price": 150})
        self.assertEqual((v["q"], v["q_source"], v["p_used"]), (47.0, "Pinnacle", 48.2))
        self.assertFalse(v["clears"], "1.2 points over Pinnacle is not 2")

    def test_a_bet_under_the_bar_is_refused_with_its_number(self):
        code, said = self.act("bet", number="2", my_pct=44.0, event="iabc12")
        self.assertEqual(code, 1)
        self.assertIn("counts as 42.8%", said)
        self.assertIn("+0.8 pts, and the bar is +2", said)
        self.assertFalse(self.m.LEDGER.exists(), "nothing recorded")

    def test_a_chosen_stake_is_refused(self):
        code, said = self.act("bet", number="2", my_pct=52.0, stake=150, event="iabc12")
        self.assertEqual(code, 1)
        self.assertIn("flat 1% of the starting bankroll", said)

    def test_a_bet_without_news_is_refused_and_told_the_ids(self):
        code, said = self.act("bet", number="2", my_pct=52.0)
        self.assertEqual(code, 1)
        self.assertIn("This row has: iabc12 (FAV QB One, Out)", said)
        code, said = self.act("bet", number="2", my_pct=52.0, event="nope")
        self.assertIn("no fresh injury 'nope'", said)

    def test_news_the_price_already_has_is_refused(self):
        self.prices_seen(before=39.0)                          # 39.0 -> 42.0 since the report
        code, said = self.act("bet", number="2", my_pct=52.0, event="iabc12")
        self.assertEqual(code, 1)
        self.assertIn("already moved +3.0 pts toward this side", said)

    def test_no_price_before_the_news_is_refused(self):
        self.prices_seen(before=41.5, hours_ago=0.5)           # first seen after the report
        code, said = self.act("bet", number="2", my_pct=52.0, event="iabc12")
        self.assertEqual(code, 1)
        self.assertIn("no price was seen before the news", said)

    def test_old_news_is_refused(self):
        self.news(hours_ago=25.0)
        self.prices_seen(before=41.5, hours_ago=27.0)
        code, said = self.act("bet", number="2", my_pct=52.0, event="iabc12")
        self.assertEqual(code, 1)
        self.assertIn("is 25.0 hours old - news is 24 hours or less", said)
        # A day old but unpriced is fine (2026-10-02: the window was 6 hours).
        self.news(hours_ago=20.0)
        self.prices_seen(before=41.5, hours_ago=22.0)
        self.assertEqual(self.act("bet", number="2", my_pct=52.0, event="iabc12")[0], 0)

    def test_a_bet_that_clears_everything_is_booked_flat_by_the_book(self):
        code, said = self.act("bet", number="2", my_pct=52.0, event="iabc12", why="QB out, line still")
        self.assertEqual(code, 0, said)
        self.assertIn("counted as 46%, stake $100.00 flat, citing QB One (Out)", said)
        [bet] = self.bets()
        self.assertEqual((bet["stake"], bet["side"], bet["event"]["id"], bet["p_used"]),
                         (100.0, "away", "iabc12", 46.0))
        self.assertEqual(bet["kelly_shadow_stake"], self.m.kelly_stake(46.0, 150, 10000)[0])
        self.assertTrue(bet["price_source"].startswith("candidates.json at"))
        bank = json.loads((self.m.AGENT / "state" / "bankroll.json").read_text())
        self.assertEqual((bank["bankroll"], len(bank["open_bets"]), bank["cycle_count"]), (9900.0, 1, 3))
        self.assertEqual(self.rows()["DOG ML"]["stake"], 100.0)

    def test_one_bet_a_game(self):
        self.assertEqual(self.act("bet", number="2", my_pct=52.0, event="iabc12")[0], 0)
        code, said = self.act("bet", number="1", my_pct=75.0, event="iabc12")
        self.assertEqual(code, 1)
        self.assertIn("already a bet on this game (DOG ML, open)", said)

    def test_the_bet_is_booked_at_the_price_now(self):
        # candidates.json can be half an hour old; the bet is re-priced first.
        os.environ.pop("ACE_NO_LIVE_PRICE")
        fake = types.SimpleNamespace(
            summary_of=lambda sport, eid: {"called": (sport, eid)},
            odds_of=lambda summary: {"moneyline_away": 140, "novig_away_pct": 42.3} if summary["called"] == ("nfl", "401") else {})
        real = self.m._load
        self.m._load = lambda name, file: fake if file == "ace-fetch.py" and name == "ace_fetch_live" else real(name, file)
        code, said = self.act("bet", number="2", my_pct=52.0, event="iabc12")
        self.assertEqual(code, 0, said)
        bet = self.bets()[0]
        self.assertEqual((bet["price"], bet["novig_pct"]), (140, 42.3))
        self.assertTrue(bet["price_source"].startswith("live at"))

    def test_every_estimated_pass_carries_its_bar(self):
        self.act("passed", my_pct=59.0)
        row = self.rows()["FAV ML"]
        self.assertEqual((row["p_used"], row["gap_pts"], row["clears_bar"]), (58.4, 0.4, False))

    def test_the_header_quotes_the_code(self):
        head = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()
        self.assertIn(f"counts as only {1 - self.m.SHRINK:.0%} of its distance", head)
        self.assertIn(f"**at least {self.m.GAP_MIN_PTS:g} points** (or {self.m.GAP_MIN_REL:g}%", head)
        lo, hi = self.m.BET_RANGE
        self.assertIn(f"{lo:g}–{hi:g}%", head)
        self.assertIn("50% against a 42.8% market\n   counts as 45.7%", head)
        stake = load("ace_book_hdr", "ace-book.py")
        self.assertIn(f"every bet is ${10000 * stake.FLAT_STAKE_PCT / 100:,.0f}**", head)
        for gone in ("8+ percentage points", "Quarter-Kelly, capped", "+3% expected value", "--stake 150"):
            self.assertNotIn(gone, head)

    def test_the_deck_highlights_by_the_flag_code_wrote(self):
        page = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        self.assertNotIn("edge_pts >= 8", page)
        self.assertIn("c.clears_bar ? 'up'", page)
        self.assertIn("clears_bar: c.clears_bar === true",
                      (ROOT / "mission-control-api" / "ace.js").read_text())


class AceBankrollHoldsDataNotRules(unittest.TestCase):
    """2026-09-28: bankroll.json carried unit_pct, max_stake_pct, max_open_bets
    and a stop-loss - settings no code read. The rules are constants in code;
    `mark` strips the old keys. 2026-10-01: the -15% stop - which a bettor
    with no edge at 3% stakes hits about half the time - became a 30% fault
    stop in ace-book.py and an evidence stop on closing-line value."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load("ace_judge_bank", "ace-judge.py")
        agent = Path(self.tmp.name) / "ace"
        (agent / "state").mkdir(parents=True)
        (agent / "data").mkdir(parents=True)
        for name, val in (("AGENT", agent), ("CANDIDATES", agent / "data" / "candidates.json"),
                          ("LEDGER", agent / "state" / "ledger.json")):
            setattr(self.m, name, val)
        self.bank = agent / "state" / "bankroll.json"
        self.bank.write_text(json.dumps({"starting_bankroll": 10000.0, "bankroll": 10000.0}))

    def lose(self, n, stake=100.0):
        b = self.m.book()
        for i in range(n):
            b.append({"kind": "bet", "id": f"b{i}", "sport": "nfl", "event_id": str(i), "stake": stake})
            b.append({"kind": "settle", "id": f"b{i}", "result": "lost", "profit": -stake})
        return b

    def test_the_seed_holds_no_rules(self):
        seed = json.loads((ROOT / "agents" / "ace" / "state" / "bankroll.seed.json").read_text())
        for k in self.m.RETIRED_BANKROLL_KEYS:
            self.assertNotIn(k, seed)
        self.assertEqual(seed["starting_bankroll"], seed["bankroll"], "the stop measures from here")

    def test_the_fault_stop_is_30_percent_of_settled_losses(self):
        b = self.lose(29)
        self.assertIsNone(b.refusal("nfl", "999", "X ML"))
        b.append({"kind": "bet", "id": "b29", "sport": "nfl", "event_id": "29", "stake": 100.0})
        b.append({"kind": "settle", "id": "b29", "result": "lost", "profit": -100.0})
        self.assertIn("30% fault stop", b.refusal("nfl", "999", "X ML"))

    def test_the_evidence_stop(self):
        graded = lambda clv: {"graded": [{"status": "bet", "clv_pct": clv, "sharp_clv_pct": None}] * 60}
        self.m._clv = lambda: types.SimpleNamespace(report=lambda **k: graded(-0.1))
        self.assertIn("after 60 bets the average closing-line value is -0.10%", self.m.evidence_stop())
        self.m._clv = lambda: types.SimpleNamespace(report=lambda **k: graded(0.4))
        self.assertIsNone(self.m.evidence_stop())
        self.m._clv = lambda: types.SimpleNamespace(report=lambda **k: {"graded": graded(-5)["graded"][:59]})
        self.assertIsNone(self.m.evidence_stop(), "59 bets is too few to judge")

    def test_mark_strips_the_retired_keys_and_keeps_the_rest(self):
        self.bank.write_text(json.dumps({"bankroll": 9800.0, "starting_bankroll": 10000.0,
                                         "unit_pct": 1.5, "max_open_bets": 2, "stop_loss_floor": 8500.0,
                                         "open_bets": [{"id": 1}], "cycle_count": 4}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.cmd_mark(types.SimpleNamespace()), 0)
        b = json.loads(self.bank.read_text())
        for k in self.m.RETIRED_BANKROLL_KEYS:
            self.assertNotIn(k, b)
        self.assertEqual((b["bankroll"], b["open_bets"], b["cycle_count"]), (9800.0, [{"id": 1}], 5))
        self.assertIn("removed settings nothing read: unit_pct, max_open_bets, stop_loss_floor",
                      out.getvalue())

    def test_the_header_quotes_the_code(self):
        head = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()
        b = load("ace_book_stop", "ace-book.py")
        self.assertIn(f"settled bets down {b.FAULT_STOP_PCT:g}% of the start "
                      f"(${10000 * b.FAULT_STOP_PCT / 100:,.0f})", head)
        self.assertIn(f"after {self.m.EVIDENCE_MIN_BETS} bets, an average closing-line value at or below\n  zero", head)
        self.assertNotIn("FULL STOP", head)
        self.assertNotIn("Bankroll down 15%", head)


class AQueuedRunThatAskedAQuestionSaysSo(unittest.TestCase):
    """2026-09-28, task #30: Emily's only tool call was ask_user. Nobody
    answers a queued task, so she waited out the 600-second limit, openclaw
    exited 0, and the card said "agent exited with code 0" - which reads as
    a run that finished. The dispatcher now reads openclaw's own summary and
    says what happened; the wake message and her header say not to ask."""

    def setUp(self):
        self.d = load("dispatcher_why", "task-dispatcher.py")
        self.tail = (FIXTURES / "emily-ask-user-timeout.log").read_text()

    def test_the_real_tail_reads_as_a_question_nobody_answered(self):
        why = self.d.why_it_stopped(self.tail, 615)
        self.assertEqual(why, "stopped to ask a question (ask_user) and waited for an answer - "
                              "nobody answers a queued task, so it sat until the time limit "
                              "cut it off after 10 min, 1 tool call(s)")

    def test_any_other_tool_reads_as_cut_off_mid_step(self):
        why = self.d.why_it_stopped(self.tail.replace('"ask_user"', '"exec"'), 615)
        self.assertEqual(why, "cut off mid-step by the time limit after 10 min, 1 tool call(s)")

    def test_either_sign_alone_is_enough(self):
        # A tool call pending at the end, or openclaw calling the run paused:
        # each on its own means it was stopped, not finished.
        tool_use = self.tail.replace('"ask_user"', '"exec"').replace('"paused"', '"working"')
        self.assertIn("cut off mid-step", self.d.why_it_stopped(tool_use, 615))
        paused = self.tail.replace('"ask_user"', '"exec"').replace('"toolUse"', '"stop"')
        self.assertIn("cut off mid-step", self.d.why_it_stopped(paused, 615))

    def test_the_last_stop_reason_is_the_one_that_counts(self):
        # An earlier turn that ended on a tool call is normal; only how the
        # run ended matters.
        earlier = '"stopReason": "toolUse",\n'
        finished = self.tail.replace('"toolUse"', '"stop"').replace('"paused"', '"working"')
        self.assertIsNone(self.d.why_it_stopped(earlier + finished, 40))

    def test_a_run_that_finished_its_turn_gets_no_story(self):
        # Exited 0 at a normal stop without calling `done`: the agent's own
        # words say why, and inventing a timeout would be wrong.
        done = self.tail.replace('"toolUse"', '"stop"').replace('"paused"', '"working"')
        self.assertIsNone(self.d.why_it_stopped(done, 40))

    def test_output_with_no_summary_says_nothing(self):
        self.assertIsNone(self.d.why_it_stopped("", 600))
        self.assertIsNone(self.d.why_it_stopped("Request timed out before a response.", 600))

    def test_the_card_gets_it(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(self.d.SCHEMA)
        conn.execute("INSERT INTO tasks (id, status, assignee) VALUES (30, 'in_progress', 'emily')")
        tail = self.tail.encode()

        class Proc:
            returncode = 0
            def poll(self): return 0
            def communicate(self, timeout=None): return tail, b""

        self.d.log = lambda msg: None
        self.d.clear_task_marker = lambda agent: None
        self.d.running.clear()
        self.d.running["emily"] = {"proc": Proc(), "task_id": 30, "started": time.time() - 615}
        self.d.reap_finished(conn)
        row = conn.execute("SELECT status, result FROM tasks WHERE id = 30").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["result"].startswith("stopped to ask a question (ask_user)"), row["result"])
        self.assertTrue(row["result"].endswith("(exit code 0)"), row["result"])

    def test_the_agent_is_told_before_it_happens(self):
        t = load("task_py_ask", "task.py")
        self.assertIn("Never ask the user anything (no ask_user)", t.wake_message(30))
        head = (ROOT / "agents" / "emily" / "_emily-agents-header.md").read_text()
        self.assertIn("**Never ask the user\nanything**", head)


class BelfortsSellRulesAreCode(unittest.TestCase):
    """"Make the script flag or force the sells that are due ... this rule
    seems it hasn't been enforced: up 8%, don't let it fall back below what he
    paid" (2026-10-01).

    The exit rules were instructions only. "+8%" could not be followed at all:
    nothing remembered how high a position had been. The fetcher now keeps
    each name's daily closes; exit_check reads the highest since entry."""

    TSM = json.loads((FIXTURES / "belfort-yahoo-chart-tsm.json").read_text())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        b = self.root / "agents" / "belfort"
        (b / "state").mkdir(parents=True)
        (b / "data").mkdir(parents=True)
        self.state = b / "state" / "portfolio.json"
        self.quotes = b / "data" / "quotes.json"

    def q(self, price, closes, start="2026-09-21"):
        """A quote with daily closes from `start`, one a trading day."""
        day = datetime.strptime(start, "%Y-%m-%d")
        hist = []
        for c in closes:
            while day.weekday() >= 5:
                day += timedelta(days=1)
            hist.append([day.strftime("%Y-%m-%d"), c])
            day += timedelta(days=1)
        return {"price": price, "history": hist}

    def pos(self, cost=100.0, entry="2026-09-21 13:36:00"):
        return {"symbol": "X", "shares": 10, "cost_basis": cost, "entry_utc": entry}

    def test_stop_loss_and_take_profit(self):
        self.assertEqual(trade.exit_check(self.pos(), self.q(90.0, [100, 95]))[0], "stop")
        self.assertIsNone(trade.exit_check(self.pos(), self.q(90.1, [100, 95]))[0])
        self.assertEqual(trade.exit_check(self.pos(), self.q(125.0, [110, 120]))[0], "take")

    def test_up_8_percent_then_back_to_cost_is_sold(self):
        # Never below what was paid, once it has been up 8%.
        been_up = self.q(100.0, [104, 108.5, 103])
        rule, reason, stop = trade.exit_check(self.pos(), been_up)
        self.assertEqual((rule, stop), ("protect", 100.0))
        self.assertIn("was up +8.5%", reason)
        # the same price, never up 8%: an ordinary position, stop at -10%
        never = self.q(100.0, [104, 107.9, 103])
        self.assertEqual(trade.exit_check(self.pos(), never)[:3:2], (None, 90.0))

    def test_the_protected_stop_follows_the_high(self):
        # Up 20% at the high: the stop is 10% under it, +8% over cost - a gain kept.
        rule, _, stop = trade.exit_check(self.pos(), self.q(110.0, [110, 120, 112]))
        self.assertEqual((rule, round(stop, 2)), (None, 108.0))
        self.assertEqual(trade.exit_check(self.pos(), self.q(107.9, [110, 120, 112]))[0], "protect")

    def test_a_high_before_buying_does_not_count(self):
        q = self.q(101.0, [130, 125, 101], start="2026-09-17")      # 130 was the 17th
        self.assertIsNone(trade.exit_check(self.pos(entry="2026-09-21 13:36:00"), q)[0])

    def test_dead_money_counts_trading_days_after_entry(self):
        # 8 trading days, and behind QQQ over them (2026-10-01: was 5 days, no QQQ)
        flat = [100.5, 99.8, 100.2, 100.9, 101.0, 100.4, 100.1, 99.9, 100.4]   # entry day + 8
        qqq = self.q(515.0, [500.0] * 9)                                        # QQQ +3% since
        rule, reason, _ = trade.exit_check(self.pos(), self.q(100.4, flat), qqq)
        self.assertEqual(rule, "dead")
        self.assertIn("after 8 trading days, while QQQ moved +3.0%", reason)
        self.assertIsNone(trade.exit_check(self.pos(), self.q(100.4, flat[:8]), qqq)[0], "7 days is not 8")
        moving = flat[:8] + [103.0]
        self.assertIsNone(trade.exit_check(self.pos(), self.q(103.0, moving), qqq)[0])

    def test_flat_in_a_flat_market_is_not_dead(self):
        flat = [100.5, 99.8, 100.2, 100.9, 101.0, 100.4, 100.1, 99.9, 100.4]
        self.assertIsNone(trade.exit_check(self.pos(), self.q(100.4, flat), self.q(500.0, [500.0] * 9))[0],
                          "QQQ flat too: not behind it")
        self.assertIsNone(trade.exit_check(self.pos(), self.q(100.4, flat))[0], "no QQQ: not judged dead")
        # QQQ's move is measured from the entry day, not from its oldest close
        early = self.q(515.0, [400.0] + [515.0] * 9, start="2026-09-18")
        self.assertIsNone(trade.exit_check(self.pos(), self.q(100.4, flat), early)[0])

    def test_real_closes_from_the_fetcher(self):
        """TSM's real daily closes, through the real fetcher's parser."""
        fetch = load("belfort_fetch_hist", "belfort-fetch.py")
        fetch.http_get = lambda url, retries=2: json.dumps(self.TSM).encode()
        q = fetch.fetch_one("TSM")
        self.assertEqual(len(q["history"]), fetch.HISTORY_BARS)
        self.assertEqual(q["history"][-1][1], q["price"])
        entry = q["history"][-8][0] + " 13:36:00"
        # bought a week ago at 9% under today's price: it has been up 8%+
        rule, reason, stop = trade.exit_check({"symbol": "TSM", "shares": 4, "entry_utc": entry,
                                               "cost_basis": round(q["price"] / 1.09, 2)}, q)
        self.assertGreaterEqual(stop, round(q["price"] / 1.09, 2))

    def run_trade(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), *args],
                              capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))

    def test_exits_apply_sells_what_is_due_and_the_book_still_balances(self):
        p = {"starting_cash": 10000.0, "cash": 7000.0, "cycle_count": 3,
             "trades": [{"side": "BUY", "symbol": "LOSR", "shares": 10, "price": 100, "notional": 1000},
                        {"side": "BUY", "symbol": "HOLD", "shares": 10, "price": 100, "notional": 1000},
                        {"side": "BUY", "symbol": "WINR", "shares": 10, "price": 100, "notional": 1000}],
             "positions": [dict(self.pos(), symbol=s) for s in ("LOSR", "HOLD", "WINR")]}
        self.state.write_text(json.dumps(p))
        self.quotes.write_text(json.dumps({"quotes": {
            "LOSR": self.q(88.0, [95, 90]), "HOLD": self.q(104.0, [101, 104]),
            "WINR": self.q(100.0, [105, 111, 102])}}))
        r = self.run_trade("exits")
        self.assertIn("DUE LOSR: STOP LOSS", r.stdout)
        self.assertIn("DUE WINR: PROTECT GAIN", r.stdout)
        self.assertNotIn("HOLD", r.stdout)
        self.assertEqual(json.loads(self.state.read_text())["cash"], 7000.0, "without --apply nothing is sold")
        r = self.run_trade("exits", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = json.loads(self.state.read_text())
        self.assertEqual([x["symbol"] for x in after["positions"]], ["HOLD"])
        self.assertEqual(after["cash"], 7000.0 + 880.0 + 1000.0)
        self.assertEqual(trade.book_gap(after)[0], 0)
        self.assertTrue(after["trades"][-1]["reason"].startswith("PROTECT GAIN"))
        self.assertIn("nothing due", self.run_trade("exits", "--apply").stdout)

    def test_the_cash_floor_is_5_percent(self):
        self.state.write_text(json.dumps({"starting_cash": 10000.0, "cash": 10000.0, "positions": [], "trades": []}))
        self.quotes.write_text(json.dumps({"quotes": {"A": {"price": 100.0}, "B": {"price": 100.0},
                                                      "C": {"price": 100.0}, "D": {"price": 100.0}},
                                           "benchmarks": {"QQQ": rising_qqq()}}))
        (self.quotes.parent / "news.json").write_text(json.dumps(fresh_news(*"ABCD")))
        for sym in "ABC":
            self.assertEqual(self.run_trade("buy", sym, "24", "--headline", f"h-{sym}").returncode, 0)
        r = self.run_trade("buy", "D", "22", "--headline", "h-D")   # would leave 6% cash
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_trade("buy", "D", "2", "--headline", "h-D")    # 4%
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("minimum is 5%", r.stderr)

    def test_the_cycle_runs_the_exits_before_belfort_wakes(self):
        src = (SCRIPTS / "belfort-cycle.sh").read_text()
        self.assertLess(src.index("belfort-trade.py\" exits --apply"), src.index("openclaw agent --agent belfort"))
        self.assertIn("$EXITS", src.split("openclaw agent --agent belfort", 1)[1].split("--session-id")[0])


class BelfortReadsNewsForAllThirty(unittest.TestCase):
    """News came from one request for the first 12 of 30 names, which Yahoo
    answers with 20 headlines between them - CRWD and PANW, both held, had
    none. Now one request a name, up to 6 each, tagged, hourly."""

    RSS = (FIXTURES / "belfort-yahoo-news-crwd.xml").read_bytes()

    def setUp(self):
        self.fetch = load("belfort_fetch_news", "belfort-fetch.py")

    def test_the_real_feed_parses(self):
        items = self.fetch.parse_rss(self.RSS.decode())
        self.assertEqual(len(items), 20)
        self.assertEqual(items[0], {"title": "headline 1", "published": "Thu, 01 Oct 2026 03:26:17 +0000",
                                    "id": "a649c928", "publisher": "example.invalid"})

    def test_every_name_is_asked_for_and_tagged(self):
        asked = []

        def get(url):
            asked.append(url)
            return self.RSS
        news = self.fetch.fetch_news(["CRWD", "PANW"], get=get, gap=0)
        self.assertEqual(len(asked), 2)
        self.assertIn("s=PANW", asked[1])
        self.assertEqual([n["symbol"] for n in news], ["CRWD"] * self.fetch.NEWS_PER_NAME,
                         "the same headline under two names is kept once")
        self.assertIn("PANW", [s for s in self.fetch.UNIVERSE[12:]], "a name past the old first 12")

    def test_a_failed_name_does_not_lose_the_rest(self):
        def get(url):
            if "s=CRWD" in url:
                raise OSError("timed out")
            return self.RSS
        news = self.fetch.fetch_news(["CRWD", "PANW"], get=get, gap=0)
        self.assertEqual({n["symbol"] for n in news}, {"PANW"})

    def test_hourly_not_every_ten_minutes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "news.json"
            stamp = lambda ago: datetime.fromtimestamp(1790800000 - ago, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            f.write_text(json.dumps({"asof_utc": stamp(600), "symbols": self.fetch.UNIVERSE, "headlines": []}))
            self.assertTrue(self.fetch.news_is_fresh(f, now=1790800000))
            f.write_text(json.dumps({"asof_utc": stamp(3600), "symbols": self.fetch.UNIVERSE, "headlines": []}))
            self.assertFalse(self.fetch.news_is_fresh(f, now=1790800000))
            # last fetched for the old 12 only: fetch all 30 now, however recent
            f.write_text(json.dumps({"asof_utc": stamp(60), "headlines": []}))
            self.assertFalse(self.fetch.news_is_fresh(f, now=1790800000))


class BelfortsSellRulesNeedAnEntryDate(unittest.TestCase):
    """A position with no entry date would read 90 days of closes as its
    own: a high from before it was bought could sell it as "protect a gain"."""

    def test_no_date_only_the_plain_stops(self):
        q = {"price": 100.0, "history": [["2026-08-03", 130.0], ["2026-09-30", 101.0]]}
        self.assertEqual(trade.exit_check({"symbol": "X", "shares": 1, "cost_basis": 100.0}, q)[:3:2],
                         (None, 90.0))

    def test_the_buy_trade_gives_the_date(self):
        p = {"trades": [{"side": "BUY", "symbol": "X", "utc": "2026-09-29 13:36:00"}]}
        pos = trade.entry_of(p, {"symbol": "X", "shares": 1, "cost_basis": 100.0})
        self.assertEqual(pos["entry_utc"], "2026-09-29 13:36:00")


class BelfortGuardrailsFromTheReview(unittest.TestCase):
    """2026-10-01, from an outside review of Belfort's rules: a market regime
    filter, caps on clusters of related names, catalysts tied to a real
    headline, a no-AI shadow book, and the numbers to compare them by."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        b = self.root / "agents" / "belfort"
        (b / "state").mkdir(parents=True)
        (b / "data").mkdir(parents=True)
        self.state, self.data = b / "state", b / "data"
        self.book({"starting_cash": 10000.0, "cash": 10000.0, "positions": [], "trades": [],
                   "created_utc": "2026-07-10 14:00:00"})
        self.prices(dict.fromkeys(["NVDA", "AMD", "MU", "PANW", "A", "B", "C", "D", "E", "F", "G", "H", "I"], 100.0))
        (self.data / "news.json").write_text(json.dumps(fresh_news("NVDA", "AMD", "MU", "PANW")))
        self.old = os.environ.get("ECOSYSTEM_ROOT")

    def book(self, p, name="portfolio.json"):
        (self.state / name).write_text(json.dumps(p))

    def prices(self, px, qqq=None, extra=None):
        quotes = {k: dict({"price": v, "sma50": v * 0.95}, **(extra or {}).get(k, {})) for k, v in px.items()}
        (self.data / "quotes.json").write_text(json.dumps(
            {"quotes": quotes, "benchmarks": {"QQQ": qqq if qqq is not None else rising_qqq()}}))

    def run_trade(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), *args],
                              capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))

    def mod(self):
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(lambda: os.environ.pop("ECOSYSTEM_ROOT", None) if self.old is None
                        else os.environ.__setitem__("ECOSYSTEM_ROOT", self.old))
        return load("belfort_trade_guard", "belfort-trade.py")

    # --- 1. the market regime ------------------------------------------------

    def qqq(self, closes, price):
        return {"price": price, "history": [[f"2026-07-{1 + i // 3:02d}", c] for i, c in enumerate(closes)]}

    def test_regime_states(self):
        t = self.mod()
        rising, falling = [400.0 + i for i in range(70)], [470.0 - i for i in range(70)]
        names = {"quotes": {s: {"price": 100.0, "sma50": 95.0} for s in "ABCD"}}
        self.assertEqual(t.regime(dict(names, benchmarks={"QQQ": self.qqq(rising, 500)}))[0], "favorable")
        self.assertEqual(t.regime(dict(names, benchmarks={"QQQ": self.qqq(rising, 440)}))[0], "neutral",
                         "below a rising average: mixed")
        self.assertEqual(t.regime(dict(names, benchmarks={"QQQ": self.qqq(falling, 380)}))[0], "unfavorable")
        weak = {"quotes": {s: {"price": 90.0, "sma50": 95.0} for s in "ABC"} | {"D": {"price": 100.0, "sma50": 95.0}}}
        self.assertEqual(t.regime(dict(weak, benchmarks={"QQQ": self.qqq(rising, 500)}))[0], "unfavorable",
                         "QQQ fine but only a quarter of the names above their average")
        self.assertEqual(t.regime(names)[0], "unknown")

    def test_unfavourable_means_no_buys_and_mixed_means_small_ones(self):
        self.prices({"NVDA": 100.0}, qqq=self.qqq([470.0 - i for i in range(70)], 380))
        r = self.run_trade("buy", "NVDA", "5", "--headline", "h-NVDA")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("regime is unfavourable", r.stderr)
        self.prices({"NVDA": 100.0}, qqq=self.qqq([400.0 + i for i in range(70)], 440))
        r = self.run_trade("buy", "NVDA", "11", "--headline", "h-NVDA")
        self.assertIn("cap is 10% while the regime is neutral", r.stderr)
        self.assertEqual(self.run_trade("buy", "NVDA", "9", "--headline", "h-NVDA").returncode, 0)

    def test_no_qqq_data_is_treated_as_mixed(self):
        (self.data / "quotes.json").write_text(json.dumps({"quotes": {"NVDA": {"price": 100.0}}}))
        r = self.run_trade("buy", "NVDA", "20", "--headline", "h-NVDA")
        self.assertIn("cap is 10% while the regime is unknown", r.stderr)

    def test_the_real_fetcher_supplies_the_benchmarks(self):
        chart = (FIXTURES / "belfort-yahoo-chart-tsm.json").read_bytes()
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        try:
            fetch = load("belfort_fetch_bench", "belfort-fetch.py")
        finally:
            os.environ.pop("ECOSYSTEM_ROOT", None) if self.old is None else os.environ.__setitem__("ECOSYSTEM_ROOT", self.old)
        fetch.http_get = lambda url, retries=2: chart
        fetch.REQUEST_GAP = 0
        fetch.fetch_news.__defaults__ = (None, 0)
        fetch.fetch_earnings = lambda symbols, *a, **k: {"TSM": "2026-10-15"}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fetch.main(), 0)
        self.assertEqual(json.loads((self.data / "earnings.json").read_text())["dates"], {"TSM": "2026-10-15"})
        doc = json.loads((self.data / "quotes.json").read_text())
        self.assertEqual(sorted(doc["benchmarks"]), ["QQQ", "SPY"])
        self.assertEqual(len(doc["benchmarks"]["QQQ"]["history"]), fetch.HISTORY_BARS)
        self.assertIn(self.mod().regime(doc)[0], ("favorable", "neutral", "unfavorable"))

    # --- 2. clusters ------------------------------------------------------------

    def test_one_cluster_is_capped_at_40_percent(self):
        for sym, n in (("NVDA", "20"), ("AMD", "20")):
            self.assertEqual(self.run_trade("buy", sym, n, "--headline", f"h-{sym}").returncode, 0)
        r = self.run_trade("buy", "MU", "5", "--headline", "h-MU")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("semiconductors would be 45.0%", r.stderr)
        self.assertEqual(self.run_trade("buy", "PANW", "20", "--headline", "h-PANW").returncode, 0,
                         "another cluster is fine")
        self.assertIn("semiconductors 40%", self.run_trade("show").stdout)

    def test_an_unlisted_name_is_its_own_cluster_and_eight_is_the_most(self):
        t = self.mod()
        self.assertEqual(t.cluster_of("A"), "A")
        p = {"starting_cash": 10000.0, "cash": 10000.0, "positions": [], "trades": []}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            for sym in "ABCDEFGH":
                self.assertEqual(t.buy(p, sym, 10, state=("favorable", "")), 0)
            self.assertEqual(t.buy(p, "I", 10, state=("favorable", "")), 1)
        self.assertIn("8 names held, the most is 8", err.getvalue())

    # --- 5. the catalyst is a real headline -----------------------------------

    def test_a_buy_cites_a_headline_from_its_own_news(self):
        self.assertIn("cites its catalyst", self.run_trade("buy", "NVDA", "5").stderr)
        self.assertIn("no headline 'nope'", self.run_trade("buy", "NVDA", "5", "--headline", "nope").stderr)
        self.assertIn("from AMD's news, not NVDA's",
                      self.run_trade("buy", "NVDA", "5", "--headline", "h-AMD").stderr)
        r = self.run_trade("buy", "NVDA", "5", "--headline", "h-NVDA", "--reason", "EARNINGS: guidance raised")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = json.loads((self.state / "portfolio.json").read_text())
        self.assertEqual(p["trades"][-1]["catalyst"]["title"], "EARNINGS: NVDA raised guidance")
        self.assertEqual(p["trades"][-1]["catalyst"]["publisher"], "example.com")
        self.assertEqual(p["positions"][0]["catalyst"]["id"], "h-NVDA")

    def test_an_old_headline_is_not_a_catalyst(self):
        from email.utils import format_datetime
        news = fresh_news("NVDA")
        news["headlines"][0]["published"] = format_datetime(datetime.now(timezone.utc) - timedelta(days=8))
        (self.data / "news.json").write_text(json.dumps(news))
        self.assertIn("is 8 days old", self.run_trade("buy", "NVDA", "5", "--headline", "h-NVDA").stderr)

    # --- 3. the shadow book --------------------------------------------------

    def candidates(self, *syms):
        (self.data / "candidates.json").write_text(json.dumps(
            {"candidates": [{"symbol": s, "price": 100.0} for s in syms]}))

    def test_the_shadow_book_buys_the_top_candidate_it_may(self):
        self.candidates("NVDA", "AMD", "PANW")
        self.book({"starting_cash": 10000.0, "cash": 5800.0, "trades": [], "created_utc": "2026-09-01 14:00:00",
                   "positions": [{"symbol": "NVDA", "shares": 21, "cost_basis": 100.0},
                                 {"symbol": "AMD", "shares": 21, "cost_basis": 100.0}]}, "shadow.json")
        r = self.run_trade("shadow")
        self.assertEqual(r.returncode, 0, r.stderr)
        # NVDA held; AMD would take semiconductors past 40%; PANW is next
        self.assertIn("bought 20 PANW (favorable)", r.stdout)
        self.assertEqual(json.loads((self.state / "portfolio.json").read_text())["positions"], [],
                         "Belfort's own book is not touched")
        self.assertIn("no buy", self.run_trade("shadow").stdout,
                      "one buy a turn, and PANW is now held")

    def test_the_shadow_book_never_adds_to_a_name_it_holds(self):
        # Like Belfort: one new name at a time, no averaging up. A 5% PANW
        # could take 20% more and stay inside every cap - it still may not.
        self.candidates("PANW")
        self.book({"starting_cash": 10000.0, "cash": 9500.0, "trades": [], "created_utc": "2026-09-01 14:00:00",
                   "positions": [{"symbol": "PANW", "shares": 5, "cost_basis": 100.0}]}, "shadow.json")
        self.assertIn("no buy", self.run_trade("shadow").stdout)

    def test_the_shadow_book_starts_itself_and_obeys_the_regime(self):
        self.candidates("PANW")
        self.prices({"PANW": 100.0}, qqq=self.qqq([470.0 - i for i in range(70)], 380))
        r = self.run_trade("shadow")
        self.assertIn("no buy: regime unfavourable", r.stdout)
        shadow = json.loads((self.state / "shadow.json").read_text())
        self.assertEqual((shadow["starting_cash"], shadow["cash"], shadow["cycle_count"]), (10000.0, 10000.0, 1))

    def test_the_shadow_book_sells_by_the_same_exit_rules(self):
        self.candidates()
        self.book({"starting_cash": 10000.0, "cash": 9000.0, "created_utc": "2026-09-01 14:00:00",
                   "trades": [{"side": "BUY", "symbol": "PANW", "shares": 10, "price": 100, "notional": 1000}],
                   "positions": [{"symbol": "PANW", "shares": 10, "cost_basis": 112.0,
                                  "entry_utc": "2026-09-01 14:00:00"}]}, "shadow.json")
        r = self.run_trade("shadow")
        self.assertIn("sold PANW: STOP LOSS", r.stdout)

    # --- 4. the numbers --------------------------------------------------------

    def test_stats_for_both_books_against_qqq(self):
        sells = [("WIN", 1100.0, 100.0, "TAKE PROFIT: +25%"), ("WIN2", 1050.0, 50.0, "Broken thesis"),
                 ("LOSS", 900.0, -100.0, "STOP LOSS: -10%")]
        self.book({"starting_cash": 10000.0, "cash": 10050.0, "positions": [], "created_utc": "2026-07-10 14:00:00",
                   "trades": [{"side": "SELL", "symbol": s, "notional": n, "realised_pnl": r, "reason": why}
                              for s, n, r, why in sells]})
        (self.state / "equity.jsonl").write_text("".join(json.dumps({"book": "belfort", "value": v}) + "\n"
                                                         for v in (10000, 10400, 9880, 10100)))
        t = self.mod()
        st = t.book_stats(t.load(), "belfort")
        self.assertEqual((st["closed"], st["win_rate"]), (3, 66.7))
        self.assertEqual((st["avg_win_pct"], st["avg_loss_pct"]), (7.5, -10.0))
        self.assertEqual((st["profit_factor"], st["expectancy_pct"]), (1.5, 1.67))
        self.assertEqual(st["max_drawdown_pct"], 5.0, "10,400 down to 9,880")
        self.assertEqual(st["exits_by_rule"], {"stop loss": 1, "take profit": 1, "protect a gain": 0,
                                               "dead money": 0, "judgement": 1})
        # QQQ from the book's first day: rising_qqq's 2026-07-10 close is 427, now 500
        self.assertEqual(st["qqq_return_pct"], round((500 / 427 - 1) * 100, 2))
        out = self.run_trade("stats").stdout
        self.assertIn("BELFORT (AI)", out)
        self.assertNotIn("SHADOW", out, "no shadow book yet, none shown")

    def test_mark_keeps_the_value_history(self):
        self.run_trade("mark")
        self.run_trade("mark")
        rows = [json.loads(l) for l in (self.state / "equity.jsonl").read_text().splitlines()]
        self.assertEqual([r["book"] for r in rows], ["belfort", "belfort"])

    def test_the_cycle_runs_the_shadow_book_and_does_not_tell_belfort(self):
        src = (SCRIPTS / "belfort-cycle.sh").read_text()
        self.assertLess(src.index("belfort-trade.py\" shadow"), src.index("openclaw agent --agent belfort"))
        message = src.split("openclaw agent --agent belfort", 1)[1].split("--session-id")[0]
        self.assertNotIn("SHADOW", message)


class TheNoAIBookIsItsOwnMoney(unittest.TestCase):
    """"Put the no AI stats in Belfort's house on a separate page and make sure
    that 10,000 is separate from his" (2026-10-01)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents" / "belfort" / "state"
        self.state.mkdir(parents=True)
        (self.root / "agents" / "belfort" / "data").mkdir()
        (self.root / "agents" / "belfort" / "data" / "quotes.json").write_text(json.dumps(
            {"quotes": {"MRVL": {"price": 110.0}, "PANW": {"price": 90.0}}, "benchmarks": {"QQQ": rising_qqq()}}))
        (self.state / "portfolio.json").write_text(json.dumps({
            "starting_cash": 10000.0, "cash": 9000.0, "created_utc": "2026-07-10 14:00:00",
            "positions": [{"symbol": "MRVL", "shares": 10, "cost_basis": 100.0}], "trades": []}))
        (self.state / "shadow.json").write_text(json.dumps({
            "starting_cash": 10000.0, "cash": 8000.0, "created_utc": "2026-10-01 13:40:00",
            "positions": [{"symbol": "PANW", "shares": 20, "cost_basis": 100.0}], "trades": []}))
        (self.state / "equity.jsonl").write_text(
            json.dumps({"utc": "2026-09-30 19:56:00", "book": "belfort", "value": 9000}) + "\n"
            + json.dumps({"utc": "2026-10-01 13:41:00", "book": "belfort", "value": 10000}) + "\n")

    def books(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), "stats", "--json"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_each_page_gets_its_own_book(self):
        d = self.books()
        mine, shadow = (next(b for b in d["books"] if b["book"] == k) for k in ("belfort", "shadow"))
        self.assertEqual((mine["value"], mine["cash"], [p["symbol"] for p in mine["positions"]]),
                         (10100.0, 9000.0, ["MRVL"]))
        self.assertEqual((shadow["value"], shadow["cash"], [p["symbol"] for p in shadow["positions"]]),
                         (9800.0, 8000.0, ["PANW"]))

    def test_side_by_side_is_over_the_same_days(self):
        # Belfort's own total runs from July; the shadow book's from today.
        # Compared from the shadow book's start: 10,000 then, 10,100 now.
        d = self.books()["same_days"]
        self.assertEqual((d["since"], d["belfort_pct"], d["shadow_pct"]), ("2026-10-01", 1.0, -2.0))

    def test_the_deck_never_counts_it(self):
        # dashboard-data.js reads portfolio.json, falling back to a file whose
        # name looks like a portfolio. shadow.json must never be that file.
        js = (ROOT / "mission-control-api" / "dashboard-data.js").read_text()
        pattern = re.search(r"find\(f => /(.+?)/i\.test\(f\)", js).group(1)
        self.assertIsNone(re.search(pattern, "shadow.json", re.I))
        self.assertIn("'portfolio.json'", js)
        self.assertNotIn("shadow", js)

    def test_the_house_has_the_two_pages(self):
        # Since 2026-10-06 Belfort's house opens the Markets page, and both
        # books live on its Books tab.
        village = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        self.assertIn("if (name === 'belfort') return openMarkets();", village)
        html = (ROOT / "mission-control-api" / "public" / "markets.html").read_text()
        self.assertIn('data-t="books">Books</button>', html)
        self.assertIn("separate $10,000 paper account</b>, not Belfort's money", html)
        self.assertIn("/api/belfort/books", html)
        self.assertIn("belfortRoutes.register(app)", (ROOT / "mission-control-api" / "server.js").read_text())


class BelfortStopsSizesAndEarnings(unittest.TestCase):
    """"Do all 8, 7 and 6 ... and add another check at 12:35" (2026-10-01),
    from the outside review: a stop and a size from each name's own
    volatility, no buys into earnings, a gentler dead-money rule, and a
    midday run of the sell rules between the 9:35 and 3:55 wakes."""

    TSM = (FIXTURES / "belfort-yahoo-chart-tsm.json").read_bytes()
    NASDAQ = (FIXTURES / "belfort-nasdaq-earnings-2026-10-21.json").read_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        b = self.root / "agents" / "belfort"
        (b / "state").mkdir(parents=True)
        (b / "data").mkdir(parents=True)
        self.state, self.data = b / "state", b / "data"
        self.book({"starting_cash": 10000.0, "cash": 10000.0, "positions": [], "trades": [],
                   "created_utc": "2026-07-10 14:00:00"})
        self.prices({"NVDA": (100.0, 5.0), "AMD": (100.0, None), "MU": (100.0, None), "PANW": (100.0, 1.0)})
        (self.data / "news.json").write_text(json.dumps(fresh_news("NVDA", "AMD", "MU", "PANW")))

    def book(self, p, name="portfolio.json"):
        (self.state / name).write_text(json.dumps(p))

    def prices(self, px, qqq=None):
        """{symbol: (price, atr14 or None)}"""
        quotes = {k: dict({"price": v, "sma50": v * 0.95}, **({"atr14": a} if a else {}))
                  for k, (v, a) in px.items()}
        (self.data / "quotes.json").write_text(json.dumps(
            {"quotes": quotes, "benchmarks": {"QQQ": qqq or rising_qqq()}}))

    def earnings(self, **dates):
        (self.data / "earnings.json").write_text(json.dumps(
            {"asof_utc": "2026-10-01 14:00:00", "source": "test", "dates": dates}))

    def run_trade(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), *args],
                              capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))

    def mod(self):
        old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        try:
            return load("belfort_trade_risk", "belfort-trade.py")
        finally:
            os.environ.pop("ECOSYSTEM_ROOT", None) if old is None else os.environ.__setitem__("ECOSYSTEM_ROOT", old)

    def saved(self, name="portfolio.json"):
        return json.loads((self.state / name).read_text())

    # --- 6. stops and sizes from volatility ------------------------------------

    def test_atr_from_the_real_chart(self):
        fetch = load("belfort_fetch_atr", "belfort-fetch.py")
        fetch.http_get = lambda url, retries=2: self.TSM
        q = fetch.fetch_one("TSM")
        bars = json.loads(self.TSM)["chart"]["result"][0]["indicators"]["quote"][0]
        h, l, c = (bars[k][-15:] for k in ("high", "low", "close"))
        want = sum(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, 15)) / 14
        self.assertEqual(q["atr14"], round(want, 2))
        self.assertEqual(q["atr_pct"], round(want / q["price"] * 100, 2))
        self.assertGreater(want, max(h[i] - l[i] for i in range(1, 15)) / 14, "a real range, not one day's")

    def test_the_stop_distance_is_twice_the_daily_move_kept_between_5_and_15(self):
        self.assertEqual(trade.risk_pct({"price": 41.12, "atr14": 2.27}), 11.04)    # SMCI-like: wild
        self.assertEqual(trade.risk_pct({"price": 513.0, "atr14": 12.17}), 5.0)     # MSFT-like: 4.7%, floored
        self.assertEqual(trade.risk_pct({"price": 100.0, "atr14": 9.0}), 15.0)      # 18%, capped
        self.assertIsNone(trade.risk_pct({"price": 100.0}), "no ATR yet: no volatility stop")

    def test_a_position_is_stopped_at_its_own_distance(self):
        pos = {"symbol": "X", "shares": 10, "cost_basis": 100.0, "entry_utc": "2026-09-21 13:36:00",
               "risk_pct": 6.0}
        q = lambda px, closes: {"price": px, "history": [[f"2026-09-{21 + i}", c] for i, c in enumerate(closes)]}
        self.assertEqual(trade.exit_check(pos, q(94.5, [100, 97]))[0], None, "a -10% stop would be wrong too")
        rule, reason, stop = trade.exit_check(pos, q(94.0, [100, 97]))
        self.assertEqual((rule, stop), ("stop", 94.0))
        self.assertIn("(its stop: -6%)", reason)
        # protecting a gain: the same 6% under the high, not 10%
        self.assertEqual(trade.exit_check(pos, q(115.0, [110, 120, 115]))[2], 112.8)
        self.assertEqual(trade.exit_check(pos, q(112.8, [110, 120, 115]))[0], "protect")
        # bought before the rule: no risk_pct, the old -10%
        del pos["risk_pct"]
        self.assertEqual(trade.exit_check(pos, q(94.0, [100, 97]))[:3:2], (None, 90.0))

    def test_a_buy_may_risk_at_most_1_percent(self):
        # NVDA at $100, ATR $5: stop -10%, so $100 of risk is 10 shares
        r = self.run_trade("buy", "NVDA", "11", "--headline", "h-NVDA")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("would lose $110.00 at its stop (-10%", r.stderr)
        self.assertIn("$100.00: 10 shares", r.stderr)
        r = self.run_trade("buy", "NVDA", "10", "--headline", "h-NVDA")
        self.assertEqual(r.returncode, 0, r.stderr)
        pos = self.saved()["positions"][0]
        self.assertEqual(pos["risk_pct"], 10.0, "the stop is stored with the position, kept as bought")

    def test_size_is_the_most_buy_allows(self):
        # One fact, one place: whatever `size` says, buy takes it and refuses one more.
        cases = [("NVDA", 10, "1% risk at a -10% stop"),      # wild: risk-limited
                 ("PANW", 20, "1% risk at a -5% stop"),       # calm: the 5% floor - 20% at most
                 ("AMD", 25, "position cap")]                 # no ATR yet: the caps only
        for sym, n, limit in cases:
            out = self.run_trade("size", sym).stdout
            self.assertIn(f"{n} shares of {sym} at $100.00, held to the {limit}", out)
            self.assertNotEqual(self.run_trade("buy", sym, str(n + 1), "--headline", f"h-{sym}").returncode, 0)
        self.assertIn("stop -5%", self.run_trade("size", "PANW").stdout, "1% daily move: floored at 5%")

    def test_size_counts_what_is_held_and_the_mixed_market_cap(self):
        # 15 AMD held: the 25% cap leaves room for 10 more
        self.book({"starting_cash": 10000.0, "cash": 8500.0, "trades": [], "created_utc": "2026-07-10 14:00:00",
                   "positions": [{"symbol": "AMD", "shares": 15, "cost_basis": 100.0}]})
        self.assertIn("10 shares of AMD at $100.00, held to the position cap", self.run_trade("size", "AMD").stdout)
        # QQQ under a rising average: mixed, 10% a name
        self.prices({"AMD": (100.0, None), "MU": (100.0, None)}, qqq={"price": 440.0, "history": [
            [f"2026-07-{1 + i // 3:02d}", 400.0 + i] for i in range(70)]})
        self.assertIn("10 shares of MU at $100.00, held to the position cap", self.run_trade("size", "MU").stdout)

    def test_size_follows_the_cluster_room_and_the_regime(self):
        self.book({"starting_cash": 10000.0, "cash": 6500.0, "trades": [], "created_utc": "2026-07-10 14:00:00",
                   "positions": [{"symbol": "AMD", "shares": 35, "cost_basis": 100.0}]})
        out = self.run_trade("size", "MU").stdout
        self.assertIn("5 shares of MU at $100.00, held to the semiconductors cluster", out)
        self.assertNotEqual(self.run_trade("buy", "MU", "6", "--headline", "h-MU").returncode, 0)
        self.assertEqual(self.run_trade("buy", "MU", "5", "--headline", "h-MU").returncode, 0)
        self.prices({"MU": (100.0, None)}, qqq={"price": 380.0, "history": [
            [f"2026-07-{1 + i // 3:02d}", 470.0 - i] for i in range(70)]})
        self.assertIn("0 shares of MU: the regime is unfavourable", self.run_trade("size", "MU").stdout)

    def test_the_shadow_book_sizes_by_risk_too(self):
        (self.data / "candidates.json").write_text(json.dumps({"candidates": [{"symbol": "NVDA", "price": 100.0}]}))
        r = self.run_trade("shadow")
        self.assertIn("bought 10 NVDA (favorable)", r.stdout, "not its usual 20: NVDA's stop is 10% away")
        self.assertEqual(self.saved("shadow.json")["positions"][0]["risk_pct"], 10.0)

    # --- 7. no buys into earnings ----------------------------------------------

    def test_trading_days_until(self):
        from datetime import date
        t = date(2026, 10, 1)                                                  # a Thursday
        self.assertEqual(trade.trading_days_until("2026-10-01", t), 0)
        self.assertEqual(trade.trading_days_until("2026-10-05", t), 2, "the weekend is not counted")
        self.assertEqual(trade.trading_days_until("2026-10-08", t), 5)
        self.assertEqual(trade.trading_days_until("2026-10-09", t), 6)
        self.assertIsNone(trade.trading_days_until("2026-09-30", t), "already reported")

    def test_the_blackout_is_5_trading_days(self):
        from datetime import date
        t = self.mod()
        self.earnings(TSM="2026-10-08", INTC="2026-10-09", MU="2026-09-30")
        today = date(2026, 10, 1)
        self.assertEqual(t.earnings_soon("tsm", today), ("2026-10-08", 5))
        self.assertIsNone(t.earnings_soon("INTC", today), "6 trading days: fine")
        self.assertIsNone(t.earnings_soon("MU", today), "already reported")
        self.assertIsNone(t.earnings_soon("NVDA", today), "not reporting soon")

    def test_a_buy_into_earnings_is_refused(self):
        soon = (et_time.eastern_now().date() + timedelta(days=1)).isoformat()
        later = (et_time.eastern_now().date() + timedelta(days=40)).isoformat()
        self.earnings(NVDA=soon, PANW=later)
        r = self.run_trade("buy", "NVDA", "5", "--headline", "h-NVDA")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"NVDA reports earnings on {soon}", r.stderr)
        self.assertIn("0 shares of NVDA: NVDA reports earnings", self.run_trade("size", "NVDA").stdout)
        self.assertIn(f"earnings  NVDA {soon} - no new buys in these", self.run_trade("show").stdout)
        self.assertEqual(self.run_trade("buy", "PANW", "5", "--headline", "h-PANW").returncode, 0,
                         "40 days out is not soon")

    def test_no_calendar_does_not_stop_every_buy_and_says_so(self):
        self.assertEqual(self.run_trade("buy", "PANW", "5", "--headline", "h-PANW").returncode, 0)
        self.assertIn("earnings  no calendar yet", self.run_trade("show").stdout)

    def test_the_real_nasdaq_calendar_parses(self):
        from datetime import date
        fetch = load("belfort_fetch_earn", "belfort-fetch.py")
        asked = []

        def get(url):
            asked.append(url)
            if url.endswith("2026-10-21"):
                return self.NASDAQ
            return b'{"data": {"asOf": "x", "rows": null}, "message": null, "status": {"rCode": 200}}'
        with contextlib.redirect_stdout(io.StringIO()):
            got = fetch.fetch_earnings(["IBM", "SAP", "NVDA"], today=date(2026, 10, 19), get=get, gap=0)
        self.assertEqual(got, {"IBM": "2026-10-21", "SAP": "2026-10-21"})
        self.assertEqual(len(asked), 15, "21 days ahead, weekdays only")
        self.assertTrue(asked[2].endswith("date=2026-10-21"))

    def test_a_calendar_that_cannot_be_read_is_not_an_empty_one(self):
        from datetime import date
        fetch = load("belfort_fetch_earn_fail", "belfort-fetch.py")

        def get(url):
            raise OSError("timed out")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(fetch.fetch_earnings(["IBM"], today=date(2026, 10, 19), get=get, gap=0))

    def test_nasdaq_is_asked_as_a_browser_once(self):
        # Seen 2026-10-01: a user agent naming a bot was held open until it
        # timed out, every day; retrying that would stall the fetcher for minutes.
        fetch = load("belfort_fetch_earn_ua", "belfort-fetch.py")
        sent = []

        def urlopen(req, timeout=None, context=None):
            sent.append(req.get_header("User-agent"))
            raise OSError("no")
        from unittest import mock
        with mock.patch.object(fetch.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(io.StringIO()):
            from datetime import date
            fetch.EARNINGS_DAYS = 1
            fetch.fetch_earnings(["IBM"], today=date(2026, 10, 19), gap=0)
        self.assertEqual(sent, ["Mozilla/5.0"])

    # --- 8. dead money: see BelfortsSellRulesAreCode -----------------------------

    def test_dead_money_reads_qqq_from_the_quotes(self):
        t = self.mod()
        self.prices({"NVDA": (100.4, None)}, qqq={"price": 515.0, "history": [["2026-09-21", 500.0]]})
        q = json.loads((self.data / "quotes.json").read_text())
        q["quotes"]["NVDA"]["history"] = [[f"2026-09-{d}", 100.0] for d in (21, 22, 23, 24, 25, 28, 29, 30)] \
            + [["2026-10-01", 100.4]]
        (self.data / "quotes.json").write_text(json.dumps(q))
        p = {"cash": 9000.0, "trades": [], "positions": [
            {"symbol": "NVDA", "shares": 10, "cost_basis": 100.0, "entry_utc": "2026-09-21 13:36:00"}]}
        self.assertEqual([r for _, r, _ in t.exits_due(p)], ["dead"])

    # --- 9. the midday check -----------------------------------------------------

    def test_the_midday_shadow_check_sells_and_never_buys(self):
        (self.data / "candidates.json").write_text(json.dumps({"candidates": [{"symbol": "AMD", "price": 100.0}]}))
        self.prices({"PANW": (88.0, None), "AMD": (100.0, None)})
        self.book({"starting_cash": 10000.0, "cash": 9000.0, "cycle_count": 4, "created_utc": "2026-09-01 14:00:00",
                   "trades": [{"side": "BUY", "symbol": "PANW", "shares": 10, "price": 100, "notional": 1000}],
                   "positions": [{"symbol": "PANW", "shares": 10, "cost_basis": 100.0,
                                  "entry_utc": "2026-09-01 14:00:00"}]}, "shadow.json")
        r = self.run_trade("shadow", "--exits-only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shadow book, midday exits: sold PANW: STOP LOSS", r.stdout)
        after = self.saved("shadow.json")
        self.assertEqual((after["positions"], after["cash"], after["cycle_count"]), ([], 9880.0, 4),
                         "no AMD bought, and not counted as a cycle")
        self.assertEqual(json.loads((self.state / "equity.jsonl").read_text().splitlines()[-1])["book"], "shadow")
        self.assertIn("nothing due", self.run_trade("shadow", "--exits-only").stdout)

    def test_the_midday_timer(self):
        timer = (ROOT / "deploy" / "belfort-exits.timer").read_text()
        service = (ROOT / "deploy" / "belfort-exits.service").read_text()
        self.assertIn("OnCalendar=Mon..Fri 12:35 America/New_York", timer)
        runs = [l.split("=", 1)[1] for l in service.splitlines() if l.startswith("ExecStart=")]
        self.assertEqual([r.split("belfort-trade.py ")[1] for r in runs], ["exits --apply", "shadow --exits-only"])
        self.assertNotIn("openclaw", service, "code only: no model is woken")
        # deploy.sh installs and starts every deploy/belfort-*.timer
        self.assertIn('"$ROOT"/deploy/"$AGENT"-*.timer', (SCRIPTS / "deploy.sh").read_text())
        # the fetcher has fresh prices at 12:30 for it
        self.assertIn("OnCalendar=Mon..Fri 09..16:00/10 America/New_York",
                      (ROOT / "deploy" / "belfort-fetch.timer").read_text())

    def test_belfort_is_told(self):
        head = (ROOT / "agents" / "belfort" / "_belfort-agents-header.md").read_text()
        for line in ("belfort-trade.py size MRVL", "1% of the\nportfolio", "earnings within 5 trading days",
                     "again at **12:35\nET**", "**8+ trading days**, and behind QQQ"):
            self.assertIn(line, head)


class AceMoneyIsKeptByCode(unittest.TestCase):
    """2026-10-01, the review's first item: Ace graded his own bets - found the
    game, decided it was won, worked out the profit, edited bankroll.json -
    and everything downstream read that file. A verifier that checks a file
    balances cannot see a consistent mistake. ace-book.py keeps the money now,
    from ESPN's final scores. Every scoreboard here is a real one."""

    NFL = json.loads((FIXTURES / "espn-nfl-scoreboard-finals-week3.json").read_text())["events"]
    MLB = json.loads((FIXTURES / "espn-mlb-scoreboard-2026-04-03.json").read_text())["events"]
    SCHEDULED = json.loads((FIXTURES / "espn-nfl-scoreboard-week.json").read_text())["events"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.b = load("ace_book_t", "ace-book.py")
        self.b.STATE, self.b.BETS, self.b.BANK, self.b.RESULTS = d, d / "bets.jsonl", d / "bankroll.json", d / "results.json"
        self.b.BANK.write_text(json.dumps({"starting_bankroll": 10000.0, "bankroll": 10000.0, "cycle_count": 7}))

    def bet(self, sport, eid, sel, side, price, start):
        return self.b.place({"sport": sport, "event_id": eid, "selection": sel, "side": side,
                             "match": "x", "price": price, "starts_utc": start})

    def settled(self):
        return {s["selection"]: (s["result"], s["profit"]) for s in json.loads(self.b.BANK.read_text())["settled_bets"]}

    def test_real_finals_settle_won_and_lost(self):
        self.bet("nfl", "401872948", "ATL ML", "away", 150, "2026-09-25T00:15Z")    # ATL 35 @ GB 14
        self.bet("nfl", "401872953", "LAC ML", "away", -110, "2026-09-27T17:00Z")   # LAC 16 @ BUF 24
        self.b.record_results("nfl", self.NFL, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        done = self.b.settle(now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(len(done), 2)
        self.assertEqual(self.settled(), {"ATL ML": ("won", 150.0), "LAC ML": ("lost", -100.0)})
        self.assertEqual(done[0]["detail"], "ATL 35 @ GB 14")
        bank = json.loads(self.b.BANK.read_text())
        self.assertEqual((bank["bankroll"], bank["open_bets"], bank["cycle_count"]), (10050.0, [], 7))
        self.assertEqual(self.b.settle(now=datetime(2026, 9, 30, tzinfo=timezone.utc)), [], "never twice")
        # A second settle line for the same bet (two fetches racing) never
        # overrides the first.
        first = [e for e in self.b.events() if e["kind"] == "settle"][0]
        self.b.append(dict(first, result="void", profit=0.0))
        self.assertEqual(self.b.fold(self.b.events(), 10000.0)["bankroll"], 10050.0)

    def test_a_real_postponement_is_void_and_the_stake_comes_back(self):
        self.bet("mlb", "401814790", "KC ML", "home", -120, "2026-04-03T23:40Z")
        self.b.record_results("mlb", self.MLB, now=datetime(2026, 4, 4, tzinfo=timezone.utc))
        self.b.settle(now=datetime(2026, 4, 4, tzinfo=timezone.utc))
        self.assertEqual(self.settled(), {"KC ML": ("void", 0.0)})
        self.assertEqual(json.loads(self.b.BANK.read_text())["bankroll"], 10000.0)

    def test_a_scheduled_game_showing_zero_zero_is_not_a_result(self):
        # ESPN puts "0" in the score of a game that has not started.
        self.assertEqual(self.b.results_of("nfl", self.SCHEDULED), {})

    def test_a_tie_is_a_push(self):
        ev = copy.deepcopy(self.NFL[0])
        for c in ev["competitions"][0]["competitors"]:
            c["score"] = "20"
        self.bet("nfl", "401872948", "GB ML", "home", -200, "2026-09-25T00:15Z")
        self.b.record_results("nfl", [ev], now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.b.settle(now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(self.settled(), {"GB ML": ("push", 0.0)})

    def test_no_result_waits_then_voids_after_72_hours(self):
        self.bet("nfl", "123", "X ML", "home", 100, "2026-09-25T00:15Z")
        self.assertEqual(self.b.settle(now=datetime(2026, 9, 27, tzinfo=timezone.utc)), [])
        self.b.settle(now=datetime(2026, 9, 28, 1, tzinfo=timezone.utc))
        self.assertEqual(self.settled(), {"X ML": ("void", 0.0)})

    def test_the_rebuilt_bankroll_passes_the_verifiers_balance(self):
        self.bet("nfl", "401872948", "ATL ML", "away", 150, "2026-09-25T00:15Z")
        self.bet("nfl", "401872953", "LAC ML", "away", -110, "2026-09-27T17:00Z")
        self.bet("nfl", "401872964", "PIT ML", "away", -148, "2026-10-02T00:15Z")   # still open
        self.b.record_results("nfl", self.NFL, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.b.settle(now=datetime(2026, 9, 30, tzinfo=timezone.utc))
        av = load("ace_verify_book", "ace-verify.py")
        problems = []
        av.reconcile(json.loads(self.b.BANK.read_text()), problems)
        self.assertEqual(problems, [])
        self.assertEqual(json.loads(self.b.BANK.read_text())["bankroll"], 10000 - 300 + 250 + 0)

    def test_a_hand_edit_is_caught(self):
        self.bet("nfl", "401872948", "ATL ML", "away", 150, "2026-09-25T00:15Z")
        self.assertIsNone(self.b.drift())
        doc = json.loads(self.b.BANK.read_text())
        doc["bankroll"] = 10400.0
        self.b.BANK.write_text(json.dumps(doc))
        self.assertIn("bankroll $10,400.00 but the bets give $9,900.00", self.b.drift())

    def test_hand_written_bets_are_imported_once(self):
        self.b.BETS.unlink(missing_ok=True)
        self.b.BANK.write_text(json.dumps({"starting_bankroll": 10000.0, "bankroll": 9915.0, "settled_bets": [
            {"selection": "LAR ML", "match": "NYG @ LAR", "stake": 150, "profit": 65.0, "result": "won"}],
            "open_bets": [{"selection": "PIT ML", "match": "PIT @ CLE", "stake": 150, "price": -148,
                           "starts_utc": "2026-10-02T00:15Z"}]}))
        self.assertEqual(self.b.migrate(), 2)
        self.assertIsNone(self.b.migrate(), "once")
        doc = self.b.rebuild()
        self.assertEqual((doc["bankroll"], len(doc["open_bets"]), doc["settled_bets"][0]["profit"]),
                         (9915.0, 1, 65.0))
        # No ESPN id on it: it settles by the fixture name and start.
        res = {"nfl|401872964": {"sport": "nfl", "event_id": "401872964", "match": "PIT @ CLE",
                                 "start_utc": "2026-10-02T00:15Z", "status": "STATUS_FINAL", "home": "CLE",
                                 "away": "PIT", "home_score": 10, "away_score": 20, "winner": "away"}}
        self.b.RESULTS.write_text(json.dumps(res))
        self.b.settle(now=datetime(2026, 10, 2, 4, tzinfo=timezone.utc))
        self.assertEqual(self.settled()["PIT ML"], ("won", round(150 * 100 / 148, 2)))

    def test_the_fetcher_grades_from_the_boards_it_already_has(self):
        root = Path(self.tmp.name) / "root"
        (root / "agents/ace/state").mkdir(parents=True)
        (root / "agents/ace/state/bankroll.json").write_text(json.dumps({"starting_bankroll": 10000.0, "bankroll": 10000.0}))
        os.environ["ECOSYSTEM_ROOT"] = str(root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        f = load("ace_fetch_settle", "ace-fetch.py")
        book = f.ace_book()
        book.place({"sport": "nfl", "event_id": "401872948", "selection": "ATL ML", "side": "away",
                    "match": "ATL @ GB", "price": 150, "starts_utc": "2026-09-25T00:15Z"})
        with contextlib.redirect_stdout(io.StringIO()):
            f.settle_bets([("nfl", self.NFL)])
        bank = json.loads((root / "agents/ace/state/bankroll.json").read_text())
        self.assertEqual((bank["bankroll"], bank["settled_bets"][0]["result"]), (10150.0, "won"))
        self.assertIn('boards.append((sport, get(f"{BASE}/{path}/scoreboard?dates={yesterday}")',
                      (SCRIPTS / "ace-fetch.py").read_text(), "late finals: yesterday's board for daily sports")

    def test_help_touches_nothing(self):
        # preflight runs `<script> --help` to read the subcommands. ace-book.py
        # once did its import before reading the command, and the test of
        # that check left a bets.jsonl in the repository.
        root = Path(self.tmp.name) / "helproot"
        (root / "agents/ace/state").mkdir(parents=True)
        (root / "agents/ace/state/bankroll.json").write_text(json.dumps({"open_bets": [{"stake": 1}]}))
        for script in ("ace-book.py", "ace-sharp.py", "ace-baselines.py"):
            r = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"], capture_output=True, text=True,
                               env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
            self.assertEqual(r.returncode, 0, script)
            self.assertIn("{", r.stdout, f"{script}: argparse lists its subcommands for preflight")
        self.assertEqual(sorted(p.name for p in (root / "agents/ace/state").iterdir()), ["bankroll.json"])

    def test_the_header_says_code_keeps_the_money(self):
        head = (ROOT / "agents" / "ace" / "_ace-agents-header.md").read_text()
        self.assertIn("python3 ../../scripts/ace-book.py show", head)
        self.assertIn("**kept by `ace-book.py`; never edit it**", head)
        self.assertNotIn("compute win/loss, update `bankroll`", head)


class AcePricesBeforeAndAtTheClose(unittest.TestCase):
    """The price history behind "has the market moved on this news?", the
    5-minute close, and Pinnacle's price from The Odds API. The Pinnacle
    payload is the shape The Odds API documents; replace it with a capture
    (`ace-sharp.py check nfl`) once a key exists - believed, not verified."""

    ODDS = json.loads((FIXTURES / "the-odds-api-v4-h2h-documented-shape.json").read_text())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "agents/ace/state").mkdir(parents=True)
        (self.root / "agents/ace/data/context").mkdir(parents=True)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        os.environ.pop("ODDS_API_KEY", None)
        self.clv = load("ace_clv_p", "ace-clv.py")
        self.sharp = load("ace_sharp_p", "ace-sharp.py")

    def ctx(self, hours, name="PIT @ CLE", eid="401872964", home=("CLE", "Cleveland Browns"),
            away=("PIT", "Pittsburgh Steelers"), sport="nfl", now=None):
        start = ((now or datetime.now(timezone.utc)) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%MZ")
        return {"sport": sport, "event_id": eid, "short": name, "start_utc": start, "status": "STATUS_SCHEDULED",
                "home": {"abbr": home[0], "name": home[1]}, "away": {"abbr": away[0], "name": away[1]},
                "odds": {"moneyline_home": 124, "moneyline_away": -148, "novig_home_pct": 42.8, "novig_away_pct": 57.2}}

    def test_every_price_is_kept_and_read_back_by_time(self):
        t0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        c = self.ctx(10, now=t0)
        path = self.root / "lines.json"
        self.clv.record_lines([c], now=t0, path=path)
        c["odds"] = dict(c["odds"], novig_home_pct=45.0, novig_away_pct=55.0)
        self.clv.record_lines([c], now=t0 + timedelta(hours=1), path=path)
        entry = list(json.loads(path.read_text()).values())[0]
        self.assertEqual(len(entry["history"]), 2)
        self.assertEqual(self.clv.price_at(entry, "home", t0 + timedelta(minutes=30)), 42.8)
        self.assertEqual(self.clv.price_at(entry, "home", t0 + timedelta(hours=2)), 45.0)
        self.assertIsNone(self.clv.price_at(entry, "home", t0 - timedelta(minutes=1)))
        self.assertEqual(self.clv.unpriced(entry, "home", t0 + timedelta(minutes=30), 44.7), (True, 1.9))
        ok, why = self.clv.unpriced(entry, "home", t0 + timedelta(minutes=30), 44.8)
        self.assertFalse(ok)
        self.assertIn("+2.0 pts", why)

    def test_the_close_is_re_read_every_5_minutes_near_the_start(self):
        f = load("ace_fetch_close", "ace-fetch.py")
        now = datetime.now(timezone.utc)
        soon, later = self.ctx(1.5, now=now), self.ctx(3, name="LATE", eid="2", now=now)
        for c in (soon, later):
            (f.CTX / f"nfl-{c['event_id']}.json").write_text(json.dumps(c))
        asked = []
        def summary(sport, eid):
            asked.append(eid)
            return {"pickcenter": [{"provider": {"name": "DraftKings"}, "homeTeamOdds": {"moneyLine": 130},
                                    "awayTeamOdds": {"moneyLine": -155}}]}
        f.GAP = 0
        with contextlib.redirect_stdout(io.StringIO()):
            f.close_run(now=now, fetch_summary=summary)
        self.assertEqual(asked, ["401872964"], "only games within 2 hours")
        self.assertEqual(json.loads((f.CTX / "nfl-401872964.json").read_text())["odds"]["moneyline_home"], 130)
        entry = list(json.loads((self.root / "agents/ace/state/lines.json").read_text()).values())[0]
        self.assertEqual(entry["last"]["moneyline_home"], 130)
        timer = (ROOT / "deploy" / "ace-close.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 10..23:00/5 America/New_York", timer)
        for unit in ("ace-close.service", "ace-fetch.service"):
            self.assertIn("/usr/bin/flock /run/ace-fetch.lock", (ROOT / "deploy" / unit).read_text(), unit)
        self.assertIn("ace-fetch.py --close", (ROOT / "deploy" / "ace-close.service").read_text())

    def test_pinnacle_joins_by_time_and_name(self):
        c = self.ctx(0, now=datetime(2026, 10, 2, 0, 15, tzinfo=timezone.utc))
        e = self.sharp.join(c, self.ODDS)
        self.assertEqual(e["home_team"], "Cleveland Browns")
        self.assertTrue(self.sharp.attach(c, e, now=datetime(2026, 10, 1, 17, tzinfo=timezone.utc)))
        self.assertEqual((c["sharp"]["novig_home_pct"], c["sharp"]["novig_away_pct"]), (42.0, 58.0))
        self.assertEqual((c["sharp"]["draftkings_home_price"], c["sharp"]["draftkings_away_price"]), (124, -147))
        far = self.ctx(0, now=datetime(2026, 10, 2, 5, tzinfo=timezone.utc))
        self.assertIsNone(self.sharp.join(far, self.ODDS), "same teams, nearly five hours off")

    def test_names_are_compared_without_accents_or_brackets(self):
        c = self.ctx(0, name="M-OH @ SJSU", home=("SJSU", "San Jose State Spartans"),
                     away=("M-OH", "Miami (OH) RedHawks"), sport="cfb",
                     now=datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc))
        self.assertIsNotNone(self.sharp.join(c, self.ODDS))
        self.assertFalse(self.sharp.same_team("Miami Hurricanes", "Miami (OH) RedHawks"))
        self.assertTrue(self.sharp.same_team("Miami RedHawks", "Miami (OH) RedHawks"))

    def test_the_budget(self):
        now = datetime(2026, 10, 1, 18, 40, tzinfo=timezone.utc)      # 2:40pm ET
        calls = []
        def fetch(sport):
            calls.append(sport)
            return self.ODDS, 480
        ctxs = [self.ctx(5, now=now)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(len(self.sharp.refresh(ctxs, now=now, fetch=fetch)), 1)
            self.sharp.refresh(ctxs, now=now + timedelta(minutes=30), fetch=fetch)
        self.assertEqual(calls, ["nfl"], "one decision snapshot a sport a day")
        early = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)          # 11am ET
        self.assertEqual(self.sharp.wanted(ctxs, False, early, self.sharp._usage(early) | {"decision_done": []}), {})
        u = self.sharp._usage(now)
        self.assertEqual((u["calls"], u["remaining"]), (1, 480))
        self.assertFalse(self.sharp.may_spend(dict(u, calls=self.sharp.DAILY_CAP)))
        self.assertFalse(self.sharp.may_spend(dict(u, remaining=self.sharp.RESERVE)))
        close_ctx = [self.ctx(0.25, now=now)]
        self.assertEqual(self.sharp.wanted(close_ctx, True, now, dict(u, last={})), {"nfl": "close"})
        self.assertEqual(self.sharp.wanted(close_ctx, True, now, dict(u, last={"nfl": "2026-10-01T18:30:00Z"})), {},
                         "a close snapshot 10 minutes ago is recent enough")

    def test_the_key_is_never_in_an_error(self):
        secret = "abc123secretkeyvalue"
        def boom(req, timeout=None, context=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error_code": "INVALID_KEY"}'))
        with unittest.mock.patch.object(self.sharp.urllib.request, "urlopen", boom):
            with self.assertRaises(self.sharp.ApiError) as e:
                self.sharp.call("/v4/sports/", {}, key=secret)
        self.assertEqual(str(e.exception), "HTTP 401 INVALID_KEY")
        self.assertNotIn(secret, str(e.exception))

    def test_the_key_comes_from_credentials_env(self):
        self.assertIsNone(self.sharp.api_key())
        creds = self.root / "agents/ace/state/credentials.env"
        creds.write_text("ODDS_API_KEY = abc123\n")
        sharp = load("ace_sharp_k", "ace-sharp.py")
        self.assertEqual(sharp.api_key(), "abc123")


class TheNoAIBettorsAndTheBlindScore(unittest.TestCase):
    """The review's comparison stable and its first gate: bettors with no AI
    on the same games, and Ace's blind estimates scored against the market's
    own numbers. If the fresh-news bettor matches Ace, the model is
    decoration; if his blind numbers do not beat the market's, his bets are
    luck."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "agents/ace/state").mkdir(parents=True)
        (self.root / "agents/ace/data").mkdir(parents=True)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.bl = load("ace_baselines_t", "ace-baselines.py")
        self.clv = load("ace_clv_t", "ace-clv.py")
        self.now = datetime(2026, 10, 1, 19, tzinfo=timezone.utc)
        self.start = "2026-10-01T23:00Z"

    def rows(self, home_pct=57.2, news=(), sharp_home=None, eid="401"):
        mk = lambda sel, side, price, pct, sh: {
            "selection": sel, "match": "PIT @ CLE", "sport": "nfl", "event_id": eid, "side": side,
            "starts_utc": self.start, "price": price, "novig_pct": pct, "sharp_pct": sh,
            "fresh_injuries": list(news)}
        return [mk("CLE ML", "home", -148, home_pct, sharp_home),
                mk("PIT ML", "away", 124, round(100 - home_pct, 1), None if sharp_home is None else round(100 - sharp_home, 1))]

    def lines(self, hist):
        return {f"nfl|PIT @ CLE|{self.start}": {"start_utc": self.start, "last": hist[-1], "history": hist}}

    def snap(self, minutes_ago, home):
        return {"at": (self.now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "novig_home_pct": home, "novig_away_pct": round(100 - home, 1)}

    def picks(self, rows, lines):
        return {s: r["selection"] for s, r, _ in self.bl.decide(rows, lines, self.now, self.clv)}

    def test_random_and_favourite_decide_within_six_hours(self):
        got = self.picks(self.rows(), {})
        self.assertEqual(got["favourite"], "CLE ML")
        self.assertIn(got["random"], ("CLE ML", "PIT ML"))
        self.assertEqual(self.picks(self.rows(), {}), got, "the coin is fixed by the game id")
        self.start = "2026-10-02T03:00Z"                                # 8 hours out
        self.assertNotIn("favourite", self.picks(self.rows(), {}))

    def test_news_backs_the_injured_teams_opponent_while_the_price_has_not_moved(self):
        inj = {"id": "i1", "team": "CLE", "player": "QB", "status": "Out", "hours_old": 1.0,
               "reported_utc": (self.now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%MZ")}
        lines = self.lines([self.snap(120, 58.0), self.snap(0, 57.2)])
        self.assertEqual(self.picks(self.rows(news=[inj]), lines)["news"], "PIT ML")
        moved = self.lines([self.snap(120, 60.0), self.snap(0, 57.2)])     # PIT 40 -> 42.8
        self.assertNotIn("news", self.picks(self.rows(news=[inj]), moved), "the market already has it")
        old = dict(inj, hours_old=4.0)
        self.assertNotIn("news", self.picks(self.rows(news=[old]), lines))
        q = dict(inj, status="Questionable")
        self.assertNotIn("news", self.picks(self.rows(news=[q]), lines))

    def test_sharp_and_steam(self):
        self.assertEqual(self.picks(self.rows(sharp_home=59.5), {})["sharp"], "CLE ML")
        self.assertNotIn("sharp", self.picks(self.rows(sharp_home=59.0), {}))
        steam = self.lines([self.snap(90, 55.5), self.snap(0, 57.2)])
        self.assertEqual(self.picks(self.rows(), steam)["steam"], "CLE ML")
        self.assertNotIn("steam", self.picks(self.rows(), self.lines([self.snap(90, 56.0), self.snap(0, 57.2)])))

    def test_run_records_each_strategy_once_a_game_and_report_scores_it(self):
        cands = self.root / "agents/ace/data/candidates.json"
        cands.write_text(json.dumps({"candidates": self.rows()}))
        out = self.root / "agents/ace/state/baselines.jsonl"
        self.assertEqual(len(self.bl.run(now=self.now, path=out)), 2)
        self.assertEqual(self.bl.run(now=self.now, path=out), [], "once a game")
        (self.root / "agents/ace/state/lines.json").write_text(json.dumps(
            {f"nfl|PIT @ CLE|{self.start}": {"start_utc": "2026-09-01T00:00Z", "last": self.snap(0, 60.0),
                                             "history": [self.snap(0, 60.0)]}}))
        (self.root / "agents/ace/state/results.json").write_text(json.dumps(
            {"nfl|401": {"winner": "home", "status": "STATUS_FINAL"}}))
        r = self.bl.report(path=out)
        fav = r["favourite"]
        self.assertEqual((fav["bets"], fav["settled"], fav["won"], fav["profit"]), (1, 1, 1, 67.57))
        self.assertEqual(fav["avg_clv_pct"], self.clv.clv_at(-148, 60.0))
        self.assertIn("ace", r)

    def test_blind_estimates_are_scored_against_the_market(self):
        st = self.root / "agents/ace/state"
        rows = [("1", 70, "home", 60.0), ("2", 40, "away", 45.0), ("3", 55, "home", 50.0)]
        with (st / "blind.jsonl").open("w") as f:
            for eid, pct, _w, _q in rows:
                f.write(json.dumps({"utc": "2026-10-01T15:00:00Z", "key": f"nfl|G{eid}|{self.start}", "sport": "nfl",
                                    "event_id": eid, "starts_utc": self.start, "home_pct": pct}) + "\n")
        (st / "results.json").write_text(json.dumps({f"nfl|{e}": {"winner": w} for e, _p, w, _q in rows}))
        (st / "lines.json").write_text(json.dumps({f"nfl|G{e}|{self.start}": {
            "last": {"novig_home_pct": q}, "history": [{"at": "2026-10-01T14:00:00Z", "novig_home_pct": q}]}
            for e, _p, _w, q in rows}))
        c = self.clv.calibration()
        # blind: (.3^2 + .4^2 + .45^2)/3 = .1508 ; market: (.4^2 + .45^2 + .5^2)/3 = .2042
        self.assertEqual((c["games"], c["brier_blind"], c["brier_then"]), (3, 0.1508, 0.2042))
        self.assertEqual(c["blind_vs_market_then"], -0.0533)
        self.assertIn("Ace is better than the market", "\n".join(self.clv.calibration_lines(c)))

    def test_an_estimate_made_after_the_start_does_not_count(self):
        st = self.root / "agents/ace/state"
        (st / "blind.jsonl").write_text(json.dumps({"utc": "2026-10-02T01:00:00Z", "key": "k", "sport": "nfl",
                                                    "event_id": "1", "starts_utc": self.start, "home_pct": 90}) + "\n")
        (st / "results.json").write_text(json.dumps({"nfl|1": {"winner": "home"}}))
        self.assertEqual(self.clv.calibration()["games"], 0)

    def test_the_preregistration_is_frozen_with_the_codes_numbers(self):
        pre = (ROOT / "agents" / "ace" / "PREREGISTRATION.md").read_text()
        j = load("ace_judge_pre", "ace-judge.py")
        b = load("ace_book_pre", "ace-book.py")
        self.assertIn("Frozen **2026-10-01**", pre)
        self.assertIn(f"| Shrink toward the market | {j.SHRINK:.0%}", pre)
        self.assertIn(f"| Bet range | market chance {j.BET_RANGE[0]:g}–{j.BET_RANGE[1]:g}% |", pre)
        self.assertIn(f"| Max open bets | {b.MAX_OPEN_BETS}, one per game |", pre)
        self.assertIn(f"{j.EVIDENCE_MIN_BETS} settled bets with an average CLV", pre)
        self.assertIn(f"reach {b.FAULT_STOP_PCT:g}% of the starting bankroll", pre)


class AWellSellingNicheIsMeasuredInSales(unittest.TestCase):
    """"Build that version ... only that it's a well selling niche" (2026-10-02).

    Scout's scans measured favourites - liking - and only happened when
    somebody typed a command, so the data went stale and Scout had nothing to
    propose. Now each scan's strongest phrases carry sales evidence (reviews
    in the last 90 days on the top listings; a review is a purchase), Scout
    may only propose where that evidence exists, a morning job keeps every
    niche measured and finds new ones in the tags of listings that sell, and
    both headers say the niche is the market, never a design to copy."""

    SPEC = json.loads((FIXTURES / "etsy-oas-getReviewsByListing.json").read_text())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents/scout/state"
        (self.state / "scans").mkdir(parents=True)
        (self.state / "ideas.json").write_text(json.dumps({"ideas": []}))
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.ms = load("market_scan_sales", "market-scan.py")
        self.now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def scan(self, seed, rows, when=None):
        (self.state / "scans" / f"{self.ms.slug(seed)}.json").write_text(json.dumps(
            {"seed": seed, "scanned_at": when or self.now, "excluded": [], "rows": rows}))

    def row(self, phrase, score=0.01, selling=None, reviews=None, sales_n=10, tags=()):
        r = {"phrase": phrase, "supply": 5000, "heat": 0.05, "pull": 0.02, "price": 18.0, "match": 1.0,
             "returned": 25, "heat_n": 25, "pull_n": 25, "score": score, "tags": list(tags)}
        if selling is not None:
            r.update(selling=selling, sales_n=sales_n, reviews=reviews or 0, sales_days=90)
        return r

    # --- the measurement ---------------------------------------------------

    def test_the_spec_says_what_the_code_reads(self):
        # Etsy's published spec: the call needs only the API key, takes
        # min_created and limit, and its reply carries an integer count.
        op = self.SPEC["operation"]
        self.assertEqual(op["operationId"], "getReviewsByListing")
        # No security of its own: the spec-wide default, the API key alone -
        # exactly what the listing search Scout already runs has.
        self.assertIsNone(op["security"])
        self.assertEqual(self.SPEC["global_security"], [{"api_key": []}])
        self.assertIsNone(self.SPEC["findAllListingsActive_security"])
        self.assertTrue({"limit", "min_created"} <= {p["name"] for p in op["parameters"]})
        self.assertEqual(self.SPEC["ListingReviews"]["properties"]["count"]["type"], "integer")
        self.assertEqual(self.SPEC["ShopListing_fields"]["listing_id"]["type"], "integer")

    def test_sales_counts_recent_reviews_and_leaves_failures_out(self):
        asked, now = [], 1_800_000_000
        rv = lambda *days: {"count": len(days), "results": [{"create_timestamp": now - d * 86400} for d in days]}
        # A year's window: reviews are rare (2026-10-02, the dog mom mug check).
        replies = {11: (rv(1, 30, 89, 200), {}, None), 12: (rv(366, 400), {}, None),
                   13: (None, {}, "HTTP 500: x"), 14: (rv(10, 364), {}, None)}
        def call(path, key):
            asked.append(path)
            return replies[int(path.split("/")[2])]
        got = self.ms.sales("k", [11, 12, 13, 14], now=now, call=call)
        self.assertEqual(got, {"reviews": 6, "selling": 2, "sales_n": 3, "sold_ids": [11, 14]},
                         "counted from the review dates; the failed call is not a zero")
        self.assertEqual(asked[0], "/listings/11/reviews?limit=100", "no min_created: it read 0 on the droplet")
        self.assertIsNone(self.ms.sales("k", [13], call=lambda p, k: (None, {}, "HTTP 500")))

    def test_measure_keeps_the_top_listing_ids_in_etsys_order(self):
        rows = [{"listing_id": i, "num_favorers": 1} for i in range(1, 14)] + [{"listing_id": "x"}]
        self.assertEqual(self.ms.measure({"results": rows, "count": 14})["ids"], list(range(1, 11)))

    def test_a_saved_scan_carries_the_sales(self):
        m = {"supply": 100, "heat": 0.1, "pull": 0.02, "price": 9.0, "match": 1.0, "returned": 25,
             "heat_n": 25, "pull_n": 25, "tags": [], "selling": 7, "sales_n": 10, "reviews": 41}
        self.ms.SCANS = self.state / "scans"
        with contextlib.redirect_stdout(io.StringIO()):
            path = self.ms.save_scan("frog sticker", [("frog sticker", m)], [])
        row = json.loads(path.read_text())["rows"][0]
        self.assertEqual((row["selling"], row["sales_n"], row["reviews"], row["sales_days"]), (7, 10, 41, 365))

    # --- Scout's gate ----------------------------------------------------------

    def test_only_a_selling_phrase_can_be_proposed(self):
        self.scan("frog sticker", [self.row("frog sticker", selling=6, reviews=31),
                                   self.row("frog sticker cute", selling=0),
                                   self.row("frog sticker pack"),
                                   self.row("frog sticker dead", score=0.0, selling=0)])
        si = load("scout_ideas_sales", "scout-ideas.py")
        self.assertIsNone(si.measured("frog sticker")[2])
        self.assertIn("none of its top 10 listings had a review in the last 90 days",
                      si.measured("frog sticker cute")[2])
        self.assertIn("its sales were never measured", si.measured("frog sticker pack")[2])
        self.assertEqual([p for p, _ in si.proposable()], ["frog sticker"])

    def test_the_brief_is_most_money_first_and_says_original(self):
        # 2026-10-08: every phrase had 10 of 10 selling, reviews decided the
        # order, and the cheapest product filled the top 25. Money decides now.
        self.scan("mug", [self.row("cat lover mug", selling=3, reviews=50),
                          self.row("teacher gift mug", selling=8, reviews=12),
                          self.row("nurse gift mug", selling=8, reviews=40)])
        self.scan("sticker", [dict(self.row("cat sticker", selling=10, reviews=600), price=4.0)])
        self.scan("sweatshirt", [dict(self.row("book sweatshirt", selling=10, reviews=227), price=38.0)])
        si = load("scout_ideas_brief", "scout-ideas.py")
        self.assertEqual([p for p, _ in si.proposable()],
                         ["book sweatshirt", "cat sticker", "cat lover mug", "nurse gift mug", "teacher gift mug"],
                         "227 sales at $38 outrank 600 at $4")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            si.cmd_brief([])
        self.assertIn("nurse gift mug  (5,000 listings; 8 of top 10 sold in 90 days, 40 reviews; "
                      "x $18.00 = $720 of sales)", out.getvalue())
        self.assertIn("never describe, copy or\n  imitate a particular listing", out.getvalue())

    def test_the_brief_gives_each_product_its_own_best(self):
        # Fifteen sticker phrases outsell everything by reviews; the shirt
        # must still reach Scout, and only PER_PRODUCT of the stickers do.
        self.scan("sticker", [dict(self.row(f"sticker idea {i}", selling=10, reviews=900 - i), price=4.0)
                              for i in range(15)])
        self.scan("shirt", [dict(self.row("crow shirt", selling=10, reviews=200), price=25.0)])
        si = load("scout_ideas_groups", "scout-ideas.py")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            si.cmd_brief([])
        text = out.getvalue()
        self.assertIn("STICKER (15 phrases measured):", text)
        self.assertIn("crow shirt", text, "the shirt was 16th by reviews and used to fall off the top")
        self.assertEqual(text.count("    sticker idea "), si.PER_PRODUCT)
        self.assertIn(f"at most {si.MAX_PER_PRODUCT} ideas of one product", text)
        self.assertLess(text.index("TSHIRT ("), text.index("STICKER ("),
                        "products by their best phrase: 200 x $25 beats 900 x $4")

    def catalogue(self, *keys):
        cat = self.root / "agents/emily/state/printify-catalog.json"
        cat.parent.mkdir(parents=True, exist_ok=True)
        cat.write_text(json.dumps({k: {"variant_ids": [1]} for k in keys}))

    def test_a_market_emily_cannot_make_is_not_handed_over(self):
        # The real brief, 2026-10-08: 'embroidered hat custom' third and
        # 'botanical prints' in the list, with no hat or print in the shop.
        self.catalogue("sticker", "tshirt", "mug")
        for phrase, reviews in (("embroidered hat custom", 545), ("botanical prints", 86),
                                ("car decal business", 325), ("crow shirt", 235), ("bird lover gift", 50)):
            self.scan(phrase, [self.row(phrase, selling=10, reviews=reviews)])
        si = load("scout_ideas_makeable", "scout-ideas.py")
        self.assertEqual([p for p, _ in si.proposable()], ["car decal business", "crow shirt", "bird lover gift"])
        self.assertEqual({k: [p for p, _ in rows] for k, rows in si.by_product(si.proposable())},
                         {"sticker": ["car decal business"], "tshirt": ["crow shirt"],
                          "any product": ["bird lover gift"]})

    def intake(self, *lines):
        (self.state / "drafts.txt").write_text("".join(l + "\n" for l in lines))
        (self.state / "proposals.json").write_text("[]\n")     # merged between runs, in a real cycle
        return subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), "intake"],
                              capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))

    def test_a_run_files_at_most_two_of_one_product_when_another_sells(self):
        self.catalogue("sticker", "tshirt")
        self.scan("sticker", [self.row(f"cat sticker {i}", selling=10, reviews=300) for i in range(3)])
        stickers = [f"cat sticker {i} | Cat {i} Sticker | sticker | x" for i in range(3)]
        r = self.intake(*stickers)
        self.assertIn("3 filed, 0 refused", r.stdout, "stickers are all there is: no cap")
        self.scan("shirt", [self.row("crow shirt", selling=10, reviews=20)])
        r = self.intake(*stickers)
        self.assertIn("2 filed, 1 refused", r.stdout, r.stderr)
        self.assertIn("at most 2 of one product a run", r.stderr)
        r = self.intake(*stickers[:2], "crow shirt | Crow Shirt | tshirt | y")
        self.assertIn("3 filed, 0 refused", r.stdout, r.stderr)

    def test_two_sticker_entries_are_one_kind_for_the_mix(self):
        # The droplet's catalogue has 'sticker' and 'kisscut'; counted as two
        # products, a run could file four stickers.
        cat = self.root / "agents/emily/state/printify-catalog.json"
        cat.parent.mkdir(parents=True, exist_ok=True)
        cat.write_text(json.dumps({"sticker": {"blueprint_title": "Sticker Sheets"},
                                   "kisscut": {"blueprint_title": "Kiss-Cut Stickers"},
                                   "tshirt": {"blueprint_title": "Unisex Jersey Short Sleeve Tee"}}))
        self.scan("sticker", [self.row(f"cat sticker {i}", selling=10, reviews=300) for i in range(4)])
        self.scan("shirt", [self.row("crow shirt", selling=10, reviews=20)])
        r = self.intake("cat sticker 0 | A | sticker | x", "cat sticker 1 | B | sticker | x",
                        "cat sticker 2 | C | kisscut | x", "cat sticker 3 | D | kisscut | x")
        self.assertIn("2 filed, 2 refused", r.stdout, r.stderr)
        self.assertIn("already 2 sticker ideas", r.stderr)

    def test_a_phrase_already_used_gives_way_to_a_fresh_one(self):
        self.scan("sticker", [dict(self.row(f"sticker idea {i}", selling=10, reviews=900 - i), price=4.0)
                              for i in range(5)])
        (self.state / "ideas.json").write_text(json.dumps({"ideas": [
            {"title": "Old", "evidence": {"phrase": "sticker idea 0"}}]}))
        si = load("scout_ideas_fresh", "scout-ideas.py")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            si.cmd_brief([])
        self.assertNotIn("sticker idea 0", out.getvalue(), "the best is used: three fresh ones instead")
        self.assertIn("sticker idea 3", out.getvalue())

    def test_the_idea_carries_its_sales_to_the_owner(self):
        self.scan("frog sticker", [self.row("frog sticker", selling=6, reviews=31)])
        r = subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), "propose", "--phrase", "frog sticker",
                            "--title", "Pond Frog Sticker", "--product", "sticker"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("6 of top 10 sold in 90 days, 31 reviews (a review is a purchase - a floor)", r.stdout)
        ev = json.loads((self.state / "proposals.json").read_text())["proposals"][0]["evidence"]
        self.assertEqual((ev["sold"], ev["sold_of"], ev["reviews"], ev["sales_days"]), (6, 10, 31, 90))

    # --- the morning niche scan ------------------------------------------------

    def test_oldest_first_never_scanned_first_and_fresh_ones_left(self):
        ns = load("niche_scan_due", "niche-scan.py")
        ns.STARTER = ["frog sticker", "cat lover mug", "dog mom mug"]
        old = (datetime.now(timezone.utc) - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        older = (datetime.now(timezone.utc) - timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.scan("frog sticker", [], when=old)
        self.scan("cat lover mug", [], when=older)
        self.assertEqual(ns.due(n=5), ["dog mom mug", "cat lover mug", "frog sticker"])
        self.scan("frog sticker", [], when=self.now)
        self.assertEqual(ns.due(n=5), ["dog mom mug", "cat lover mug"], "scanned today: not due")
        self.assertEqual(ns.due(n=1), ["dog mom mug"])
        self.assertEqual(ns.due(n=5, force=True), ["dog mom mug", "cat lover mug", "frog sticker"],
                         "--force: re-scan this week's too, when the measuring changed")

    def test_somebody_elses_property_is_never_scanned(self):
        ns = load("niche_scan_ip", "niche-scan.py")
        ns.STARTER = ["disney sticker", "frog sticker"]
        self.assertEqual(ns.due(n=5), ["frog sticker"])

    def test_new_niches_come_from_the_tags_of_listings_that_sell(self):
        ns = load("niche_scan_find", "niche-scan.py")
        ns.STARTER = ["cat lover mug"]
        self.scan("cat lover mug", [
            self.row("cat lover mug", selling=6, reviews=20,
                     tags=["cat mom gift", "Coffee Mug", "cat", "disney cat", "funny cat mug", "x1!"]),
            self.row("cat lover mug cheap", selling=2, reviews=3, tags=["sad cat"])])
        d = {"niches": {}}
        added = ns.discover(d)
        self.assertEqual(added, ["cat mom mug", "coffee mug", "funny cat mug"])
        self.assertTrue(d["niches"]["cat mom mug"]["source"].startswith("tag on cat lover mug"))
        self.assertEqual(ns.discover(d), [], "nothing twice")
        ns.DISCOVER_PER_RUN = 1
        self.assertEqual(len(ns.discover({"niches": {}})), 1)

    def test_a_run_scans_with_market_scan_and_saves(self):
        ns = load("niche_scan_run", "niche-scan.py")
        ns.STARTER, ran = ["frog sticker"], []
        def fake(cmd, **k):
            ran.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="  sales: frog sticker - 6 of the top 10\n  saved 3", stderr="")
        with unittest.mock.patch.object(ns.subprocess, "run", fake), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ns.main(["run"]), 0)
        self.assertEqual(ran[0][1:], [str(SCRIPTS / "market-scan.py"), "scan", "frog", "sticker", "--save"])
        self.assertIn("sales: frog sticker", out.getvalue())
        self.assertTrue((self.state / "niches.json").exists())

    def test_the_morning_timer_runs_before_scout(self):
        timer = (ROOT / "deploy" / "scout-niches.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 06:30 America/Chicago", timer)
        self.assertIn("OnCalendar=*-*-* 08:00 America/Chicago", (ROOT / "deploy" / "scout-cycle.timer").read_text())
        self.assertIn("scripts/niche-scan.py run", (ROOT / "deploy" / "scout-niches.service").read_text())
        ns = load("niche_scan_budget", "niche-scan.py")
        self.assertLessEqual(ns.MAX_NICHES / ns.RUN_N, load("scout_ideas_stale", "scout-ideas.py").STALE_DAYS,
                             "every niche re-measured before Scout calls it stale")

    def test_both_headers_say_original_never_a_copy(self):
        scout = (ROOT / "agents/scout/_scout-agents-header.md").read_text()
        emily = (ROOT / "agents/emily/_emily-agents-header.md").read_text()
        self.assertIn("**You may only propose into a phrase with recent sales.**", scout)
        self.assertIn("**The niche is the market, not the design.**", scout)
        self.assertIn("**Every design is your own.**", emily)
        self.assertIn("a near-copy is still a copy", emily)
        self.assertNotIn("Cap 3 drafts a day", emily, "the owner removed the cap on 2026-10-01")


class TheSameBlankTheSellersUse(unittest.TestCase):
    """"Recreate the product as much as possible, so using the same shirt, mug
    or poster. If we don't have that product available to Emily yet, set some
    type of signal to find the closest thing to it" (owner, 2026-10-02).

    The listing text a scan already downloads names the blank on most
    apparel ("Comfort Colors 1717"); Printify's blueprints carry `brand` and
    `model` (its docs). blanks.py is the one matcher; the morning scan warns
    when Emily lacks a selling blank and names the closest Printify product;
    Emily's draft repeats the warning. A signal, never a refusal. The listing
    wording here is believed, not captured - Etsy's site refuses this session -
    and the first droplet scan is where it is checked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "agents/scout/state/scans").mkdir(parents=True)
        (self.root / "agents/emily/state").mkdir(parents=True)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.b = load("blanks_t", "blanks.py")
        self.BPS = [{"id": 706, "title": "Unisex Garment-Dyed T-shirt", "brand": "Comfort Colors", "model": "1717"},
                    {"id": 6, "title": "Unisex Heavy Cotton Tee", "brand": "Gildan", "model": "5000"},
                    {"id": 49, "title": "Unisex Heavy Blend Crewneck Sweatshirt", "brand": "Gildan", "model": "18000"},
                    {"id": 68, "title": "Ceramic Mug 11oz", "brand": "Generic", "model": "Mug"}]

    def names(self, text, kind=None):
        return [x["blank"] for x in self.b.identify(text, kind)]

    # --- reading a listing -----------------------------------------------------

    def test_brand_and_model_as_sellers_write_them(self):
        self.assertEqual(self.names("Bookish Tee | Comfort Colors® C1717 garment dyed"), ["Comfort Colors 1717"])
        self.assertEqual(self.names("printed on a Gildan 18000 crewneck"), ["Gildan 18000"])
        self.assertEqual(self.names("Bella + Canvas 3001 unisex"), ["Bella+Canvas 3001"])
        self.assertEqual(self.names("Independent Trading Co. SS4500 hoodie"), ["Independent Trading Co. 4500"])
        self.assertEqual(self.names("soft Comfort Colors shirt"), ["Comfort Colors"], "brand alone still says something")
        self.assertEqual(self.names("Comfort Colors 1717. Our Comfort Colors shirts run big"), ["Comfort Colors 1717"],
                         "the same brand twice is one blank")
        self.assertEqual(self.names("Cute frog shirt, soft and comfy", "tshirt"), [], "nothing named, nothing guessed")

    def test_a_percentage_is_not_a_model(self):
        # Bug: the droplet warned 'bookish sweatshirt embroidered' sells on
        # "Comfort Colors 100" - believed to be "Comfort Colors 100% cotton".
        self.assertEqual(self.names("Comfort Colors 100% ring-spun cotton"), ["Comfort Colors"])
        self.assertEqual(self.names("Comfort Colors 100 % cotton"), ["Comfort Colors"])
        self.assertEqual(self.names("Comfort Colors 1566, 100% cotton"), ["Comfort Colors 1566"])

    def test_attributes_where_no_model_is_named(self):
        self.assertEqual(self.names("Cat Mug, 11 oz white ceramic coffee cup", "mug"), ["mug 11oz ceramic"])
        self.assertEqual(self.names("Botanical 18x24 inch matte print", "poster"), ["poster 18x24 matte"])
        self.assertEqual(self.names("18x24 unframed poster", "poster"), ["poster 18x24 unframed"],
                         "unframed is not framed")
        self.assertEqual(self.names("Frog sticker, kiss-cut vinyl", "sticker"), ["sticker kiss-cut vinyl"])

    def test_counted_among_the_listings_that_sold(self):
        texts = {1: "Comfort Colors 1717 tee", 2: "Comfort Colors 1717", 3: "Gildan 5000 tee",
                 4: "Gildan 5000", 5: "Gildan 5000 shirt"}
        top = self.b.tally(texts, sold_ids={1, 2, 3}, kind="tshirt")
        self.assertEqual([(t["blank"], t["selling"], t["mentions"]) for t in top],
                         [("Comfort Colors 1717", 2, 2), ("Gildan 5000", 1, 3)],
                         "what the sellers who SELL use wins over what is mentioned most")

    # --- Emily's products and Printify's ---------------------------------------

    def test_a_gap_says_what_emily_would_print_on_instead(self):
        top = [{"blank": "Comfort Colors 1717", "brand": "Comfort Colors", "model": "1717", "attrs": [], "selling": 5}]
        tee = {"key": "tshirt", "blueprint_title": "Unisex Heavy Cotton Tee", "brand": "Gildan", "model": "5000"}
        g = self.b.gap(top, "shirt", [tee])
        self.assertEqual((g["blank"], g["have"]), ("Comfort Colors 1717", ["Unisex Heavy Cotton Tee"]))
        same = dict(tee, brand="Comfort Colors", model="C1717")
        self.assertIsNone(self.b.gap(top, "tshirt", [same]), "the same blank, written differently")

    def test_the_closest_printify_product(self):
        exact = self.b.closest({"brand": "Comfort Colors", "model": "1717", "attrs": []}, "tee", self.BPS)
        self.assertEqual((exact[0]["id"], exact[0]["why"]), (706, "same brand and model"))
        near = self.b.closest({"brand": "Comfort Colors", "model": "1566", "attrs": []}, "crewneck", self.BPS)
        self.assertEqual((near[0]["id"], near[0]["why"]), (49, "same kind, different brand"))
        mug = self.b.closest({"brand": None, "model": None, "attrs": ["11oz", "ceramic"]}, "mug", self.BPS)
        self.assertEqual((mug[0]["id"], mug[0]["why"]), (68, "same kind and the same attributes"))
        self.assertEqual(self.b.closest({"brand": "Gildan", "model": "18500", "attrs": []}, "poster", self.BPS), [],
                         "nothing of the same kind is not a suggestion")

    def test_printifys_brand_carries_the_registered_mark(self):
        # Bug: the BPS above write "Comfort Colors"; the real catalogue writes
        # "Comfort Colors®" (droplet, 2026-10-02), so the bookish-sweatshirt
        # niche - 9 of 10 selling on Comfort Colors 1717 - never matched blueprint
        # 706 and was offered a Gildan instead. These two records are as Printify
        # returned them.
        real = [{"id": 706, "title": "Unisex Garment-Dyed T-shirt", "brand": "Comfort Colors®", "model": "1717"},
                {"id": 1257, "title": "Unisex Color Blast Crewneck Sweatshirt", "brand": "Comfort Colors®", "model": "1545"},
                {"id": 49, "title": "Unisex Heavy Blend Crewneck Sweatshirt", "brand": "Gildan", "model": "18000"}]
        seller = self.b.identify("Bookish Sweatshirt | Comfort Colors 1717")[0]
        self.assertEqual(self.b.matches(seller, real[0]), "exact")
        exact = self.b.closest(seller, "tshirt", real)
        self.assertEqual((exact[0]["id"], exact[0]["why"]), (706, "same brand and model"))
        near = self.b.closest({"brand": "Comfort Colors", "model": "1566", "attrs": []}, "crewneck", real)
        self.assertEqual(near[0]["id"], 1257, "the same brand's crewneck before another brand's")
        self.assertEqual(self.b.brand_key("Bella+Canvas"), self.b.brand_key("BELLA + CANVAS™"))

    def test_a_brand_alone_counts_only_on_the_same_kind(self):
        # Bug: after the owner set up Comfort Colors 1717 as Emily's tee, the
        # 'bookish sweatshirt' warning (sellers naming only "Comfort Colors")
        # disappeared - a tee answered for a sweatshirt. Entries as saved by
        # pick on the droplet, 2026-10-02.
        tee = {"key": "tshirt", "blueprint_title": "Unisex Garment-Dyed T-shirt",
               "brand": "Comfort Colors®", "model": "1717"}
        crew = {"key": "sweatshirt", "blueprint_title": "Unisex Heavy Blend™ Crewneck Sweatshirt",
                "brand": "Gildan", "model": "18000"}
        cc = [{"blank": "Comfort Colors", "brand": "Comfort Colors", "model": None, "attrs": [], "selling": 3}]
        g = self.b.gap(cc, "sweatshirt", [tee, crew])
        self.assertIsNotNone(g, "a Comfort Colors tee is not a Comfort Colors sweatshirt")
        self.assertEqual(g["have"], ["Unisex Heavy Blend™ Crewneck Sweatshirt"])
        self.assertIsNone(self.b.gap(cc, "tshirt", [tee, crew]), "on a tee it is the same brand")
        exact = [{"blank": "Comfort Colors 1717", "brand": "Comfort Colors", "model": "1717", "attrs": [], "selling": 3}]
        self.assertIsNone(self.b.gap(exact, "shirt", [tee, crew]), "a model number names the garment")
        zipper = [{"blank": "tote zipper", "brand": None, "model": None, "attrs": ["zipper"], "selling": 2}]
        zt = {"key": "zipper-tote", "blueprint_title": "Zippered Canvas Tote", "brand": "Q-Tees", "model": "Q1300"}
        self.assertIsNone(self.b.gap(zipper, "tote", [tee, zt]))
        canvas = [{"blank": "tote canvas", "brand": None, "model": None, "attrs": ["canvas"], "selling": 2}]
        wall = {"key": "canvas", "blueprint_title": "Matte Canvas, Stretched, 1.25\"", "brand": None, "model": None}
        self.assertIsNotNone(self.b.gap(canvas, "tote", [wall]), "a wall canvas is not a canvas tote")

    def test_the_zippered_canvas_tote(self):
        # Bug: 'teacher tote bag with zipper' (2 of 3 selling on a zipper tote)
        # and 'library tote bag pattern' (canvas cotton zipper) never suggested
        # Printify's "Zippered Canvas Tote" - "zippered" is not "zipper", and
        # "canvas" was read as a plural ("canva") on the title side only.
        # Titles as Printify returned them on the droplet, 2026-10-02.
        totes = [{"id": 507, "title": "Canvas Tote Bag, 5-Color Straps", "brand": "Generic brand", "model": ""},
                 {"id": 553, "title": "Cotton Tote Bag", "brand": "AS Colour", "model": "1001"},
                 {"id": 1990, "title": "Zippered Canvas Tote", "brand": None, "model": None}]
        zipper = {"blank": "tote zipper", "brand": None, "model": None, "attrs": ["zipper"]}
        got = self.b.closest(zipper, "tote", totes)
        self.assertEqual((got[0]["id"], got[0]["why"]), (1990, "same kind and the same attributes"))
        library = {"blank": "tote canvas cotton zipper", "brand": None, "model": None,
                   "attrs": ["canvas", "cotton", "zipper"]}
        got = self.b.closest(library, "tote", totes)
        self.assertEqual((got[0]["id"], got[0]["why"]), (1990, "same kind, shares: canvas, zipper"))
        self.assertEqual(got[1]["why"], "same kind, shares: canvas")

    def test_a_kids_or_womens_cut_is_not_the_same_model(self):
        # Bug: the droplet's 'best cat mom shirt' (Gildan 5000) listed Kids Heavy
        # Cotton Tee 5000B and Women's Midweight 5000L as "same brand and model";
        # only their higher ids kept them behind the real one. Titles and models
        # as Printify returned them; the ids are swapped so order cannot be luck.
        bps = [{"id": 1, "title": "Kids Heavy Cotton™ Tee", "brand": "Gildan", "model": "5000B"},
               {"id": 2, "title": "Women's Midweight Cotton Tee", "brand": "Gildan", "model": "5000L"},
               {"id": 6, "title": "Unisex Heavy Cotton Tee", "brand": "Gildan", "model": "5000"}]
        seller = self.b.identify("Best Cat Mom Shirt, Gildan 5000 unisex")[0]
        got = self.b.closest(seller, "tshirt", bps)
        self.assertEqual([g["id"] for g in got], [6, 1, 2])
        self.assertEqual(got[0]["why"], "same brand and model")
        self.assertTrue(got[1]["why"].startswith("same brand, a variant of the model"))
        top = [dict(seller, selling=1)]
        self.assertIsNotNone(self.b.gap(top, "tshirt", [dict(bps[0], key="tshirt")]),
                             "owning the kids' tee is not owning the one that sells")
        self.assertIsNone(self.b.gap(top, "tshirt", [dict(bps[2], key="tshirt")]))

    # --- the scan, the warning, the draft --------------------------------------

    def test_the_scan_reads_the_text_and_notes_who_sold(self):
        ms = load("market_scan_blank", "market-scan.py")
        m = ms.measure({"count": 2, "results": [
            {"listing_id": 7, "title": "Frog Tee", "description": "On a Comfort Colors 1717.", "materials": ["cotton"]},
            {"listing_id": 8, "title": "Frog Tee 2"}]})
        self.assertIn("Comfort Colors 1717", m["texts"][7])
        self.assertIn("cotton", m["texts"][7])
        recent = {"count": 3, "results": [{"create_timestamp": int(time.time()) - 86400}] * 3}
        got = ms.sales("k", [7, 8], call=lambda p, k: (recent if "/7/" in p else {"count": 0, "results": []}, {}, None))
        self.assertEqual(got["sold_ids"], [7])
        ms.SCANS = self.root / "agents/scout/state/scans"
        row = {"supply": 10, "heat": 0.1, "pull": 0.1, "price": 25.0, "match": 1.0, "returned": 2, "heat_n": 2,
               "pull_n": 2, "tags": [], "selling": 1, "sales_n": 2, "reviews": 3,
               "blanks": self.b.tally(m["texts"], {7}, "tshirt")}
        with contextlib.redirect_stdout(io.StringIO()):
            saved = json.loads(ms.save_scan("frog shirt", [("frog shirt", row)], []).read_text())
        self.assertEqual(saved["rows"][0]["blanks"][0]["blank"], "Comfort Colors 1717")
        self.assertNotIn("texts", saved["rows"][0], "sellers' copy is never saved")

    def write_scan(self, blank="Comfort Colors 1717", brand="Comfort Colors", model="1717"):
        (self.root / "agents/scout/state/scans/frog-shirt.json").write_text(json.dumps({
            "seed": "frog shirt", "scanned_at": "2026-10-02T10:00:00Z", "rows": [
                {"phrase": "frog shirt", "score": 0.01, "selling": 6, "sales_n": 10,
                 "blanks": [{"blank": blank, "brand": brand, "model": model, "attrs": [], "selling": 5, "mentions": 6}]}]}))

    def test_the_morning_warning_names_the_closest_product(self):
        ns = load("niche_scan_gap", "niche-scan.py")
        self.write_scan()
        ns.catalogue = lambda: [{"key": "tshirt", "blueprint_title": "Unisex Heavy Cotton Tee", "brand": "Gildan", "model": "5000"}]
        [g] = ns.gaps(bps=self.BPS)
        lines = ns.gap_lines(g)
        self.assertIn("WARNING: 'frog shirt' sells on Comfort Colors 1717 (5 of its 6 selling listings name it). "
                      "Emily has: Unisex Heavy Cotton Tee.", lines[0])
        self.assertIn("closest on Printify: blueprint 706 Unisex Garment-Dyed T-shirt", lines[1])
        self.assertIn("pick --product tshirt --blueprint 706", lines[-1])
        self.assertEqual(json.loads((self.root / "agents/scout/state/blank-gaps.json").read_text())["gaps"][0]["blank"],
                         "Comfort Colors 1717")
        doc = json.loads((self.root / "agents/scout/state/scans/frog-shirt.json").read_text())
        doc["rows"].append(dict(doc["rows"][0], phrase="frog shirt funny"))
        (self.root / "agents/scout/state/scans/frog-shirt.json").write_text(json.dumps(doc))
        self.assertEqual(len(ns.gaps(bps=self.BPS)), 1, "one warning a blank, however many phrases sell on it")
        ns.catalogue = lambda: [{"key": "tshirt", "blueprint_title": "Garment-Dyed T-shirt", "brand": "Comfort Colors", "model": "1717"}]
        self.assertEqual(ns.gaps(bps=self.BPS), [], "Emily has it: no warning")

    def test_printify_is_read_once_a_week_and_a_failure_is_not_fatal(self):
        ns = load("niche_scan_bps", "niche-scan.py")
        calls = []
        ns.printify = lambda: types.SimpleNamespace(call=lambda path: calls.append(path) or [
            {"id": 1, "title": "T", "brand": "B", "model": "M", "images": ["x"]}])
        self.assertEqual(ns.blueprints(), [{"id": 1, "title": "T", "brand": "B", "model": "M"}])
        ns.blueprints()
        self.assertEqual(calls, ["/catalog/blueprints.json"], "cached")
        later = datetime.now(timezone.utc) + timedelta(days=8)
        def boom(path):
            raise SystemExit(2)                     # token() exits when there is no token
        ns.printify = lambda: types.SimpleNamespace(call=boom)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ns.blueprints(now=later), [])

    def test_emilys_draft_repeats_the_warning(self):
        ep = load("emily_printify_blank", "emily-printify.py")
        self.write_scan()
        build = self.root / "build"
        build.mkdir()
        self.assertEqual(ep.blank_warning(build, "tshirt", {"blueprint_title": "Unisex Heavy Cotton Tee"}, "tshirt"), [],
                         "no evidence with the build: nothing to say")
        (build / "evidence.json").write_text(json.dumps({"phrase": "frog shirt"}))
        lines = ep.blank_warning(build, "tshirt", {"blueprint_title": "Unisex Heavy Cotton Tee",
                                                    "brand": "Gildan", "model": "5000"}, "tshirt")
        self.assertIn("WARNING: the selling 'frog shirt' listings are on Comfort Colors 1717", lines[0])
        self.assertIn("Drafting anyway", lines[-1])
        same = ep.blank_warning(build, "tshirt", {"blueprint_title": "T", "brand": "Comfort Colors", "model": "1717"}, "tee")
        self.assertEqual(same, ["blank: Comfort Colors 1717 - the same one the selling 'frog shirt' listings use"])
        src = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1]
        self.assertIn("blank_warning(d, cat_key, cat, product_type)", src.split("\ndef ", 1)[0])

    def test_the_build_keeps_its_evidence(self):
        nb = load("emily_new_build_ev", "emily-new-build.py")
        nb.generate_artwork = lambda *a, **k: (True, "ok")
        nb.call = lambda method, path, body=None: (201, {"id": 1})
        argv = ["emily-new-build.py", "Frog Tee", "--product", "tshirt", "--evidence", json.dumps({"phrase": "frog shirt"})]
        with unittest.mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nb.main(), 0)
        self.assertEqual(json.loads((self.root / "agents/emily/builds/frog-tee/evidence.json").read_text()),
                         {"phrase": "frog shirt"})

    def test_pick_and_refresh_keep_brand_and_model(self):
        ep = load("emily_printify_refresh", "emily-printify.py")
        ep.CATALOG = self.root / "agents/emily/state/printify-catalog.json"
        ep.CATALOG.write_text(json.dumps({"tshirt": {"blueprint_id": 706, "blueprint_title": "Garment-Dyed T-shirt"}}))
        ep.call = lambda path, *a, **k: {"title": "Unisex Garment-Dyed T-shirt", "brand": "Comfort Colors", "model": "1717"}
        with contextlib.redirect_stdout(io.StringIO()):
            ep.cmd_refresh(types.SimpleNamespace())
        e = json.loads(ep.CATALOG.read_text())["tshirt"]
        self.assertEqual((e["brand"], e["model"]), ("Comfort Colors", "1717"))
        pick = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_pick(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"brand": bp.get("brand")', pick)


class TheFirstRealNicheRun(unittest.TestCase):
    """2026-10-02, the first droplet run of niche-scan.py. Nine warnings, all
    "(0 of its 0 selling listings name it)" - blanks merely mentioned, not
    what buyers chose; "40oz stainless travel" pointed at a plain 11oz mug
    ahead of Printify's Stainless Steel Travel Mug; 'cat stickers' was added
    as a niche beside 'cat sticker'; and nearly every mug phrase read 0 of 10
    sold, which needs the raw numbers to tell real from a fault."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "agents/scout/state/scans").mkdir(parents=True)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.b = load("blanks_run1", "blanks.py")

    def test_a_blank_nobody_bought_on_is_no_warning(self):
        mentioned = [{"blank": "mug 11oz ceramic", "attrs": ["11oz", "ceramic"], "selling": 0, "mentions": 3}]
        self.assertIsNone(self.b.gap(mentioned, "mug", []))
        sold = [dict(mentioned[0], selling=1)]
        self.assertEqual(self.b.gap(sold, "mug", [])["blank"], "mug 11oz ceramic")

    def test_the_closest_shares_the_most_attributes(self):
        bps = [{"id": 68, "title": "Mug 11oz"}, {"id": 70, "title": "Stainless Steel Travel Mug"},
               {"id": 289, "title": "Latte Mug"}, {"id": 479, "title": "Black Mug (11oz, 15oz)"}]
        travel = self.b.closest({"attrs": ["40oz", "stainless", "travel"]}, "mug", bps)
        self.assertEqual((travel[0]["id"], travel[0]["why"]), (70, "same kind, shares: stainless, travel"))
        black = self.b.closest({"attrs": ["11oz", "black", "ceramic"]}, "mug", bps)
        self.assertEqual(black[0]["id"], 479)

    def test_a_plural_is_the_same_niche(self):
        ns = load("niche_scan_plural", "niche-scan.py")
        ns.STARTER = ["cat sticker"]
        (self.root / "agents/scout/state/scans/x.json").write_text(json.dumps({"seed": "cat sticker meme", "rows": [
            {"phrase": "cat sticker meme", "selling": 6, "sales_n": 10,
             "tags": ["cat stickers", "cute cat sticker", "cute cat stickers"]}]}))
        self.assertEqual(ns.discover({"niches": {}}), ["cute cat sticker"])
        self.assertEqual(ns.same("glass mugs"), "glass mug")
        old = {"niches": {k: {"source": "found"} for k in ("cute cat stickers", "cute cats sticker", "cute cat sticker")}}
        self.assertEqual([s for s, _src, _a in ns.niches(old)], ["cat sticker", "cute cat stickers"],
                         "duplicates saved before the fix are listed once")
        self.assertEqual(ns.same("class pass"), "class pass", "ss is not a plural")

    def test_sales_check_shows_the_raw_numbers(self):
        ms = load("market_scan_check", "market-scan.py")
        now = 1_800_000_000
        def call(path, key):
            if path.startswith("/listings/active"):
                return {"results": [{"listing_id": 1, "shop_id": 9, "num_favorers": 40, "title": "Dog Mom Mug",
                                     "original_creation_timestamp": now - 400 * 86400}]}, {}, None
            if path.startswith("/listings/1/reviews"):
                return {"count": 12, "results": [{"create_timestamp": now - d * 86400}
                                                 for d in (100, 400, 700)]}, {}, None
            if path == "/shops/9":
                return {"transaction_sold_count": 5400}, {}, None
            raise AssertionError(path)
        rows, err = ms.sales_check("k", "dog mom mug", call=call, now=now)
        self.assertIsNone(err)
        self.assertEqual({k: rows[0][k] for k in ("recent", "ever", "shop_sold", "favs")},
                         {"recent": 1, "ever": 12, "shop_sold": 5400, "favs": 40})
        self.assertEqual(rows[0]["last"], "2026-10-07", "the newest review's date, for a person to check")
        self.assertAlmostEqual(rows[0]["age_days"], 400, places=0)
        self.assertIn("sales-check", (SCRIPTS / "market-scan.py").read_text().split("def main(", 1)[1])


class SalesAreAskedOfTheListingsPeopleWant(unittest.TestCase):
    """2026-10-02, the first sales-check on the droplet: the API's top 10 for
    'dog mom mug' were 609 to 3,058 days old with 0-10 favourites and 5
    reviews between them - from shops with 6,270 to 40,012 sales. The count
    was right; the listings were the wrong ones. Sales are now asked of the
    most-favourited 10 of the API's first 100."""

    def setUp(self):
        self.ms = load("market_scan_favs", "market-scan.py")
        self.asked = []
        rows = [{"listing_id": i, "num_favorers": f, "title": f"mug {i}", "description": "11oz ceramic"}
                for i, f in enumerate([0, 9, 0, 263, 4, 0, 10, 1, 5000, 120, 77, 3, 41, 900], start=1)]
        rows.append({"listing_id": "bad", "num_favorers": 99999})
        def call(path, key):
            self.asked.append(path)
            if path.startswith("/listings/active"):
                return {"results": rows}, {}, None
            if "/reviews" in path:
                return {"count": 2}, {}, None
            return {"transaction_sold_count": 1}, {}, None
        self.call = call

    def test_the_most_favourited_of_the_pool(self):
        ids, texts = self.ms.top_by_favs("k", "dog mom mug", call=self.call)
        self.assertEqual(ids, [9, 14, 4, 10, 11, 13, 7, 2, 5, 12])
        self.assertIn("limit=100", self.asked[0])
        self.assertIn("11oz ceramic", texts[9])

    def test_sales_check_shows_the_favourite_order_too(self):
        rows, _ = self.ms.sales_check("k", "dog mom mug", call=self.call, by_favs=True)
        self.assertEqual([r["listing_id"] for r in rows][:3], [9, 14, 4])
        rows, _ = self.ms.sales_check("k", "dog mom mug", call=self.call)
        self.assertEqual([r["listing_id"] for r in rows][:3], [1, 2, 3], "and the API's own order")

    def test_the_scan_uses_the_favourite_order(self):
        src = (SCRIPTS / "market-scan.py").read_text().split("def cmd_scan(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("ids, texts = top_by_favs(key, cand)", src)
        self.assertIn("got = sales(key, ids)", src)


class TheOwnerSetsThePrices(unittest.TestCase):
    """"I will set the prices myself on printify, Emily doesn't need to worry
    about it" (owner, 2026-10-02). So Emily has no price to give: her listing
    command refuses one, the draft never reads one, and her instructions say
    the prices are not hers. A sweatshirt drafted at a sticker's $5.99 because
    she typed it would be the old failure - a model's number reaching a shop."""

    def setUp(self):
        self.m = load("emily_listing_np", "emily-listing.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m.ROOT = Path(self.tmp.name)
        (self.m.ROOT / "agents/emily/builds/b").mkdir(parents=True)

    def invoke(self, *extra):
        real = sys.argv
        sys.argv = ["emily-listing.py", "listing", "b", "--title", "T", "--description", "D",
                    "--tag", "x", "--product", "tshirt"] + list(extra)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return self.m.main()
        except SystemExit as e:
            return e.code
        finally:
            sys.argv = real

    def test_emily_cannot_give_a_price(self):
        self.assertEqual(self.invoke("--price", "5.99"), 2, "argparse refuses an unknown option")
        self.assertFalse((self.m.ROOT / "agents/emily/builds/b/listing.json").exists())
        self.assertEqual(self.invoke(), 0)

    def test_the_draft_reads_no_price_of_hers(self):
        draft = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("price_suggestion", draft)
        self.assertIn("PRICE NOT SET", draft)

    def test_her_instructions_say_so(self):
        h = (ROOT / "agents/emily/_emily-agents-header.md").read_text()
        self.assertIn("## Prices are not yours", h)
        self.assertNotIn("--price", h)
        self.assertNotIn("emily-printify.py market-price", h)


class ScoutsProductIsEmilysProduct(unittest.TestCase):
    """Scout writes products in its own words; Emily's catalogue is keyed by
    the owner's. Before this, "tote bag with zipper" reached Emily as a
    product nothing answered to, or fell to the all-over-print tote - so the
    zipper tote set up for the zipper-tote niches (2026-10-02) could never be
    used. Entries as `pick` saved them on the droplet that day."""

    CAT = {"_fees": {"x": 1},
           "tote": {"blueprint_id": 1389, "blueprint_title": "Tote Bag (AOP)"},
           "zipper-tote": {"blueprint_id": 1990, "blueprint_title": "Zippered Canvas Tote"},
           "tshirt": {"blueprint_id": 706, "blueprint_title": "Unisex Garment-Dyed T-shirt"},
           "sweatshirt": {"blueprint_id": 49, "blueprint_title": "Unisex Heavy Blend™ Crewneck Sweatshirt"},
           "hoodie": {"blueprint_id": 77, "blueprint_title": "Unisex Heavy Blend™ Hooded Sweatshirt"},
           "mug": {"blueprint_id": 478, "blueprint_title": "Ceramic Mug, (11oz, 15oz)"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "agents/emily/state").mkdir(parents=True)
        (root / "agents/emily/state/printify-catalog.json").write_text(json.dumps(self.CAT))
        os.environ["ECOSYSTEM_ROOT"] = str(root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.m = load("scout_ideas_key", "scout-ideas.py")

    def test_scouts_words_land_on_the_owners_products(self):
        for said, key in [("tote bag with zipper", "zipper-tote"), ("zipper tote", "zipper-tote"),
                          ("tote", "tote"), ("canvas tote bag", "tote"),
                          ("t-shirt", "tshirt"), ("comfort colors tee", "tshirt"),
                          ("hooded sweatshirt", "hoodie"), ("crewneck", "sweatshirt"),
                          ("coffee mug", "mug")]:
            self.assertEqual(self.m.emily_key(said), key, said)

    def test_nothing_set_up_is_left_as_written(self):
        self.assertIsNone(self.m.emily_key("poster"))

    def test_propose_files_the_key(self):
        src = (SCRIPTS / "scout-ideas.py").read_text().split("def cmd_propose(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"product": emily_key(a.product) or a.product.strip()', src)


class TheFirstTestDraftsPrintRight(unittest.TestCase):
    """The owner's first end-to-end run (2026-10-02) drafted two products and
    both were wrong in ways no check caught:

      * "Cozy Bookish Merch Sweat" read "I'M JUST HE HER FOR THE BOOKISH
        MERCH" and sat small and low on the front.
      * "Threaded Spine Chest Emblem" carried that day's date, 2026-10-02,
        and was drafted as a sticker - Scout proposed an embroidered emblem,
        which Emily has no product for.

    So: a cut-out is trimmed to its art and fitted to the measured print
    area, top of the chest on a garment; the words are proofread as the art
    is drawn and again before upload; and Scout cannot propose a product
    Emily has not been set up for."""

    def setUp(self):
        self.k = load("knockout_trim", "knockout.py")
        self.ep = load("emily_printify_fit", "emily-printify.py")
        self.ea = load("emily_assets_proof", "emily-assets.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    # --- placement ---------------------------------------------------------

    def art_in_a_corner(self):
        """A 120x120 cream square with a 60x40 red block low and to the left -
        where the bookish text sat in its frame."""
        w = h = 120
        px = bytearray()
        for y in range(h):
            for x in range(w):
                inside = 10 <= x < 70 and 70 <= y < 110
                px += bytes([200, 30, 30, 255]) if inside else bytes([245, 240, 225, 255])
        src = self.dir / "design.png"
        self.k.encode(src, w, h, px)
        return src

    def test_a_cutout_is_trimmed_to_its_art(self):
        src, out = self.art_in_a_corner(), self.dir / "cut.png"
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"), str(src), str(out)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        w, h = self.k.size(out)
        self.assertTrue(w < 70 and h < 50, f"the empty frame around the art is gone: {w}x{h}")
        self.assertTrue(w >= 60 and h >= 40, f"and none of the art: {w}x{h}")

    def test_nothing_visible_is_left_alone(self):
        self.assertEqual(self.k.trim(10, 10, bytearray(400))[:2], (10, 10))

    def test_a_garments_art_goes_to_the_top_of_its_area(self):
        crew = {"variant_titles": ["S / White", "M / White"]}
        img = self.ep.print_areas("I", (900, 420), [1, 2], crew,
                                  {1: (3600, 4200), 2: (3600, 4200)})[0]["placeholders"][0]["images"][0]
        self.assertEqual((img["x"], img["scale"]), (0.5, 1.0), "centred across, full width")
        top = img["y"] - img["scale"] * 3600 * 420 / 900 / 4200 / 2
        self.assertAlmostEqual(top, 0.0, places=3)

    def test_tall_art_is_shrunk_to_fit_not_cropped(self):
        img = self.ep.print_areas("I", (400, 900), [1], {"variant_titles": ["L / Ash"]},
                                  {1: (3600, 4200)})[0]["placeholders"][0]["images"][0]
        self.assertLessEqual(img["scale"] * 3600 * 900 / 400, 4200 + 1)

    def test_anything_not_worn_is_centred(self):
        img = self.ep.print_areas("I", (900, 420), [1], {"variant_titles": ["11oz"]},
                                  {1: (2700, 1050)})[0]["placeholders"][0]["images"][0]
        self.assertEqual(img["y"], 0.5)

    def test_without_a_measurement_nothing_moves(self):
        self.assertEqual(self.ep.print_areas("I", (900, 420), [1], {"variant_titles": ["S"]}),
                         self.ep.print_areas("I", (900, 420), [1], {}, None))
        self.assertEqual(self.ep.print_areas("I", (900, 420), [1, 2], {}, {1: (10, 10)})[0]
                         ["placeholders"][0]["images"][0]["scale"], 1, "a variant unmeasured: as before")

    # --- the words -----------------------------------------------------------

    def test_the_two_real_mistakes_are_caught(self):
        sweat = self.ea.read_proof('```json\n{"lines": ["I\'M JUST HE HER", "FOR THE BOOKISH MERCH.", '
                                   '"EST. 2026"], "errors": ["HE HER -> HERE"]}\n```')
        self.assertEqual(self.ea.proof_problems(sweat), ["text error: HE HER -> HERE"])
        emblem = self.ea.read_proof('{"lines": ["2026-10-02"], "errors": []}')
        self.assertEqual(self.ea.proof_problems(emblem), ["prints a date: '2026-10-02'"])

    def test_a_year_is_a_style_and_a_date_is_not(self):
        ok = {"lines": ["EST. 2026", "Since May 2020", "Book Club"], "errors": []}
        self.assertEqual(self.ea.proof_problems(ok), [])
        for d in ("10/02/2026", "Oct 2", "2 October", "2026.10.02"):
            self.assertTrue(self.ea.proof_problems({"lines": [d]}), d)

    def test_an_unreadable_reply_is_not_a_pass(self):
        self.assertIsNone(self.ea.read_proof("Looks great!"))
        self.assertIsNone(self.ea.read_proof("{not json}"))

    def test_a_mistake_is_drawn_again(self):
        calls, prompts = [], []

        def generate(path, prompt, key, model=None, product=""):
            prompts.append(prompt)
            Path(path).write_bytes(b"x" * 3000)
            return 3000, {}

        def proof(path, key, model=None, name=None):
            calls.append(1)
            return (["text error: HE HER -> HERE"], ["I'M JUST HE HER"]) if len(calls) == 1 \
                else ([], ["I'M JUST HERE"])

        self.ea.generate, self.ea.proof = generate, proof
        self.ea.load_credentials = lambda: None
        real = sys.argv
        sys.argv = ["emily-assets.py", "--prompt", "bookish", "--out", str(self.dir / "d.png")]
        os.environ["OPENROUTER_API_KEY"] = "test-not-a-key"
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                self.ea.main()
        finally:
            sys.argv = real
        rep = json.loads(out.getvalue())
        self.assertEqual((rep["attempts"], rep["proof"]), (2, "clean"))
        self.assertIn("HE HER -> HERE", prompts[1], "told what went wrong")

    def test_the_draft_reads_the_words_before_uploading(self):
        body = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("proofread(design, ", body)
        self.assertLess(body.index("proofread(design, "), body.index("/uploads/images.json"))
        self.assertIn("measured", body.split("print_areas(image_id", 1)[1][:120])

    def test_the_art_direction_forbids_dates(self):
        self.assertIn("Never draw a date", self.ea.PRINT_DIRECTION)

    # --- the product ---------------------------------------------------------

    def test_scout_cannot_propose_what_emily_cannot_make(self):
        root = self.dir / "eco"
        state = root / "agents/scout/state"
        (state / "scans").mkdir(parents=True)
        (state / "ideas.json").write_text('{"ideas":[]}')
        (state / "scans/s.json").write_text(json.dumps({
            "seed": "bookish sweatshirt", "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "rows": [{"phrase": "bookish sweatshirt embroidered", "supply": 5000, "heat": 0.05, "pull": 0.04,
                      "price": 40.0, "match": 1.0, "selling": 8, "sales_n": 10, "reviews": 60,
                      "returned": 25, "heat_n": 25, "pull_n": 25, "score": 0.02}], "excluded": []}))
        (root / "agents/emily/state").mkdir(parents=True)
        (root / "agents/emily/state/printify-catalog.json").write_text(json.dumps({
            "sticker": {"blueprint_id": 400, "blueprint_title": "Round Vinyl Stickers"},
            "sweatshirt": {"blueprint_id": 49, "blueprint_title": "Unisex Heavy Blend™ Crewneck Sweatshirt"}}))
        env = dict(os.environ, ECOSYSTEM_ROOT=str(root))

        def propose(product):
            return subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), "propose",
                                   "--phrase", "bookish sweatshirt embroidered",
                                   "--title", "Threaded Spine Chest Emblem", "--product", product],
                                  capture_output=True, text=True, env=env)
        r = propose("embroidered chest emblem")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("She can make: sticker, sweatshirt", r.stderr)
        r = propose("crewneck sweatshirt")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        filed = json.loads((state / "proposals.json").read_text())["proposals"][0]
        self.assertEqual(filed["product"], "sweatshirt")
        brief = subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), "brief"],
                               capture_output=True, text=True, env=env)
        self.assertIn("products Emily can make (anything else is refused): sticker, sweatshirt", brief.stdout)

    def listing(self, *extra, order=None):
        m = load("emily_listing_order", "emily-listing.py")
        m.ROOT = self.dir
        b = self.dir / "agents/emily/builds/emblem"
        b.mkdir(parents=True, exist_ok=True)
        if order:
            (b / "order.json").write_text(json.dumps({"product": order}))
        real, err = sys.argv, io.StringIO()
        sys.argv = ["emily-listing.py", "listing", "emblem", "--title", "Threaded Spine",
                    "--description", "D", "--tag", "bookish", *extra]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = m.main()
        finally:
            sys.argv = real
        got = b / "listing.json"
        return code, (json.loads(got.read_text())["product_type"] if got.exists() else None), err.getvalue()

    def test_a_listing_without_a_product_is_the_product_ordered(self):
        code, product, _ = self.listing(order="sweatshirt")
        self.assertEqual((code, product), (0, "sweatshirt"), "not the old sticker default")

    def test_a_listing_for_another_product_is_refused(self):
        code, product, err = self.listing("--product", "sticker", order="sweatshirt")
        self.assertEqual((code, product), (2, None))
        self.assertIn("ordered as 'sweatshirt'", err)

    def test_the_build_records_what_was_ordered(self):
        src = (SCRIPTS / "emily-new-build.py").read_text()
        self.assertIn('"order.json"', src)
        self.assertIn('{"product": a.product, "name": a.idea, "brief": a.brief}', src)
        h = (ROOT / "agents/emily/_emily-agents-header.md").read_text()
        self.assertNotIn("--product sticker", h, "no product in the example to copy")


class APlaceholderIsNeverDrafted(unittest.TestCase):
    """2026-10-02: "Shift-Ready Nurse Tee" went to Printify as the geometric
    placeholder - concentric bands on a dark field - because drawing the real
    art failed and every "is this artwork" check passes flat geometry on a
    plain background. knockout --check called it "fit to print". The
    placeholder now carries a mark, and draft refuses anything carrying it."""

    def setUp(self):
        self.ea = load("emily_assets_ph", "emily-assets.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.png = Path(self.tmp.name) / "design.png"

    def test_the_placeholder_is_marked_and_real_art_is_not(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "emily-assets.py"), "--prompt", "Night Shift RN",
                            "--out", str(self.png), "--placeholder-only"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ea.is_placeholder(self.png))
        k = load("knockout_ph", "knockout.py")
        art = Path(self.tmp.name) / "art.png"
        k.encode(art, 4, 4, bytearray([10, 20, 30, 255] * 16))
        self.assertFalse(self.ea.is_placeholder(art))
        self.assertFalse(self.ea.is_placeholder(Path(self.tmp.name) / "missing.png"))

    def test_draft_refuses_it_before_anything_else_is_asked(self):
        body = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("is_placeholder(design)", body)
        self.assertLess(body.index("is_placeholder(design)"), body.index("/uploads/images.json"))

    def test_redrawing_stops_before_the_art_step_is_killed(self):
        clock = iter([0, 0, 200, 200, 400])
        # A stand-in for this module's `time` only - patching time.monotonic
        # itself would move the clock for every test after this one.
        self.ea.time = types.SimpleNamespace(monotonic=lambda: next(clock))
        n = []

        def generate(path, prompt, key, model=None, product=""):
            n.append(1)
            Path(path).write_bytes(b"x" * 3000)
            return 3000, {}
        self.ea.generate = generate
        self.ea.proof = lambda path, key, model=None, name=None: (["text error: HE HER -> HERE"], ["HE HER"])
        self.ea.load_credentials = lambda: None
        real = sys.argv
        sys.argv = ["emily-assets.py", "--prompt", "p", "--out", str(self.png)]
        os.environ["OPENROUTER_API_KEY"] = "test-not-a-key"
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                self.ea.main()
        finally:
            sys.argv = real
        rep = json.loads(out.getvalue())
        self.assertLess(len(n), self.ea.PROOF_TRIES, "no third drawing past the budget")
        self.assertIn("out of time", rep["proof"])
        self.assertLess(self.ea.REDRAW_BUDGET_S, 180, "inside emily-new-build's limit")


class TheDeckWaitsForTheArt(unittest.TestCase):
    """Approving an idea draws its art first (emily-new-build allows 180s,
    proofread redraws included). The Deck's approve call gave up after 60s,
    so an approval from the dashboard could be killed mid-drawing."""

    def test_the_deck_outwaits_the_art_step(self):
        js = (ROOT / "mission-control-api" / "scout.js").read_text()
        ms = int(re.search(r"timeout:\s*(\d+)", js.split("function runReview", 1)[1]).group(1))
        art = int(re.search(r"timeout=(\d+)", (SCRIPTS / "emily-new-build.py").read_text()
                            .split("def generate_artwork(", 1)[1]).group(1))
        self.assertGreater(ms / 1000, art)


class AceWakesBeforeTheEarlyKickoffs(unittest.TestCase):
    """The betting wake was 15:00 ET. College football's noon ET (11:00
    Central) kickoffs had started by then, and NFL Sunday's 13:00 games were
    13.5 hours after the 23:30 wake - outside BET_WINDOW_HOURS - so no wake
    could ever bet them. Moved to 11:30 ET on 2026-10-03 (owner)."""

    def wakes(self):
        timer = (ROOT / "deploy" / "ace-cycle.timer").read_text()
        return [int(h) * 60 + int(m) for h, m in
                re.findall(r"^OnCalendar=\*-\*-\* (\d\d):(\d\d) America/New_York", timer, re.M)]

    def test_every_usual_kickoff_is_inside_some_wakes_window(self):
        window = load("ace_fetch_window", "ace-fetch.py").BET_WINDOW_HOURS * 60
        wakes = self.wakes()
        # today's wakes, and yesterday's (a minus-one-day offset)
        every = [w for w in wakes] + [w - 24 * 60 for w in wakes]
        for name, kick in [("college noon", 12 * 60), ("NFL early", 13 * 60),
                           ("college afternoon", 15 * 60 + 30), ("NFL late", 16 * 60 + 25),
                           ("night games", 19 * 60 + 30)]:
            self.assertTrue(any(0 < kick - w <= window for w in every), f"{name} is never bettable")

    def test_the_wake_keeps_its_slot_name(self):
        et = load("et_time_ace", "et_time.py")
        for minute in self.wakes():
            when = datetime(2026, 10, 3, minute // 60, minute % 60, tzinfo=timezone(timedelta(hours=-4)))
            self.assertIn(et.slot("ace", when), ("afternoon", "night"))
        self.assertIn(11 * 60 + 30, self.wakes())


class TheDesignDoesNotPrintItsOwnName(unittest.TestCase):
    """2026-10-03: a mug came back reading "TEACHER'S COFFEE PLOT MUG" - its
    own product name, every word spelled right, so the proofread passed it.
    The idea title went into the art prompt bare, and the image model drew
    it. Now it is labelled as a name never to draw, and the proofread refuses
    art whose words spell out the name."""

    def setUp(self):
        self.ea = load("emily_assets_name", "emily-assets.py")

    def test_the_real_mug_is_refused(self):
        lines = ["TEACHER'S", "COFFEE PLOT MUG"]          # as drawn
        got = self.ea.proof_problems({"lines": lines, "errors": []}, "Teacher's Coffee Plot Mug")
        self.assertEqual(len(got), 1)
        self.assertIn("product's own name", got[0])

    def test_a_design_that_shares_a_word_is_fine(self):
        for lines in (["Coffee & Plot Twists"], ["This Is My Reading Shirt"], []):
            self.assertEqual(self.ea.proof_problems({"lines": lines}, "Teacher's Coffee Plot Mug"), [], lines)
        self.assertEqual(self.ea.proof_problems({"lines": ["COFFEE PLOT MUG"]}), [], "no name, no check")

    def test_the_prompt_says_the_name_is_not_text(self):
        said = self.ea.compose("Teacher's Coffee Plot Mug", "", "mug", None)
        self.assertIn("NEVER text on the design: Teacher's Coffee Plot Mug", said)

    def test_the_name_reaches_both_proofreads(self):
        nb = (SCRIPTS / "emily-new-build.py").read_text()
        self.assertIn('"--name", str(idea or "")', nb)
        main = (SCRIPTS / "emily-assets.py").read_text().split("def main(", 1)[1]
        self.assertIn("proof(a.out, key, name=a.name or None)", main)
        draft = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('proofread(design, name or listing.get("title"))', draft)


class StuckBuildsAreRedrawn(unittest.TestCase):
    """2026-10-03: "Library Stacks Linework Tote" sat LOCAL ONLY - the day's
    image money had run out, the art step fell back to the placeholder, and
    draft rightly refused it. Nothing ever tried again. emily-finish.py
    --redraw (hourly, redraw-art.timer) redraws such builds from what they
    were ordered as and drafts the ones that come back as real art."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.builds = self.root / "agents/emily/builds"
        self.fin = load("emily_finish_redraw", "emily-finish.py")
        self.fin.ROOT = self.root
        (self.root / "scripts").symlink_to(SCRIPTS)
        self.drafted, self.drawn = [], []
        self.fin.draft = lambda d: self.drafted.append(d.name) or 0

    def build(self, name, placeholder=True, drafted=False, age=0):
        d = self.builds / name
        d.mkdir(parents=True)
        (d / "listing.json").write_text(json.dumps({"title": "Library Tote", "product_type": "tote"}))
        (d / "order.json").write_text(json.dumps({"product": "zipper-tote", "name": "Library Stacks Linework Tote",
                                                  "brief": "clean linear shelves"}))
        (d / "build.json").write_text(json.dumps({"printify_product_id": "p1"} if drafted else {}))
        if placeholder:
            subprocess.run([sys.executable, str(SCRIPTS / "emily-assets.py"), "--prompt", name,
                            "--out", str(d / "design.png"), "--placeholder-only"], capture_output=True)
        else:
            (d / "design.png").write_bytes(b"\x89PNG real art" + b"x" * 3000)
        os.utime(d / "design.png", (1000 + age, 1000 + age))
        return d

    def fake_art(self, real):
        def generate_artwork(slug, idea, brief, product, evidence=None):
            self.drawn.append((slug, idea, brief, product))
            if real:
                (self.builds / slug / "design.png").write_bytes(b"\x89PNG real art" + b"x" * 3000)
            return True, "design.png (generated)" if real else "design.png (placeholder)"
        nb = types.SimpleNamespace(generate_artwork=generate_artwork)
        real_module = self.fin._module
        self.fin._module = lambda name, file: nb if file == "emily-new-build.py" else real_module(name, file)

    def test_a_placeholder_build_is_redrawn_and_drafted(self):
        self.build("library-stacks-linework-tote")
        self.build("already-drafted", drafted=True)
        self.build("real-art", placeholder=False)
        self.fake_art(real=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.fin.redraw()
        self.assertEqual(self.drawn, [("library-stacks-linework-tote", "Library Stacks Linework Tote",
                                       "clean linear shelves", "zipper-tote")], "as ordered, only the stuck one")
        self.assertEqual(self.drafted, ["library-stacks-linework-tote"])

    def test_still_failing_stops_the_run_and_says_so(self):
        self.build("oldest", age=0)
        self.build("newer", age=50)
        self.fake_art(real=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.fin.redraw()
        self.assertEqual([x[0] for x in self.drawn], ["oldest"], "one attempt, not one per build")
        self.assertEqual(self.drafted, [])
        blocked = json.loads((self.builds / "oldest/build.json").read_text())["draft_blocked"]
        self.assertIn("redraw still failing", blocked["reason"])

    def test_the_timer_runs_it(self):
        self.assertIn("emily-finish.py --redraw", (ROOT / "deploy/redraw-art.service").read_text())
        self.assertIn("OnCalendar=", (ROOT / "deploy/redraw-art.timer").read_text())
        nb = (SCRIPTS / "emily-new-build.py").read_text()
        self.assertIn('"brief": a.brief', nb)
        self.assertIn('root = Path(os.environ.get("ECOSYSTEM_ROOT", here.parent))', nb)


class NoRealArtNoEmily(unittest.TestCase):
    """2026-10-03: "dad s little boss tee" - the OpenRouter money was out, the
    art came back as the placeholder, Emily was woken anyway, her run ended
    at once, and the card said FAILED with nothing retrying it (the redraw
    only knew builds Emily had already listed). Now the build waits for its
    art, and the redraw sends it to Emily once the art is real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "ECOSYSTEM_ROOT", None)
        self.nb = load("emily_new_build_hold", "emily-new-build.py")
        self.posted = []
        self.nb.call = lambda method, path, body=None: (self.posted.append(body) or (201, {"id": 7}))

    def approve(self, real_art):
        def generate_artwork(slug, idea, brief, product, evidence=None):
            out = self.root / "agents/emily/builds" / slug / "design.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            if real_art:
                out.write_bytes(b"\x89PNG real" + b"x" * 3000)
            else:
                subprocess.run([sys.executable, str(SCRIPTS / "emily-assets.py"), "--prompt", idea,
                                "--out", str(out), "--placeholder-only"], capture_output=True)
            return True, "design.png"
        self.nb.generate_artwork = generate_artwork
        real = sys.argv
        sys.argv = ["emily-new-build.py", "Dad's Little Boss Tee", "--product", "tshirt", "--brief", "bold"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return self.nb.main()
        finally:
            sys.argv = real

    def test_a_placeholder_holds_the_build_and_wakes_nobody(self):
        self.assertEqual(self.approve(real_art=False), 0, "the approval still counts")
        self.assertEqual(self.posted, [], "Emily is not woken")
        b = json.loads((self.root / "agents/emily/builds/dad-s-little-boss-tee/build.json").read_text())
        self.assertEqual(b["status"], "waiting_for_art")

    def test_real_art_goes_to_emily_as_before(self):
        self.assertEqual(self.approve(real_art=True), 0)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.posted[0]["assignee"], "emily")

    def test_the_redraw_sends_a_held_build_to_emily(self):
        self.approve(real_art=False)
        fin = load("emily_finish_hold", "emily-finish.py")
        fin.ROOT = self.root
        (self.root / "scripts").symlink_to(SCRIPTS)
        sent = []

        def generate_artwork(slug, idea, brief, product, evidence=None):
            (self.root / "agents/emily/builds" / slug / "design.png").write_bytes(b"\x89PNG real" + b"x" * 3000)
            return True, "design.png (generated)"
        fake = types.SimpleNamespace(generate_artwork=generate_artwork,
                                     queue=lambda *a: sent.append(a) or 0)
        real_module = fin._module
        fin._module = lambda name, file: fake if file == "emily-new-build.py" else real_module(name, file)
        fin.draft = lambda d: self.fail("nothing to draft before Emily has listed it")
        with contextlib.redirect_stdout(io.StringIO()):
            fin.redraw()
        self.assertEqual(sent[0][:4], ("dad-s-little-boss-tee", "Dad's Little Boss Tee", "bold", "tshirt"))
        b = json.loads((self.root / "agents/emily/builds/dad-s-little-boss-tee/build.json").read_text())
        self.assertNotIn("status", b, "no longer waiting - the queue says what happens next")

    def test_the_card_says_waiting_not_failed(self):
        js = (ROOT / "mission-control-api/emily.js").read_text()
        self.assertIn("case 'waiting_for_art':", js)
        self.assertIn("label: 'waiting for art'", js)


class ARedrawSaysWhyTheArtFailed(unittest.TestCase):
    """2026-10-04: the redraw reported "design.png 17 KB (placeholder)" for two
    builds and not one word of why - emily-assets puts the reason on stderr
    and generate_artwork kept only the mode. And the dad tee, never reached
    by Emily, had no build.json, so its card could not say anything either."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_the_reason_is_read_from_what_emily_assets_really_prints(self):
        ea = load("emily_assets_why", "emily-assets.py")
        ea.load_credentials = lambda: None

        def generate(*a, **k):
            raise RuntimeError("HTTP Error 402: Payment Required")
        ea.generate = generate
        os.environ["OPENROUTER_API_KEY"] = "test-not-a-key"
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
        real, err = sys.argv, io.StringIO()
        sys.argv = ["emily-assets.py", "--prompt", "p", "--out", str(self.dir / "d.png")]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                ea.main()
        finally:
            sys.argv = real
        nb = load("emily_new_build_why", "emily-new-build.py")
        self.assertEqual(nb.why_failed(err.getvalue()), "HTTP Error 402: Payment Required")
        self.assertEqual(nb.why_failed(""), "no reason given")

    def test_a_build_with_no_record_gets_one_for_the_reason(self):
        fin = load("emily_finish_why", "emily-finish.py")
        (self.dir / "dad-s-little-boss-tee").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            fin.note_draft(self.dir / "dad-s-little-boss-tee", {"reason": "redraw still failing: 402"})
        b = json.loads((self.dir / "dad-s-little-boss-tee/build.json").read_text())
        self.assertEqual(b["draft_blocked"]["reason"], "redraw still failing: 402")

    def test_an_unreadable_record_is_still_left_alone(self):
        fin = load("emily_finish_why2", "emily-finish.py")
        (self.dir / "b").mkdir()
        (self.dir / "b/build.json").write_text("{ broken")
        with contextlib.redirect_stdout(io.StringIO()):
            fin.note_draft(self.dir / "b", {"reason": "x"})
        self.assertEqual((self.dir / "b/build.json").read_text(), "{ broken")

    def test_the_message_carries_it(self):
        src = (SCRIPTS / "emily-new-build.py").read_text().split("def generate_artwork(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("why_failed(res.stderr)", src)


class PreflightReadsTheKeysCapNotTheAccount(unittest.TestCase):
    """2026-10-04: preflight said "credit $-16.13 left of $1.00" while the
    account held $12.88, and advised topping up. It subtracted the key's
    LIFETIME spend from its $1 DAILY cap. OpenRouter reports what is left of
    the cap as limit_remaining, and the period as limit_reset (its docs)."""

    def run_check(self, data):
        pf = load("preflight_cap", "preflight.py")
        pf.openrouter_key = lambda: "test-not-a-key"
        pf.key_status = lambda key, timeout=20: data
        pf.BLOCK.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pf.check_credit()
        return out.getvalue(), list(pf.BLOCK)

    def test_the_real_numbers(self):
        out, block = self.run_check({"limit": 1.0, "limit_remaining": 0.0, "limit_reset": "daily",
                                     "usage": 17.13, "usage_daily": 1.02})
        self.assertIn("$0.00 left of $1.00 a day", out)
        self.assertNotIn("-16.13", out)
        self.assertIn("account balance is separate", out)
        self.assertEqual(len(block), 1)
        self.assertIn("midnight UTC", block[0][1])
        self.assertNotIn("top up", block[0][2], "the account had money; the cap was the stop")

    def test_a_cap_with_room_left_blocks_nothing(self):
        out, block = self.run_check({"limit": 1.0, "limit_remaining": 0.62, "limit_reset": "daily",
                                     "usage": 17.13})
        self.assertIn("$0.62 left of $1.00 a day", out)
        self.assertEqual(block, [])


class ImageCallsFitTheDailyCap(unittest.TestCase):
    """2026-10-04: every drawing was refused with 402 while the key still had
    $0.64 of its $1.00 day. OpenRouter prices a request at its worst case -
    input plus max_tokens - before running it (its docs), and the image call
    set no max_tokens, so its worst case was the model's whole allowance. And
    the error kept only "Payment Required", not OpenRouter's explanation."""

    def setUp(self):
        self.ea = load("emily_assets_cap", "emily-assets.py")

    def capture(self, fn):
        sent = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"choices": [{"message": {"content": "{\\"lines\\": []}"}}]}'

        def urlopen(req, timeout=None):
            sent["body"] = json.loads(req.data)
            return Resp()
        old = self.ea.urllib.request.urlopen
        self.ea.urllib.request.urlopen = urlopen
        try:
            try:
                fn()
            except RuntimeError:
                pass                                  # no image in the stub reply
        finally:
            self.ea.urllib.request.urlopen = old
        return sent["body"]

    def test_both_requests_say_how_much_they_may_write(self):
        body = self.capture(lambda: self.ea.generate("/dev/null", "p", "k", model="m"))
        self.assertEqual(body["max_tokens"], self.ea.IMAGE_MAX_TOKENS)
        png = Path(tempfile.mkdtemp()) / "a.png"
        png.write_bytes(b"\x89PNG")
        body = self.capture(lambda: self.ea.proof(png, "k"))
        self.assertEqual(body["max_tokens"], self.ea.PROOF_MAX_TOKENS)

    def test_a_refusal_keeps_openrouters_reason(self):
        import urllib.error
        msg = ("This request requires more credits, or fewer max_tokens. You requested up to "
               "32768 tokens, but can only afford 5294.")

        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError(OR := "https://openrouter.ai", 402, "Payment Required", {},
                                         io.BytesIO(json.dumps({"error": {"code": 402, "message": msg}}).encode()))
        old = self.ea.urllib.request.urlopen
        self.ea.urllib.request.urlopen = urlopen
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.ea.generate("/dev/null", "p", "k", model="m")
        finally:
            self.ea.urllib.request.urlopen = old
        self.assertIn("HTTP 402: This request requires more credits", str(ctx.exception))
        self.assertIn("can only afford 5294", str(ctx.exception))


class TheEarlyDraftsAreRedone(unittest.TestCase):
    """The first test drafts went to Printify before the proofread, the name
    check, the trim and the placeholder refusal existed: "I'M JUST HE HER",
    a date, "TEACHER'S COFFEE PLOT MUG", a placeholder nurse tee, and an
    emblem drafted as a sticker. emily-finish.py --redo deletes the old
    UNPUBLISHED draft, draws again through today's pipeline and drafts again;
    anything live on Etsy is refused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fin = load("emily_finish_redo", "emily-finish.py")
        self.fin.ROOT = self.root
        (self.root / "scripts").symlink_to(SCRIPTS)
        self.d = self.root / "agents/emily/builds/threaded-spine-chest-emblem"
        self.d.mkdir(parents=True)
        (self.d / "build.json").write_text(json.dumps({"idea": "Threaded Spine Chest Emblem",
            "printify_product_id": "p1", "printify_shop_id": "s1", "status": "ready_for_review",
            "printify_drafts": [{"product_id": "p1"}]}))
        (self.d / "listing.json").write_text(json.dumps({"title": "Threaded Spine Emblem",
                                                         "product_type": "sticker", "description": "d"}))
        (self.d / "design.png").write_bytes(b"\x89PNG old" + b"x" * 3000)
        self.deleted, self.drafted, self.drawn = [], [], []
        self.live = {"published": False, "locked": False}

        class ApiError(Exception):
            def __init__(self, code): self.code = code
        ep = types.SimpleNamespace(
            ApiError=ApiError, load_credentials=lambda: None,
            _live_state=lambda shop, pid: dict(self.live),
            call=lambda path, body=None, method=None, soft=False: self.deleted.append((method, path)) or {})

        def generate_artwork(slug, idea, brief, product, evidence=None):
            self.drawn.append((idea, product))
            (self.d / "design.png").write_bytes(b"\x89PNG new" + b"x" * 3000)
            return True, "design.png (generated)"
        nb = types.SimpleNamespace(generate_artwork=generate_artwork)
        real = self.fin._module
        self.fin._module = lambda name, file: {"emily-printify.py": ep, "emily-new-build.py": nb}.get(file) \
            or real(name, file)
        self.fin.draft = lambda d: self.drafted.append(d.name) or 0

    def redo(self, *argv):
        real = sys.argv
        sys.argv = ["emily-finish.py", "--redo", *argv]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return self.fin.main()
        finally:
            sys.argv = real

    def test_redraft_keeps_the_art_and_drafts_again(self):
        # "Paws & Kissies" (2026-10-07): the art was right, the cut and the
        # placement were not. --redraft deletes the unpublished draft and
        # drafts the same design.png - nothing is drawn.
        self.fin._module = (lambda real: lambda name, file: types.SimpleNamespace(is_placeholder=lambda p: False)
                            if file == "emily-assets.py" else real(name, file))(self.fin._module)
        real = sys.argv
        sys.argv = ["emily-finish.py", "--redraft", "threaded-spine-chest-emblem"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.fin.main(), 0)
        finally:
            sys.argv = real
        self.assertEqual(self.deleted, [("DELETE", "/shops/s1/products/p1.json")])
        self.assertEqual(self.drawn, [], "the art is kept")
        self.assertEqual(self.drafted, ["threaded-spine-chest-emblem"])
        self.assertTrue((self.d / "design.png").read_bytes().startswith(b"\x89PNG old"))

    def test_a_live_product_is_never_deleted(self):
        self.live["published"] = True
        self.assertEqual(self.redo("threaded-spine-chest-emblem"), 1)
        self.assertEqual((self.deleted, self.drawn, self.drafted), ([], [], []))

    def test_an_unpublished_draft_is_replaced_on_the_right_product(self):
        self.assertEqual(self.redo("threaded-spine-chest-emblem", "--product", "sweatshirt"), 0)
        self.assertEqual(self.deleted, [("DELETE", "/shops/s1/products/p1.json")])
        self.assertEqual(self.drawn, [("Threaded Spine Chest Emblem", "sweatshirt")])
        self.assertEqual(self.drafted, ["threaded-spine-chest-emblem"])
        b = json.loads((self.d / "build.json").read_text())
        self.assertNotIn("printify_product_id", b)
        self.assertEqual(json.loads((self.d / "listing.json").read_text())["product_type"], "sweatshirt")
        self.assertEqual(json.loads((self.d / "order.json").read_text())["product"], "sweatshirt")


class ARoundStickerKeepsItsArtInsideTheCut(unittest.TestCase):
    """2026-10-04: the "Keep Going" sticker - a round badge with ONE DAY AT A
    TIME across the bottom - was placed as a full-width square on Printify's
    Round Vinyl Stickers, and the circular cut took the ends off "AT A TIME"
    at every size. On a round product the art's farthest visible pixel must
    sit inside the circle; a round badge is not shrunk for empty corners."""

    def setUp(self):
        self.ep = load("emily_printify_round", "emily-printify.py")
        self.k = load("knockout_round", "knockout.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.round = {"blueprint_title": "Round Vinyl Stickers", "variant_titles": ['2" × 2"']}

    def art(self, name, inside):
        w = h = 120
        px = bytearray(w * h * 4)
        for y in range(h):
            for x in range(w):
                if inside(x, y):
                    px[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes([30, 60, 90, 255])
        p = Path(self.tmp.name) / f"{name}.png"
        self.k.encode(p, w, h, px)
        return p

    def placed(self, path, entry):
        reach = self.ep.round_reach(path, step=1)
        img = self.ep.print_areas("I", (120, 120), [1], entry, {1: (1800, 1800)}, reach)
        return img[0]["placeholders"][0]["images"][0]["scale"], reach

    def test_the_badge_with_text_across_the_bottom_stays_inside(self):
        badge = self.art("badge", lambda x, y: (x - 60) ** 2 + (y - 60) ** 2 <= 50 ** 2 or y >= 100)
        scale, reach = self.placed(badge, self.round)
        self.assertLessEqual(scale * reach, self.ep.ROUND_SAFE + 1e-6, "its farthest pixel is inside the cut")
        self.assertLess(scale, 0.9)

    def test_a_round_badge_is_not_shrunk_for_empty_corners(self):
        disc = self.art("disc", lambda x, y: (x - 60) ** 2 + (y - 60) ** 2 <= 59 ** 2)
        scale, _ = self.placed(disc, self.round)
        self.assertGreater(scale, 0.9)

    def test_a_square_cut_product_is_untouched(self):
        full = self.art("full", lambda x, y: True)
        scale, _ = self.placed(full, {"blueprint_title": "Kiss-Cut Stickers", "variant_titles": ['3" × 3"']})
        self.assertEqual(scale, 1.0)

    def test_draft_measures_it(self):
        body = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("round_reach(upload_from) if is_round(cat) else None", body)

    def test_unmeasured_it_still_stays_inside(self):
        # "Paws & Kissies" (2026-10-07): with no print area read, the fallback
        # placed it centred at full width and the circle took the ends off the
        # lettering. A round cut is square, so the fit needs no measurement.
        badge = self.art("badge2", lambda x, y: (x - 60) ** 2 + (y - 60) ** 2 <= 50 ** 2 or y >= 100)
        reach = self.ep.round_reach(badge, step=1)
        img = self.ep.print_areas("I", (120, 120), [1], self.round, None, reach)[0]["placeholders"][0]["images"][0]
        self.assertLessEqual(img["scale"] * reach, self.ep.ROUND_SAFE + 1e-6)
        self.assertLess(img["scale"], 0.9)
        square = self.ep.print_areas("I", (120, 120), [1], {"blueprint_title": "Kiss-Cut Stickers"}, None, None)
        self.assertEqual(square[0]["placeholders"][0]["images"][0]["scale"], 1, "a square cut is untouched")


class LettersAndColoursOnAGarment(unittest.TestCase):
    """2026-10-05, "Trailside Girls Hiking Tee": (1) the holes inside the
    letters printed as background-coloured blobs - knockout floods from the
    border and never reaches enclosed background; (2) the lettering was pale
    cream and vanished on White, Ivory and Butter, because nothing told the
    model which tee colours it was printing on."""

    BG, RING, NEAR = (245, 240, 225), (40, 50, 60), (225, 220, 205)   # NEAR: 20 off - inside 32, outside 16

    def letter_o(self, size=60):
        """A 60x60 cream square: a dark ring (an O) whose hole is background,
        with a dot at its centre in a shade close to the background - detail
        to keep, which only the tight match spares. `size` widens the field
        round the same O, so its hole can be a letter's share of a design."""
        w = h = size
        k = load("knockout_holes", "knockout.py")
        px = bytearray()
        for y in range(h):
            for x in range(w):
                d = (x - 30) ** 2 + (y - 26) ** 2
                c = self.RING if 100 <= d <= 400 else (self.NEAR if d <= 4 else self.BG)
                px += bytes(c) + b"\xff"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "o.png"
        k.encode(src, w, h, px)
        return k, src, Path(tmp.name)

    def cut(self, *flags, size=60):
        k, src, d = self.letter_o(size)
        out = d / "cut.png"
        # --keep-specks: the despeckle pass would take the small dot for a
        # speck; this measures the hole pass alone.
        r = subprocess.run([sys.executable, str(SCRIPTS / "knockout.py"), str(src), str(out), *flags,
                            "--keep-specks"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        w, h, px = k.decode(out)
        return w, px

    def alpha_at(self, w, px, x, y):
        return px[(y * w + x) * 4 + 3]

    def test_a_garment_cut_clears_the_hole_in_the_o(self):
        # trimmed output: find the hole as the centre of the ring's bounding box
        w, px = self.cut("--holes")
        opaque = [(i % w, i // w) for i in range(len(px) // 4) if px[i * 4 + 3]]
        xs, ys = [p[0] for p in opaque], [p[1] for p in opaque]
        cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
        self.assertEqual(self.alpha_at(w, px, cx, cy - 6), 0, "the hole is transparent")
        self.assertNotEqual(self.alpha_at(w, px, cx, cy), 0, "the near-background dot survives")

    def test_without_holes_the_hole_is_kept(self):
        w, px = self.cut()
        opaque = [(i % w, i // w) for i in range(len(px) // 4) if px[i * 4 + 3]]
        xs, ys = [p[0] for p in opaque], [p[1] for p in opaque]
        self.assertNotEqual(self.alpha_at(w, px, (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2 - 6), 0,
                            "with neither flag, the enclosed background is kept")

    def test_draft_asks_for_holes_on_garments_and_counters_elsewhere(self):
        body = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('(["--holes"] if is_garment(cat) else ["--counters"])', body)

    def test_a_sticker_cut_clears_the_inside_of_a_letter(self):
        # "Paws & Kissies" (2026-10-07) printed its blue background inside the
        # "&". Off a garment the small enclosed pieces go too - the near-
        # background dot still survives, as it does with --holes.
        w, px = self.cut("--counters", size=160)        # the hole: ~1.2% of the frame
        opaque = [(i % w, i // w) for i in range(len(px) // 4) if px[i * 4 + 3]]
        xs, ys = [p[0] for p in opaque], [p[1] for p in opaque]
        cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
        self.assertEqual(self.alpha_at(w, px, cx, cy - 6), 0, "the inside of the O is transparent")
        self.assertNotEqual(self.alpha_at(w, px, cx, cy), 0, "the near-background dot survives")

    def test_a_badge_keeps_a_big_enclosed_field(self):
        # A frame enclosing more than COUNTER_MAX_PCT of the image in the
        # background colour: that is the design, and only --holes clears it.
        k = load("knockout_badge", "knockout.py")
        w = h = 60
        px = bytearray()
        for y in range(h):
            for x in range(w):
                d = (x - 30) ** 2 + (y - 30) ** 2
                px += bytes(self.RING if 400 <= d <= 625 else self.BG) + b"\xff"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "badge.png"
        k.encode(src, w, h, px)
        _, _, px = k.decode(src)
        k.knockout(w, h, px)                                   # the outside goes first, as in main()
        inside = sum(1 for i in range(w * h) if (i % w - 30) ** 2 + (i // w - 30) ** 2 < 400)
        self.assertGreater(inside * 100.0 / (w * h), k.COUNTER_MAX_PCT, "the fixture must be past the limit")
        self.assertEqual(k.holes(w, h, bytearray(px), self.BG, max_pct=k.COUNTER_MAX_PCT), 0)
        self.assertEqual(k.holes(w, h, bytearray(px), self.BG), inside)

    def test_the_model_is_told_the_tee_colours(self):
        ea = load("emily_assets_ink", "emily-assets.py")
        ep = load("emily_printify_ink", "emily-printify.py")
        tee = {"variant_titles": ["S / White", "M / White", "S / Ivory", "S / Butter", "S / Moss"]}
        ea._catalogue_entry = lambda product: (ep, tee)
        said = ea.directed("a trail marker", "tshirt")
        self.assertIn("garments in these colours: White, Ivory, Butter, Moss", said)
        self.assertIn("No white, cream, beige or pale lettering", said)
        ea._catalogue_entry = lambda product: (ep, {"variant_titles": ["11oz", "15oz"]})
        self.assertNotIn("garments in these colours", ea.directed("x", "mug"))

    def test_the_raw_variants_dump_exists(self):
        src = (SCRIPTS / "emily-printify.py").read_text()
        self.assertIn('p.add_argument("--raw"', src)


class OnlyColoursTheDesignCanBeReadOn(unittest.TestCase):
    """2026-10-05: pale cream lettering ("WILD & FREE ON THE TRAIL") was
    offered on White, Ivory and Butter, where it vanished. Printify's product
    payload carries each colour's code (fixture: captured on the droplet), so
    the draft offers only colours where little of the design's ink is lost."""

    def setUp(self):
        self.ep = load("emily_printify_colours", "emily-printify.py")
        self.product = json.loads((ROOT / "tests/fixtures/printify-product-options-cc1717.json").read_text())
        self.hexes = self.ep.colour_hex_from(self.product)

    def ink(self, cream_share):
        """A hiking-tee-like design: pale cream lettering and a dark signpost."""
        cream, dark = self.ep.luminance("#EFE6CF"), self.ep.luminance("#2E2B2A")
        n = 200
        return [cream] * int(n * cream_share) + [dark] * (n - int(n * cream_share))

    def test_the_real_payload_is_read(self):
        self.assertEqual(self.hexes["White"], "#ffffff")
        self.assertEqual(self.hexes["Ivory"], "#FFF7E7")
        self.assertEqual(self.hexes["Black"], "#000000")

    def test_pale_lettering_drops_the_pale_shirts_and_dark_ink_the_dark_ones(self):
        titles = {1: "S / White", 2: "S / Ivory", 3: "S / Butter", 4: "S / Blue Jean", 5: "S / Black", 6: "M / White"}
        keep, dropped = self.ep.readable(list(titles), titles, self.hexes, self.ink(0.3))
        self.assertEqual(set(dropped), {"White", "Ivory", "Butter", "Black"})
        self.assertEqual(keep, [4], "Blue Jean reads both the cream and the dark")

    def test_an_unknown_colour_is_kept_and_all_bad_keeps_the_best(self):
        titles = {1: "S / White", 2: "S / Chalky Mint"}
        keep, _ = self.ep.readable(list(titles), titles, self.hexes, self.ink(0.5))
        self.assertIn(2, keep, "no code known is not unreadable")
        titles = {1: "S / White", 2: "S / Ivory"}
        keep, dropped = self.ep.readable(list(titles), titles, self.hexes, self.ink(1.0))
        self.assertEqual(len(keep), 1, "a draft needs a variant; the least-bad is kept")

    def test_draft_filters_and_learns(self):
        body = (SCRIPTS / "emily-printify.py").read_text().split("def cmd_draft(", 1)[1].split("\ndef cmd_", 1)[0]
        self.assertIn('readable(variant_ids, titles, cat["colour_hex"], ink)', body)
        self.assertIn('whole[cat_key]["colour_hex"] = hexes', body)
        self.assertIn("ink_sample(upload_from) if is_garment(cat) else None", body)


class LastRunReadsTheWrappedRun(unittest.TestCase):
    """Bug, Paul's first dry cycle 2026-10-05: openclaw 2026.9.2 prints the
    run wrapped - {"runId", "status", "summary", "result": {"payloads",
    "meta": {..., "agentMeta": {model, usage, costUsd, assistantTurns}}}} -
    and last-run.py read it flat: "model None", "tool calls None", the whole
    result dict on the model line, and no cost. Expected values are read from
    the fixture's own nesting, never restated (fixtures/README.md)."""

    FIXTURE = ROOT / "tests" / "fixtures" / "openclaw-agent-json-2026.9.log"

    def test_model_tools_turns_and_cost_come_through(self):
        spec = importlib.util.spec_from_file_location("last_run", ROOT / "scripts" / "last-run.py")
        lr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lr)
        raw = self.FIXTURE.read_text()
        lr.fetch = lambda unit: raw
        run = lr.json_blocks(raw)[-1]
        meta = run["result"]["meta"]
        agent = meta["agentMeta"]
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["last-run.py", "paul"]), contextlib.redirect_stdout(out):
            lr.main()
        said = out.getvalue()
        lines = {l.split()[0]: l for l in said.splitlines() if l.strip() and not l.startswith(" ")}
        self.assertIn(agent["model"], lines["model"])
        self.assertIn(f"tool calls   {meta['toolSummary']['calls']}", said)
        self.assertIn(f"failures={meta['toolSummary']['failures']}", said)
        self.assertIn(f"turns {agent['assistantTurns']}", said)
        self.assertIn(f"${agent['costUsd']:.4f}", lines["cost"])
        self.assertNotIn("payloads", lines["model"], "the whole result was printed as the model")



class TheMarketsPageDrawsBelfortsOwnNumbers(unittest.TestCase):
    """Owner, 2026-10-06: a Markets page for Belfort's house and the Deck.
    Guards two things. The candles go in bars.json, never quotes.json -
    Belfort reads quotes.json every wake, and cost scales with what he reads.
    And the page's numbers are his: the stop it shows is exit_check's stop,
    and its lines are the same SMA and MACD as the quote he trades on.
    Real TSM closes from Yahoo, through the real fetcher."""

    TSM = json.loads((ROOT / "tests" / "fixtures" / "belfort-yahoo-chart-tsm.json").read_text())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "agents" / "belfort" / "data"
        old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(lambda: os.environ.__setitem__("ECOSYSTEM_ROOT", old) if old
                        else os.environ.pop("ECOSYSTEM_ROOT", None))
        fetch = load("belfort_fetch_board", "belfort-fetch.py")
        fetch.UNIVERSE, fetch.BENCHMARKS, fetch.REQUEST_GAP = ["TSM", "NVDA"], ["QQQ"], 0
        fetch.http_get = lambda url, retries=2, ua=None: json.dumps(self.TSM).encode()
        fetch.fetch_news = lambda symbols, get=None, gap=0: [
            {"title": "TSMC monthly sales jump", "published": "Mon, 05 Oct 2026 12:00:00 +0000",
             "id": "abc12345", "publisher": "reuters.com", "symbol": "TSM"}]
        fetch.fetch_earnings = lambda symbols, today=None, get=None, gap=0: {}
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            fetch.main()
        self.fetch = fetch

    def tearDown(self):
        self.tmp.cleanup()

    def test_candles_stay_out_of_what_belfort_reads(self):
        quotes = json.loads((self.data / "quotes.json").read_text())
        for q in list(quotes["quotes"].values()) + list(quotes["benchmarks"].values()):
            self.assertNotIn("_chart", q)
            self.assertNotIn("bars", q)
        cands = json.loads((self.data / "candidates.json").read_text())["candidates"]
        self.assertFalse(any("_chart" in c for c in cands))
        bars = json.loads((self.data / "bars.json").read_text())["bars"]
        self.assertEqual(sorted(bars), ["NVDA", "QQQ", "TSM"])

    def test_the_lines_are_the_quotes_own_arithmetic(self):
        q = json.loads((self.data / "quotes.json").read_text())["quotes"]["TSM"]
        ch = json.loads((self.data / "bars.json").read_text())["bars"]["TSM"]
        self.assertEqual(len(ch["bars"]), self.fetch.CHART_BARS)
        self.assertEqual(ch["bars"][-1][4], q["price"])
        self.assertEqual(ch["bars"][-1][0], q["last_bar"])
        self.assertEqual(ch["sma20"][-1], q["sma20"])
        self.assertEqual(ch["sma50"][-1], q["sma50"])
        self.assertEqual(ch["macd_hist"][-1], q["macd_hist"])
        raw = self.TSM["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
        vols = [v for v in raw if v is not None]
        self.assertEqual(ch["volume"], vols[-1])
        self.assertEqual(ch["volume_avg20"], int(sum(vols[-21:-1]) / 20), "the 20 days BEFORE today")
        self.assertAlmostEqual(ch["volume_ratio"], vols[-1] / (sum(vols[-21:-1]) / 20), places=2)

    def test_the_board_shows_his_stop_and_his_candidates(self):
        q = json.loads((self.data / "quotes.json").read_text())["quotes"]["TSM"]
        state = self.root / "agents" / "belfort" / "state"
        state.mkdir(parents=True)
        entry = q["history"][-5][0] + " 13:36:00"
        cb = round(q["price"] / 1.03, 2)
        (state / "portfolio.json").write_text(json.dumps({
            "starting_cash": 10000.0, "cash": 9000.0, "created_utc": "2026-09-01 13:00:00",
            "positions": [{"symbol": "TSM", "shares": 2, "cost_basis": cb, "entry_utc": entry, "score": 8}],
            "trades": []}))
        reports = self.root / "agents" / "belfort" / "reports"
        reports.mkdir(parents=True)
        (reports / "2026-10-05-close.md").write_text("# Belfort\nheld TSM\n")
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), "board"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        b = json.loads(r.stdout)
        tsm = b["watch"][0]
        self.assertEqual((tsm["symbol"], tsm["verdict"], tsm["score"]), ("TSM", "HOLD", 8))
        trade = load("belfort_trade_board", "belfort-trade.py")
        stop = trade.exit_check({"symbol": "TSM", "shares": 2, "cost_basis": cb, "entry_utc": entry}, q)[2]
        self.assertEqual(tsm["stop"], stop)
        self.assertTrue(any(f"${stop:,.2f}" in text for side, text in tsm["flips"] if side == "SELL"))
        self.assertEqual(len(tsm["candles"]), self.fetch.CHART_BARS)
        self.assertEqual(tsm["news"][0]["title"], "TSMC monthly sales jump")
        self.assertEqual(b["report"]["name"], "2026-10-05-close.md")
        self.assertIn("Belfort (AI)", [r["name"] for r in b["showdown"]["rows"]])


class TheMarketsPageShowsHisMoney(TheMarketsPageDrawsBelfortsOwnNumbers):
    """Owner, 2026-10-07: "add the money value at the top". The dollars come
    from board(), off the same cash and market_value() as the book - the page
    prints them and adds nothing up itself."""

    def test_the_account_in_dollars(self):
        q = json.loads((self.data / "quotes.json").read_text())["quotes"]["TSM"]
        state = self.root / "agents" / "belfort" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "portfolio.json").write_text(json.dumps({
            "starting_cash": 10000.0, "cash": 9000.0, "created_utc": "2026-09-01 13:00:00",
            "positions": [{"symbol": "TSM", "shares": 3, "cost_basis": 100.0}], "trades": []}))
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), "board"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        a = json.loads(r.stdout)["account"]
        value = 9000.0 + 3 * q["price"]
        self.assertEqual((a["value"], a["cash"], a["invested"]), (round(value, 2), 9000.0, round(3 * q["price"], 2)))
        self.assertEqual(a["pnl"], round(value - 10000.0, 2))
        self.assertEqual(a["today"], round(3 * (q["price"] - q["prev_close"]), 2))
        self.assertNotEqual(a["today"], 0, "the fixture must move today, or this tests nothing")
        html = (ROOT / "mission-control-api" / "public" / "markets.html").read_text()
        self.assertIn("const a = B.account;", html)
        self.assertIn("${money(a.value)}", html)


class SizeKnowsTheEightNameCap(TheMarketsPageDrawsBelfortsOwnNumbers):
    """buy() refused a 9th name, but size() - "the most a buy may take now" -
    still offered shares. Found 2026-10-06 when the Markets page began
    quoting size() as the reason a candidate is not held."""

    def book(self, n):
        state = self.root / "agents" / "belfort" / "state"
        state.mkdir(parents=True, exist_ok=True)
        names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"][:n]
        p = {"starting_cash": 10000.0, "cash": 9000.0, "created_utc": "2026-09-01 13:00:00", "trades": [],
             "positions": [{"symbol": x, "shares": 1, "cost_basis": 10.0} for x in names]}
        (state / "portfolio.json").write_text(json.dumps(p))
        return p

    def board_row(self, sym):
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), "board"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        return next(w for w in json.loads(r.stdout)["watch"] if w["symbol"] == sym)

    def test_a_ninth_name_gets_no_shares(self):
        trade = load("belfort_trade_cap", "belfort-trade.py")
        self.assertEqual(trade.size(self.book(8), "NVDA", ("favorable", "test"))[0], 0)
        self.assertIn("8 names held, the most is 8", trade.size(self.book(8), "NVDA", ("favorable", "test"))[1])
        self.assertGreater(trade.size(self.book(7), "NVDA", ("favorable", "test"))[0], 0)

    def test_the_page_says_why_a_candidate_is_not_held(self):
        trade = load("belfort_trade_why", "belfort-trade.py")
        state = trade.regime()[0]
        self.book(8)
        row = self.board_row("NVDA")
        self.assertIn("His rules block a buy", row["why_not"])
        if state != "unfavorable":
            self.assertIn("8 names held", row["why_not"])
            self.book(0)
            self.assertIn("no headline under 7 days old", self.board_row("NVDA")["why_not"])


class BelfortWritesHisCallOnEachName(TheMarketsPageDrawsBelfortsOwnNumbers):
    """Owner, 2026-10-06, on the Markets page's "his report says why he
    passed": "I don't see it in the report ... I want it to say it there for
    each stock on why it passed." The report gave one sentence for all of
    them. Now each candidate he does not buy gets a score and a reason in
    state/calls.txt, filed by `calls`, owed by the one rule signoff.py and
    belfort-verify.py both ask, and drawn per name by the page."""

    def wake(self, held=(), cands=("TSM", "NVDA"), at_wake=None):
        self.state = self.root / "agents" / "belfort" / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "portfolio.json").write_text(json.dumps({
            "starting_cash": 10000.0, "cash": 9000.0, "created_utc": "2026-09-01 13:00:00", "trades": [],
            "positions": [{"symbol": x, "shares": 1, "cost_basis": 10.0} for x in held]}))
        (self.data / "candidates.json").write_text(json.dumps({"candidates": [{"symbol": c} for c in cands]}))
        self.started = int(time.time()) - 60
        (self.state / ".cycle-started").write_text(f"{self.started}\n")
        if at_wake is not None:
            (self.state / ".candidates-at-wake.json").write_text(
                json.dumps({"candidates": [{"symbol": c} for c in at_wake]}))
        return load(f"belfort_trade_calls_{id(self)}_{time.time_ns()}", "belfort-trade.py")

    def run_(self, *args, script="belfort-trade.py"):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True,
                              env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)), cwd=str(self.root))

    def write(self, text):
        (self.state / "calls.txt").write_text(text)

    def test_a_name_he_did_not_buy_is_owed_a_call_until_filed(self):
        trade = self.wake(held=["TSM"])
        self.assertEqual(trade.calls_missing(), ["NVDA"], "a held name is not owed one; a passed one is")
        self.write("NVDA | 4 | ANALYST: Jefferies reiterates Buy - no new fact, so it stops at 4\n")
        r = self.run_("calls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Every candidate has a call", r.stdout)
        self.assertEqual(trade.calls_missing(), [])
        filed = [json.loads(x) for x in (self.state / "calls.jsonl").read_text().splitlines()]
        self.assertEqual([(c["symbol"], c["score"], c["wake"]) for c in filed],
                         [("NVDA", 4, trade.wake_name(self.started))])

    def test_a_call_from_an_earlier_wake_is_not_this_wakes(self):
        trade = self.wake()
        self.write("NVDA | 4 | ANALYST: Jefferies reiterates Buy - no new fact at all\n")
        old = self.started - 3600
        os.utime(self.state / "calls.txt", (old, old))
        r = self.run_("calls")
        self.assertEqual(r.returncode, 1)
        self.assertIn("from an earlier wake", r.stderr)
        self.assertFalse((self.state / "calls.jsonl").exists())
        (self.state / "calls.jsonl").write_text(json.dumps(
            {"ts": old, "wake": "x", "symbol": "NVDA", "score": 4, "reason": "r"}) + "\n")
        self.assertIn("NVDA", trade.calls_missing(), "yesterday's call does not pay today's")

    def test_a_lazy_or_malformed_line_files_nothing(self):
        self.wake()
        for text, why in (("NVDA | 4 | no catalyst\nTSM | 3 | only price-move headlines this week, nothing to cite\n",
                           "is not a reason"),
                          ("NVDA | 11 | a score that does not exist on the ten point scale\n", "0 to 10"),
                          ("ZZZZ | 4 | a symbol that is not one of the thirty at all\n", "not one of the names"),
                          ("NVDA 4 reasons without the separators between them\n", "SYMBOL | score | reason")):
            self.write(text)
            r = self.run_("calls")
            self.assertEqual(r.returncode, 1, text)
            self.assertIn(why, r.stderr)
            self.assertFalse((self.state / "calls.jsonl").exists(), "a refused file records nothing")

    def test_nothing_owed_is_not_an_error(self):
        self.wake(held=["TSM", "NVDA"])
        r = self.run_("calls")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "No candidate is owed a call this wake - nothing to file."))
        self.write("NVDA | 4 | ANALYST: Jefferies reiterates Buy - no new fact, so it stops at 4\n")
        old = self.started - 3600
        os.utime(self.state / "calls.txt", (old, old))
        self.assertEqual(self.run_("calls").returncode, 0, "last wake's file, nothing owed: still not an error")

    def test_a_name_that_screened_in_after_wake_is_not_owed(self):
        trade = self.wake(cands=("TSM", "NVDA"), at_wake=("NVDA",))
        self.assertEqual(trade.calls_missing(), ["NVDA"])

    def test_signoff_and_the_verifier_ask_the_same_rule(self):
        self.wake(held=["TSM"])
        args = ("0", "-1", str(self.started))
        self.assertIn("CALLS: MISSING - 1 candidate(s) have no call this wake (NVDA)",
                      self.run_("belfort", script="signoff.py").stdout)
        self.assertIn("(NVDA) - each needs a line in state/calls.txt", self.run_(*args, script="belfort-verify.py").stdout)
        self.write("NVDA | 4 | ANALYST: Jefferies reiterates Buy - no new fact, so it stops at 4\n")
        self.assertEqual(self.run_("calls").returncode, 0)
        self.assertIn("CALLS: every candidate has a call", self.run_("belfort", script="signoff.py").stdout)
        self.assertNotIn("have no call", self.run_(*args, script="belfort-verify.py").stdout)

    def test_the_page_gets_each_names_call_beside_its_report(self):
        trade = self.wake()
        reports = self.root / "agents" / "belfort" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{trade.wake_name(self.started)}.md").write_text("# Belfort\npassed\n")
        from email.utils import format_datetime
        (self.data / "news.json").write_text(json.dumps({"headlines": [
            {"title": "Jefferies reiterates Buy on Nvidia", "published": format_datetime(datetime.now(timezone.utc)),
             "id": "n1", "publisher": "reuters.com", "symbol": "NVDA"}]}))
        self.write("TSM | 6 | EARNINGS: September sales up 30% - strong, but priced in after the run\n"
                   "NVDA | 4 | ANALYST: Jefferies reiterates Buy - no new fact, so it stops at 4\n")
        self.assertEqual(self.run_("calls").returncode, 0)
        b = json.loads(self.run_("board").stdout)
        nvda = next(w for w in b["watch"] if w["symbol"] == "NVDA")
        self.assertEqual((nvda["call"]["score"], nvda["call"]["wake"]), (4, trade.wake_name(self.started)))
        self.assertIn("Jefferies", nvda["call"]["reason"])
        if trade.regime()[0] != "unfavorable":
            self.assertIn("1 fresh headline(s)", nvda["why_not"])
        self.assertNotIn("his report says", nvda.get("why_not") or "", "the call says why now, not the report")
        wake = b["calls"][trade.wake_name(self.started)]
        self.assertEqual([c["symbol"] for c in wake], ["TSM", "NVDA"], "highest score first")
        html = (ROOT / "mission-control-api" / "public" / "markets.html").read_text()
        self.assertIn("(B.calls || {})[String(rep.name).replace(", html)
        self.assertIn("const c = w.call;", html)

    def test_the_wake_message_names_the_owed_and_freezes_them(self):
        sh = (SCRIPTS / "belfort-cycle.sh").read_text()
        self.assertIn('cp "$AGENT/data/candidates.json" "$AGENT/state/.candidates-at-wake.json"', sh)
        self.assertIn('belfort-trade.py" calls --owed', sh)
        self.assertLess(sh.index(".candidates-at-wake.json"), sh.index("openclaw agent"))
        self.assertIn("belfort-trade.py calls", (ROOT / "agents" / "belfort" / "_belfort-agents-header.md").read_text())


class BelfortWatchesMoreThanChips(unittest.TestCase):
    """Owner, 2026-10-06: "most of the ones he picks are semiconductor
    stocks" - the 30 names were all tech, a third of them chips. Four groups
    were added (with HIMS, by name), he is shown at most 10 candidates and 3
    from one group, and SPY joins QQQ as a yardstick."""

    def setUp(self):
        self.fetch = load("belfort_fetch_wide", "belfort-fetch.py")
        self.trade = load("belfort_trade_wide", "belfort-trade.py")

    def test_every_watched_name_is_in_exactly_one_cluster(self):
        # cluster_of() returns a stray name as its own group, which the 40%
        # cap then never sees: a name added without a cluster is uncapped.
        members = [m for ms in self.trade.CLUSTERS.values() for m in ms]
        self.assertEqual(len(members), len(set(members)), "a name in two clusters")
        self.assertEqual(set(self.fetch.UNIVERSE) - set(members), set())
        self.assertIn("HIMS", self.fetch.UNIVERSE)
        self.assertEqual(self.trade.cluster_of("HIMS"), "healthcare")
        for group in ("healthcare", "financials", "industrials and energy", "consumer and retail"):
            self.assertGreaterEqual(len(self.trade.CLUSTERS[group]), 4, group)

    @staticmethod
    def q(sym, price, hist):
        return {"symbol": sym, "price": price, "macd_hist": hist, "mechanical_score": 3}

    def test_at_most_ten_and_three_from_one_group_strongest_first(self):
        chips = [self.q(s, 100.0, 5.0 - i * 0.1) for i, s in enumerate(["NVDA", "AMD", "AVGO", "MU", "TSM", "ARM"])]
        others = [self.q(s, 100.0, 1.0 - i * 0.05) for i, s in enumerate(
            ["LLY", "JPM", "GE", "COST", "META", "PLTR", "COIN", "SHOP", "ISRG", "GS"])]
        failed = dict(self.q("XOM", 100.0, 9.0), mechanical_score=2)
        shown, left = self.fetch.shortlist(chips + others + [failed], self.trade.cluster_of)
        syms = [c["symbol"] for c in shown]
        self.assertEqual(len(shown), 10)
        self.assertEqual(syms[:3], ["NVDA", "AMD", "AVGO"], "the strongest three chips, in order")
        self.assertEqual(sum(self.trade.cluster_of(s) == "semiconductors" for s in syms), 3)
        self.assertNotIn("XOM", syms + [x["symbol"] for x in left], "a name that failed a screen is not a candidate")
        why = {x["symbol"]: x["why"] for x in left}
        self.assertEqual(why["MU"], "already 3 from semiconductors")
        self.assertEqual(why["GS"], "list full at 10")

    def test_ranked_by_push_for_its_price_not_by_dollars(self):
        # COST at $900 with a $3 histogram is 0.33%; HIMS at $50 with $1 is 2%.
        shown, _ = self.fetch.shortlist([self.q("COST", 900.0, 3.0), self.q("HIMS", 50.0, 1.0)], self.trade.cluster_of)
        self.assertEqual([c["symbol"] for c in shown], ["HIMS", "COST"])

    def test_spy_is_measured_from_its_own_closes(self):
        doc = {"quotes": {}, "benchmarks": {
            "QQQ": {"price": 110.0, "history": [["2026-10-01", 100.0], ["2026-10-02", 105.0]]},
            "SPY": {"price": 103.0, "history": [["2026-10-01", 100.0], ["2026-10-02", 101.0]]}}}
        p = {"starting_cash": 10000.0, "cash": 10000.0, "created_utc": "2026-10-02 13:35:00",
             "positions": [], "trades": []}
        b = self.trade.book_stats(p, "belfort", doc)
        self.assertEqual((b["qqq_return_pct"], b["spy_return_pct"], b["spy_from"]), (4.76, 1.98, "2026-10-02"))


class TheShortlistIsWhatHeReads(TheMarketsPageDrawsBelfortsOwnNumbers):
    """candidates.json carried each name's 90 daily closes, which he never
    needs to judge a headline; quotes.json keeps them for the exit rules."""

    def test_no_closes_in_candidates_and_the_count_is_kept(self):
        d = json.loads((self.data / "candidates.json").read_text())
        self.assertTrue(d["candidates"], "the fixture should screen in")
        self.assertFalse(any("history" in c for c in d["candidates"]))
        self.assertEqual((d["screened"], d["passed"]), (2, len(d["candidates"]) + len(d["left_out"])))
        q = json.loads((self.data / "quotes.json").read_text())["quotes"]
        self.assertTrue(all(len(v["history"]) == self.fetch.HISTORY_BARS for v in q.values()))

    def test_the_showdown_has_spy(self):
        state = self.root / "agents" / "belfort" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "portfolio.json").write_text(json.dumps({"starting_cash": 10000.0, "cash": 10000.0,
                                                           "created_utc": "2026-09-01 13:00:00", "positions": [], "trades": []}))
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), "board"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SPY", [row["name"] for row in json.loads(r.stdout)["showdown"]["rows"]])


class BelfortLearnsFromNumbersNotMemory(unittest.TestCase):
    """Owner, 2026-10-07: "Does it learn from its wins and losses?" It did
    not - each wake is a new conversation and MEMORY.md holds a week of his
    own notes. belfort-learn.py works his record out in code: closed trades by
    the catalyst and score he bought on, what the names he passed on did over
    the next 10 trading days against SPY, and a monthly review that suggests -
    past MIN_SAMPLE only - and changes nothing."""

    DAYS = [f"2026-09-{d:02d}" for d in range(1, 31)]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = self.root / "agents" / "belfort"
        for d in ("state", "data", "reports"):
            (self.agent / d).mkdir(parents=True)
        old = os.environ.get("ECOSYSTEM_ROOT")
        os.environ["ECOSYSTEM_ROOT"] = str(self.root)
        self.addCleanup(lambda: os.environ.__setitem__("ECOSYSTEM_ROOT", old) if old
                        else os.environ.pop("ECOSYSTEM_ROOT", None))
        self.book([])
        self.prices({"NVDA": [100.0 + i for i in range(30)], "JPM": [100.0 - i for i in range(30)]},
                    spy=[100.0 + i * 0.5 for i in range(30)])
        self.learn = load(f"belfort_learn_{time.time_ns()}", "belfort-learn.py")

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, trades):
        (self.agent / "state" / "portfolio.json").write_text(json.dumps({
            "starting_cash": 10000.0, "cash": 10000.0, "created_utc": "2026-09-01 13:00:00",
            "positions": [], "trades": trades}))

    def prices(self, closes, spy):
        hist = lambda cs: [[d, c] for d, c in zip(self.DAYS, cs)]
        (self.agent / "data" / "quotes.json").write_text(json.dumps({
            "quotes": {s: {"symbol": s, "price": cs[-1], "history": hist(cs)} for s, cs in closes.items()},
            "benchmarks": {"SPY": {"price": spy[-1], "history": hist(spy)}}}))

    def calls(self, rows):
        (self.agent / "state" / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    @staticmethod
    def trade(side, sym, shares, price, utc, **kw):
        t = {"side": side, "symbol": sym, "shares": shares, "price": price, "notional": shares * price, "utc": utc}
        return {**t, **kw}

    def test_a_closed_trade_carries_what_it_was_bought_on(self):
        self.book([self.trade("BUY", "MU", 2, 100.0, "2026-09-02 13:36:00", reason="EARNINGS: guidance raised",
                              score=8, regime="favorable"),
                   self.trade("BUY", "MU", 1, 110.0, "2026-09-03 13:36:00", reason="added", score=7),
                   self.trade("SELL", "MU", 1, 120.0, "2026-09-04 19:56:00", realised_pnl=16.67, reason="judgement"),
                   self.trade("SELL", "MU", 2, 90.0, "2026-09-05 19:56:00", realised_pnl=-26.67, reason="STOP LOSS"),
                   self.trade("BUY", "MU", 1, 95.0, "2026-09-08 13:36:00", reason="ANALYST: Citi to Buy", score=7),
                   self.trade("SELL", "MU", 1, 99.0, "2026-09-09 19:56:00", realised_pnl=4.0, reason="judgement")])
        closed = self.learn.T.closed_trades(self.learn.T.load())
        self.assertEqual([(c["catalyst_type"], c["score"]) for c in closed],
                         [("EARNINGS", 8), ("EARNINGS", 8), ("ANALYST", 7)],
                         "a position added to keeps its first buy; a new one after a full close starts over")
        self.assertEqual(closed[0]["cluster"], "semiconductors")
        self.assertEqual(self.learn.T.catalyst_type("listicle with no type"), "untagged")

    def test_a_pass_is_measured_ten_trading_days_on_against_spy(self):
        self.calls([{"symbol": "NVDA", "utc": "2026-09-05 13:40:00", "score": 6, "price": 104.0, "spy": 102.0,
                     "held": False, "wake": "2026-09-05-open"},
                    {"symbol": "NVDA", "utc": "2026-09-05 19:56:00", "score": 2, "held": False},
                    {"symbol": "JPM", "utc": "2026-09-05 13:40:00", "score": 4, "held": True}])
        out = self.learn.pass_outcomes()
        self.assertEqual(sorted(out), ["NVDA|2026-09-05"], "held is not a pass; one call a name a day")
        o = out["NVDA|2026-09-05"]
        # NVDA closes 104 on 09-05 (index 4) and 114 ten bars on; SPY 102 -> 107.
        self.assertEqual((o["score"], o["ret_10"], o["spy_10"], o["vs_spy_10"]), (6, 9.62, 4.9, 4.71))
        self.assertEqual(o["ret_5"], round((109 / 104 - 1) * 100, 2))
        self.prices({"NVDA": [1.0] * 30}, spy=[1.0] * 30)
        self.assertEqual(self.learn.pass_outcomes()["NVDA|2026-09-05"]["ret_10"], 9.62,
                         "a measured outcome is kept - quotes.json forgets after 90 closes")

    def test_too_recent_to_measure_is_left_open(self):
        self.calls([{"symbol": "NVDA", "utc": "2026-09-25 13:40:00", "score": 5, "price": 124.0, "held": False},
                    {"symbol": "NVDA", "utc": "2026-09-25 19:56:00", "score": 3, "price": 200.0, "held": False}])
        o = self.learn.pass_outcomes()["NVDA|2026-09-25"]
        self.assertEqual(o["ret_5"], round((129 / 124 - 1) * 100, 2), "the day's first call is the one measured")
        self.assertIsNone(o.get("vs_spy_10"))
        self.assertEqual(self.learn.record()["passes"]["measured"], 0)

    def test_the_wake_lines_quote_codes_numbers(self):
        self.book([self.trade("BUY", "MU", 2, 100.0, "2026-09-02 13:36:00", reason="EARNINGS: x", score=8),
                   self.trade("SELL", "MU", 2, 110.0, "2026-09-05 19:56:00", realised_pnl=20.0, reason="TAKE PROFIT")])
        self.calls([{"symbol": "NVDA", "utc": "2026-09-05 13:40:00", "score": 6, "held": False}])
        lines = "\n".join(self.learn.wake_lines(self.learn.record()))
        self.assertIn("closed trades: 1, 1 won (100%), average +10.0%", lines)
        self.assertIn("by catalyst: EARNINGS 1 (1 won, +10.0%)", lines)
        self.assertIn("scored 6: 1 (1 beat it, +4.7%)", lines)
        self.assertIn("does not change the rules", lines)

    def losers(self, n, typ="ANALYST"):
        out = []
        for i in range(n):
            out += [self.trade("BUY", f"S{i}", 1, 100.0, "2026-09-02 13:36:00", reason=f"{typ}: x", score=8),
                    self.trade("SELL", f"S{i}", 1, 95.0, "2026-09-05 19:56:00", realised_pnl=-5.0, reason="STOP LOSS")]
        return out

    def test_suggestions_only_past_the_minimum_sample(self):
        self.book(self.losers(self.learn.MIN_SAMPLE - 1))
        self.assertEqual(self.learn.suggestions(self.learn.record()), [])
        self.book(self.losers(self.learn.MIN_SAMPLE))
        tips = self.learn.suggestions(self.learn.record())
        self.assertEqual(len(tips), 1)
        self.assertIn("**ANALYST** buys have lost on average", tips[0])

    def test_passes_that_keep_winning_say_the_bar_may_be_strict(self):
        rows = [{"symbol": "NVDA", "utc": f"{d} 13:40:00", "score": 6, "held": False} for d in self.DAYS[:20]]
        self.DAYS = [f"2026-09-{d:02d}" for d in range(1, 31)] + [f"2026-10-{d:02d}" for d in range(1, 11)]
        self.prices({"NVDA": [100.0 * 1.01 ** i for i in range(40)]}, spy=[100.0] * 40)
        self.calls(rows[:self.learn.MIN_SAMPLE - 1])
        self.assertEqual(self.learn.suggestions(self.learn.record()), [])
        self.calls(rows)
        tips = self.learn.suggestions(self.learn.record())
        self.assertTrue(any("scored **6** and passed on went on to beat SPY" in t for t in tips), tips)

    def test_the_monthly_review_is_written_for_last_month(self):
        self.assertEqual(self.learn.last_month(datetime(2027, 1, 1, 8, 5)), "2026-12")
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-learn.py"), "review", "--month", "2026-09", "--write"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.agent / "reviews" / "2026-09.md").read_text()
        self.assertIn("# Belfort - review of 2026-09", text)
        self.assertIn("Nothing here changes a rule", text)
        self.assertIn(f"A suggestion needs at least {self.learn.MIN_SAMPLE} in a group", text)
        j = json.loads(subprocess.run([sys.executable, str(SCRIPTS / "belfort-learn.py"), "record", "--json"],
                                      capture_output=True, text=True,
                                      env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root))).stdout)
        self.assertEqual(j["reviews"][0]["name"], "2026-09.md")

    def test_a_buy_keeps_its_score_and_a_call_its_price(self):
        trade = self.learn.T
        p = trade.load()
        with contextlib.redirect_stdout(io.StringIO()):
            trade.buy(p, "NVDA", 1, 129.0, "EARNINGS: x", 8, None, ("favorable", "test"))
        self.assertEqual(p["trades"][-1]["score"], 8)
        (self.agent / "data" / "candidates.json").write_text(json.dumps({"candidates": [{"symbol": "NVDA"}]}))
        (self.agent / "state" / ".cycle-started").write_text(f"{int(time.time()) - 60}\n")
        (self.agent / "state" / "calls.txt").write_text("NVDA | 5 | a reason that is long enough to count\n")
        r = subprocess.run([sys.executable, str(SCRIPTS / "belfort-trade.py"), "calls"], capture_output=True,
                           text=True, env=dict(os.environ, ECOSYSTEM_ROOT=str(self.root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        c = json.loads((self.agent / "state" / "calls.jsonl").read_text().splitlines()[-1])
        self.assertEqual((c["price"], c["spy"]), (129.0, 114.5))

    def test_it_is_wired_in(self):
        sh = (SCRIPTS / "belfort-cycle.sh").read_text()
        self.assertIn('RECORD="$(python3 "$ROOT/scripts/belfort-learn.py" record', sh)
        self.assertLess(sh.index("RECORD="), sh.index("openclaw agent"))
        self.assertIn("\n$RECORD\"", sh)
        timer = (ROOT / "deploy" / "belfort-review.timer").read_text()
        self.assertIn("OnCalendar=*-*-01 08:05 America/New_York", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("belfort-learn.py review --write", (ROOT / "deploy" / "belfort-review.service").read_text())
        self.assertIn("app.get('/api/belfort/record'", (ROOT / "mission-control-api" / "belfort.js").read_text())
        pub = ROOT / "mission-control-api" / "public"
        self.assertIn("const SEEN = 'belfort-review-seen';", (pub / "markets.html").read_text())
        self.assertIn("localStorage.getItem('belfort-review-seen')", (pub / "dashboard.html").read_text())
        self.assertIn("## Your record", (ROOT / "agents" / "belfort" / "_belfort-agents-header.md").read_text())


class TheMarketsPageIsWiredInOnePlace(unittest.TestCase):
    """Owner, 2026-10-06: Belfort's house and the Deck's Markets tab are one
    page, and the report renderer they share is one file - it was two
    identical inline copies in dashboard.html and village.html."""

    API = ROOT / "mission-control-api"

    def test_one_page_two_doors(self):
        server = (self.API / "server.js").read_text()
        self.assertIn("app.get('/markets'", server)
        self.assertIn("app.use('/shared'", server)
        self.assertIn('href="/markets"', (self.API / "public" / "dashboard.html").read_text())
        village = (self.API / "public" / "village.html").read_text()
        self.assertIn("f.src = '/markets?embed=1'", village)
        self.assertIn("'markets-close'", (self.API / "public" / "markets.html").read_text())
        self.assertIn("app.get('/api/belfort/board'", (self.API / "belfort.js").read_text())

    def test_the_markdown_renderer_is_one_file(self):
        for page in ("dashboard.html", "village.html", "markets.html"):
            html = (self.API / "public" / page).read_text()
            self.assertIn('<script src="/shared/md.js"></script>', html, page)
            self.assertNotIn("function mdToHtml", html, f"{page} has its own copy again")
        self.assertIn("function mdToHtml", (self.API / "public" / "shared" / "md.js").read_text())


class BelfortsLatestReportIsTheLatestWake(unittest.TestCase):
    """The Markets page first showed "2026-10-05-open" as the latest report:
    by name it sorts after "-close", though the close is the later wake."""

    def test_close_after_open_and_days_in_order(self):
        trade = load("belfort_trade_order", "belfort-trade.py")
        names = ["2026-10-05-close.md", "2026-10-04-close.md", "2026-10-05-open.md", "2026-10-06-open.md"]
        got = [f.name for f in sorted((Path(n) for n in names), key=trade.report_order, reverse=True)]
        self.assertEqual(got, ["2026-10-06-open.md", "2026-10-05-close.md", "2026-10-05-open.md",
                               "2026-10-04-close.md"])


class ReportsHideOpenclawReplyTags(unittest.TestCase):
    """Belfort's 2026-10-06 open report began "[[reply_to_current]]" - an
    openclaw routing tag, printed on the Markets page as if he wrote it."""

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_tag_line_is_dropped_and_the_rest_kept(self):
        md = ROOT / "mission-control-api" / "public" / "shared" / "md.js"
        src = "[[reply_to_current]]\n\nMarket open notes.\n[[reply_to:abc123]]\nKeep [[this]] inline."
        out = subprocess.run(["node", "-e", f"process.stdout.write(require({json.dumps(str(md))}).mdToHtml({json.dumps(src)}))"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("reply_to", out.stdout)
        self.assertIn("Market open notes.", out.stdout)
        self.assertIn("Keep [[this]] inline.", out.stdout)

