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
        # No "cutout" key - it was picked yesterday. Inferred from the sizes.
        self.assertTrue(self.pf.needs_cutout(
            {"variant_titles": ["Black / S", "Navy / 2XL", "Maroon / 5XL"]}))

    def test_stickers_are_left_alone(self):
        # Die-cut already; an opaque square is correct for them.
        self.assertFalse(self.pf.needs_cutout(
            {"variant_titles": ['2" x 2"', '4" x 4"', '5.5" x 5.5"']}))

    def test_an_explicit_flag_beats_the_inference(self):
        self.assertFalse(self.pf.needs_cutout({"cutout": False,
                                               "variant_titles": ["Black / S"]}))
        self.assertTrue(self.pf.needs_cutout({"cutout": True,
                                              "variant_titles": ['2" x 2"']}))

    def test_an_empty_entry_does_not_crash(self):
        for entry in ({}, None, {"variant_titles": []}):
            with self.subTest(entry=entry):
                self.assertFalse(self.pf.needs_cutout(entry))

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
        self.proposals.write_text(json.dumps([{"title": "Trail map hoodie",
                                               "product": "hoodie"}]))
        self.merge()
        titles = [i["title"] for i in self.log()]
        self.assertEqual(titles, ["Old one", "Trail map hoodie"])

    def test_ids_are_assigned_here_not_by_the_agent(self):
        self.ideas.write_text(json.dumps({"ideas": [{"id": 7, "title": "Old"}]}))
        self.proposals.write_text(json.dumps([{"title": "A"}, {"title": "B"}]))
        self.merge()
        self.assertEqual([i["id"] for i in self.log()], [7, 8, 9])
        self.assertTrue(all(i.get("status") == "pending" for i in self.log()[1:]))

    def test_a_repeated_title_is_not_filed_twice(self):
        self.ideas.write_text(json.dumps({"ideas": [{"id": 1, "title": "Trail map hoodie"}]}))
        self.proposals.write_text(json.dumps([{"title": "  trail MAP hoodie "}]))
        self.merge()
        self.assertEqual(len(self.log()), 1)

    def test_rerunning_the_merge_adds_nothing(self):
        self.ideas.write_text(json.dumps({"ideas": []}))
        self.proposals.write_text(json.dumps([{"title": "One"}]))
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
        self.assertIn("Never write `state/ideas.json` yourself", header)

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

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, *args):
        env = dict(os.environ, ECOSYSTEM_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPTS / "scout-ideas.py"), *args],
                              capture_output=True, text=True, env=env)

    def test_an_inch_mark_survives_propose(self):
        r = self.run_it("propose", "--title", "Pocket Folklore Deer",
                        "--product", "hoodie",
                        "--brief", 'Bold flat shapes at 3.5" wide.')
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(self.proposals.read_text())["proposals"]
        self.assertIn('3.5"', rows[0]["brief"], "the words are the agent's")
        self.assertEqual(self.run_it("merge").returncode, 0)

    def test_proposals_accumulate_across_calls(self):
        for t in ("One", "Two", "Three"):
            self.run_it("propose", "--title", t, "--product", "hoodie")
        self.assertEqual(len(json.loads(self.proposals.read_text())["proposals"]), 3)

    def test_propose_will_not_clobber_a_file_it_cannot_read(self):
        self.proposals.write_text("{ not json")
        r = self.run_it("propose", "--title", "New", "--product", "hoodie")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.proposals.read_text(), "{ not json")

    def test_the_inch_repair_recovers_a_hand_written_file(self):
        # What actually happened, kept because a model writing measurements
        # into JSON will do it again even with propose available.
        self.proposals.write_text(
            '{"proposals":[{"title":"Minimalist Mountain Badge",'
            '"product":"hoodie","brief":"sized for a 3.5" chest print."}]}')
        r = self.run_it("merge")
        self.assertEqual(r.returncode, 0)
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
        self.assertIn("scout-ideas.py propose", header)
        self.assertIn("Do not write any JSON by hand", header)


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
        src = (SCRIPTS / "knockout.py").read_text()
        body = src.split("def main(", 1)[1]
        self.assertLess(body.index("MAX_REMOVED_PCT"), body.index("despeckle(w, h, px)"))
        self.assertLess(body.index("despeckle(w, h, px)"), body.index("encode(dst"))


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

    def test_the_simulation_counts_what_the_player_has_actually_done(self):
        # Five games, two of them over 60. A bootstrap claims no shape beyond
        # the one he produced.
        probs = self.m.simulate([10.0, 20.0, 65.0, 80.0, 30.0],
                                (60,), draws=10_000, seed="x")
        self.assertAlmostEqual(probs[60], 40.0, delta=2.0)

    def test_the_same_seed_gives_the_same_forecast(self):
        # A number nobody can reproduce cannot be audited after the game.
        sample = [12.0, 45.0, 77.0, 31.0, 66.0, 9.0, 52.0, 88.0]
        bars = self.m.MARKETS["receiving_yards"]["thresholds"]
        a = self.m.simulate(sample, bars, seed="game-1-player-2")
        b = self.m.simulate(sample, bars, seed="game-1-player-2")
        c = self.m.simulate(sample, bars, seed="game-1-player-9")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_an_empty_sample_forecasts_nothing(self):
        self.assertEqual(self.m.simulate([], (40, 50)), {})

    def test_a_thin_sample_is_refused_by_the_minimum(self):
        # Week 3 gives two games. A confident number off two games is the
        # failure being copied, not the feature.
        self.assertGreaterEqual(self.m.MIN_GAMES, 8)

    def test_probabilities_fall_as_the_bar_rises(self):
        sample = [5.0, 18.0, 33.0, 44.0, 55.0, 61.0, 72.0, 90.0, 101.0]
        probs = self.m.simulate(sample,
                                self.m.MARKETS["receiving_yards"]["thresholds"],
                                seed="s")
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
        flat = [20.0] * 12          # no game between any two bars
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
        card = self.html.split("function forecastCard(", 1)[1].split("\nasync function", 1)[0]
        feed = self.js.split("/api/ace/forecasts", 1)[1].split("app.get(", 1)[0]
        for word in ("price", "odds", "novig", "moneyline", "stake", "edge"):
            self.assertNotIn(word, card.lower(), f"a forecast card must not show {word}")
            self.assertNotIn(word, feed.lower().replace("no odds", ""),
                             f"the forecast feed must not carry {word}")

    def test_the_card_says_it_is_not_a_bet(self):
        card = self.html.split("function forecastCard(", 1)[1].split("\nasync function", 1)[0]
        self.assertIn("NOT A BET", card)

    def test_a_coarse_forecast_is_marked_in_the_interface_too(self):
        # It is flagged in the script's output; a card that dropped the flag
        # would present the weakest rows as if they were the strongest.
        card = self.html.split("function forecastCard(", 1)[1].split("\nasync function", 1)[0]
        self.assertIn("f.coarse", card)
        self.assertIn("COARSE", card)

    def test_the_sample_is_shown_beside_the_number(self):
        # A probability off 11 games is not the same claim as one off 21, and
        # printing them identically is the failure being designed against.
        card = self.html.split("function forecastCard(", 1)[1].split("\nasync function", 1)[0]
        for field in ("sample_games", "sample_mean", "p25", "p75"):
            self.assertIn(field, card, f"the card must show {field}")

    def test_an_ungraded_forecast_is_not_drawn_as_a_result(self):
        # Showing a pending forecast as a miss would make every new slate look
        # like a failure before a ball was thrown.
        card = self.html.split("function forecastCard(", 1)[1].split("\nasync function", 1)[0]
        self.assertIn("awaiting the final score", card)
        self.assertIn("f.actual !== null", card)

    def test_the_panel_is_loaded_for_ace(self):
        # Assert the CALL, not the name: `async function loadAceForecasts(){`
        # contains "loadAceForecasts()" as a substring, so the first version
        # of this passed on the definition of a function nothing invoked.
        self.assertIn('id="aceForecasts"', self.html)
        self.assertRegex(self.html,
                         r"if \(a\.name === 'ace'\) \{[^}]*loadAceForecasts\(\);")

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
    # A box score receiving category, captured from the same game.
    BOX_KEYS = ['receptions', 'receivingYards', 'yardsPerReception',
                'receivingTouchdowns', 'longReception', 'receivingTargets']

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

    def test_a_stat_that_is_not_in_the_payload_is_missing_not_zero(self):
        # A quarterback's log has no receiving column at all. Reading that as
        # index 0 would forecast his completions as receptions.
        self.assertIsNone(self.m.column_of(
            ['passingYards', 'passingTouchdowns'], "receivingYards"))

    def test_every_market_names_a_stat_espn_actually_publishes(self):
        # A typo in MARKETS costs nothing at import and everything at 1pm on
        # Sunday: the column simply is not found and the market silently
        # disappears from the file. Checked against captured names.
        known = set(self.TE_NAMES) | set(self.RB_NAMES) | set(self.BOX_KEYS)
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

    def tearDown(self):
        self.m.LOG_CACHE.clear()

    def cache(self, aid, season, targets, carries):
        self.m.LOG_CACHE[(str(aid), season)] = {
            "receiving_targets": [("s", float(t)) for t in targets],
            "rushing_attempts": [("s", float(c)) for c in carries],
        }

    def cand(self, aid, name):
        return (str(aid), name, "WR", "LAR", "14")

    def test_the_alphabet_does_not_decide_who_is_forecast(self):
        self.m.PER_TEAM = 2
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
        self.assertEqual(self.m.usage_of(
            {"receiving_targets": [], "rushing_attempts": [("s", 20.0)]}), 20.0)
        self.assertEqual(self.m.usage_of(
            {"receiving_targets": [("s", 9.0)], "rushing_attempts": []}), 9.0)

    def test_week_one_falls_back_to_last_season_not_to_the_alphabet(self):
        # Nobody has played. Without the fallback every score is zero and the
        # sort returns the roster order it was built to replace.
        now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        self.cache(1, None, [], []); self.cache(1, 2025, [30], [0])
        self.cache(2, None, [], []); self.cache(2, 2025, [180], [0])
        self.m.PER_TEAM = 1
        picked = self.m.top_by_usage(
            [self.cand(1, "A Adams"), self.cand(2, "Z Nacua")], now=now)
        self.assertEqual([p[1] for p in picked], ["Z Nacua"])

    def test_the_cap_is_per_side_not_per_slate(self):
        # Six from each team, or one lopsided offence takes every slot and the
        # other side of the game is not forecast at all.
        self.m.PER_TEAM = 1
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

    def row(self, player, market, probs, coarse=False, games=20):
        return {"athlete_id": player, "player": player, "market": market,
                "unit": "rec", "probabilities": probs, "coarse": coarse,
                "sample_games": games, "sample_mean": 4.0, "p25": 2.0,
                "p75": 6.0, "availability": None}

    def test_a_confident_under_beats_a_middling_over(self):
        picks = self.m.strongest([
            self.row("A", "receptions", {"3": 61.0, "6": 8.0})])
        self.assertEqual(picks[0]["side"], "UNDER")
        self.assertEqual(picks[0]["threshold"], 6.0)
        self.assertEqual(picks[0]["confidence"], 92.0)

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
