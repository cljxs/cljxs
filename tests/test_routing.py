"""ROUTING.md - the table a session reads instead of the whole repo.

    python3 -m unittest tests.test_routing -b

Owner, 2026-10-20: "Can you create a routing table for yourself so you don't
have to read the entire repo every time?" A routing table that points at a
renamed file, or leaves a new one out, sends the next session to the wrong
place - worse than none, because it is trusted. So every path in it must
exist, every script, Deck module, page, unit and test file must be in it, and
every anchor it names must still be in the file it names.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLE = (ROOT / "ROUTING.md").read_text()
TICKS = re.findall(r"`([^`]+)`", TABLE)


def section(title):
    return TABLE.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


class EveryPathExists(unittest.TestCase):

    def test_paths(self):
        paths = [t.rstrip("/") for t in TICKS
                 if re.fullmatch(r"\w[\w.-]*(/[\w.-]+)*/?", t) and ("/" in t or re.search(r"\.(py|md|js|html|sh|json)$", t))]
        self.assertGreater(len(paths), 100, "the pattern found the table's paths")
        missing = [p for p in paths if not (ROOT / p).exists()]
        self.assertEqual(missing, [], "ROUTING.md points at files that are not there")


class NothingIsLeftOut(unittest.TestCase):

    def listed(self, files):
        return sorted(f for f in files if f not in TABLE)

    def test_scripts(self):
        files = [f"scripts/{p.name}" for p in (ROOT / "scripts").iterdir() if p.suffix in (".py", ".sh")]
        self.assertEqual(self.listed(files), [], "a script with no row - add it to ROUTING.md")

    def test_deck_modules_and_pages(self):
        api = ROOT / "mission-control-api"
        files = [f"mission-control-api/{p.name}" for p in api.glob("*.js")]
        files += [f"mission-control-api/public/{p.name}" for p in (api / "public").glob("*.html")
                  if p.name not in ("dashboard.html", "village.html")]
        self.assertEqual(self.listed(files), [])

    def test_clipper_modules(self):
        files = [f"clipper/{p.name}" for p in (ROOT / "clipper").glob("*.py")
                 if p.name not in ("__init__.py", "__main__.py")]
        self.assertEqual(self.listed(files), [])

    def test_tests(self):
        files = [f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py")]
        self.assertEqual(self.listed(files), [])

    def test_units(self):
        units = {p.stem for p in (ROOT / "deploy").iterdir() if p.suffix in (".service", ".timer")}
        missing = sorted(u for u in units if f"`{u}`" not in TABLE and u not in TABLE)
        self.assertEqual(missing, [], "a systemd unit with no row")


class EveryAnchorIsStillThere(unittest.TestCase):

    def anchors(self, text):
        return [t for t in re.findall(r"`([A-Za-z_][\w]*)`", text)]

    def test_paul(self):
        src = (ROOT / "scripts" / "paul.py").read_text()
        names = self.anchors(section("paul.py anchors"))
        self.assertGreater(len(names), 10)
        self.assertEqual([n for n in names if not re.search(rf"\b{n}\b", src)], [])

    def test_village(self):
        src = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        block = section("The Command Deck").split("Village anchors", 1)[1]
        names = [n for n in re.findall(r"`([A-Za-z_][\w ]*)`", block.split("Shared front-end", 1)[0])]
        names = [n.split()[-1] for n in names]
        self.assertGreater(len(names), 10)
        self.assertEqual([n for n in names if not re.search(rf"\b{n}\b", src)], [])

    def test_clipper(self):
        block = section("Clipper")
        for row in block.splitlines():
            mods = re.findall(r"`(clipper/\w+\.py)`", row)
            for name in re.findall(r"`([a-z_]+)`", row.split("|")[1] if row.count("|") > 2 else ""):
                if name in ("cmd_",):
                    continue
                src = "".join((ROOT / m).read_text() for m in mods)
                self.assertRegex(src, rf"\b{name}\b", f"{name} is not in {mods}")

    def test_the_session_reads_it(self):
        self.assertIn("@ROUTING.md", (ROOT / "CLAUDE.md").read_text(),
                      "imported into CLAUDE.md, so it is loaded without being asked for")


if __name__ == "__main__":
    unittest.main()
