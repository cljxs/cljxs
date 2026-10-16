"""The command-centre frame round the village (owner, 2026-10-16: "Do not make
the design in the middle of the screen just the stuff around the edges. I
want my village to stay the same for now").

    python3 -m unittest tests.test_village_frame -b

The village is not redrawn: it keeps its own box, and the frame takes the
four edges. These check that contract, and that every number in the frame
comes from what the Deck already serves.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "mission-control-api" / "public" / "village.html").read_text()


class TheFrameLeavesTheVillageAlone(unittest.TestCase):

    def test_the_village_sits_in_its_own_box(self):
        self.assertIn("#game{position:fixed;top:var(--ft);left:var(--fl);right:var(--fr);bottom:var(--fb)}", PAGE)
        self.assertIn("body.noframe{--ft:0px;--fl:0px;--fr:0px;--fb:0px}", PAGE, "hidden, it is the whole screen")
        # Zoom from the box, not the window - or the village is cropped inside the frame.
        self.assertIn("(g.clientWidth || window.innerWidth) / 800", PAGE)
        # The touch stick, the action button and the prompt move in with it.
        for el in ("#stick", "#act", "#prompt"):
            rule = re.search(re.escape(el) + r"\{[^}]*\}", PAGE).group(0)
            self.assertIn("var(--f", rule, el)

    def test_the_frame_reads_the_decks_own_answers(self):
        self.assertIn("fetch('/api/attention'", PAGE)
        self.assertIn("renderFrame();", PAGE.split("async function pull(first)", 1)[1].split("\nfunction boot", 1)[0],
                      "redrawn from each dashboard pull")
        # Navigation opens what the houses open.
        self.assertIn("if (FRAMES[key]) return openFrame(key);", PAGE)
        for key in ("belfort", "ace", "peter", "bolt", "paul", "shop", "agentroom", "stan"):
            self.assertIn(f"['{key}',", PAGE.split("const NAV = [", 1)[1].split("];", 1)[0])

    def test_quick_actions_are_the_existing_buttons_and_ask_first(self):
        qa = PAGE.split("const QA = {", 1)[1].split("};", 1)[0]
        for url in ("/api/shop/tiktok/make", "/api/scout/scan", "/api/paul/find"):
            self.assertIn(url, qa)
        self.assertIn("if (!confirm(ask)) return;", PAGE)

    def test_it_can_be_hidden_and_folds_away_on_a_small_screen(self):
        self.assertIn("localStorage.setItem('village-frame'", PAGE)
        self.assertIn("window.dispatchEvent(new Event('resize'));", PAGE, "the village refits when the frame goes")
        self.assertIn("@media (max-width: 1000px)", PAGE)
        self.assertIn("z-index:10", PAGE.split(".fx{", 1)[1].split("}", 1)[0],
                      "under the houses' panels (20) and pages (60)")


if __name__ == "__main__":
    unittest.main()
