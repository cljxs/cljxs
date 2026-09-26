#!/usr/bin/env python3
"""
ip-check.py — is this phrase somebody else's property?

    ip-check.py "fall sticker pikmin"
    ip-check.py "cozy fall sweatshirt"

WHY THIS EXISTS. trend-probe.py expanded 'fall sticker' into 229 real
searches and put 198 of them in a bucket labelled BUYING. Among them:

    fall sticker pikmin
    fall sticker decor pikmin

Pikmin is Nintendo's. Those are real searches by real people, and they are
also the fastest way to get an Etsy shop suspended. The chain that matters
here is automatic: Scout proposes, Emily generates art and drafts a listing,
and nothing in between was looking at trademarks. A name list is cheap and
that chain is not.

WHAT THIS IS NOT. It is not legal advice and it is not complete. No list of
names can be. It is a floor - it stops the obvious ones reaching an agent
that would cheerfully print them - and a phrase it passes has not been
cleared, only not-recognised.

TWO TIERS, because a one-tier list is wrong in whichever direction you pick.
'pikmin' has no ordinary meaning and can be refused outright. 'frozen',
'friends' and 'stanley' are ordinary English words that are also enormous
properties, so they are flagged for a human rather than refused - the same
lesson as 'target practice sticker', which an earlier list deleted as
someone shopping at Target.

Standard library only.
"""

import re
import sys

# No ordinary meaning in a product phrase. Refused.
BLOCKED = [
    ("nintendo", r"pikmin|nintendo|mario|luigi|zelda|pok[eé]mon|pikachu|"
                 r"animal crossing|splatoon|kirby|donkey kong"),
    # bloxburg and berry avenue are Roblox games, and both turned up in a
    # real expansion of 'fall sticker' without the word Roblox anywhere near
    # them. The franchise name is not what people type.
    ("game studios", r"minecraft|fortnite|roblox|bloxburg|berry avenue|"
                     r"stardew valley|overwatch|skyrim|hollow knight|"
                     r"undertale|sonic the hedgehog|fallout|elden ring"),
    ("disney", r"disney|mickey mouse|minnie mouse|pixar|encanto|moana|"
               r"lilo and stitch|epcot|magic kingdom"),
    ("star wars / marvel", r"star wars|mandalorian|baby yoda|grogu|jedi|"
                           r"marvel|spider-?man|avengers|deadpool|iron man"),
    ("wizarding world", r"harry potter|hogwarts|gryffindor|slytherin|"
                        r"ravenclaw|hufflepuff|dumbledore"),
    ("tv & film", r"bluey|paw patrol|peppa pig|cocomelon|stranger things|"
                  r"ted lasso|squid game|wednesday addams|barbie movie"),
    ("sanrio & ghibli", r"sanrio|hello kitty|kuromi|cinnamoroll|my melody|"
                        r"studio ghibli|totoro|ponyo|kiki'?s delivery"),
    ("seuss & peanuts", r"dr\.? seuss|grinch|lorax|cat in the hat|"
                        r"snoopy|charlie brown|peanuts gang|woodstock"),
    ("music", r"taylor swift|swiftie|eras tour|beyonc[eé]|beyhive|"
              r"olivia rodrigo|bad bunny|billie eilish"),
    ("brands", r"starbucks|nike|adidas|lululemon|coca[- ]cola|john deere|"
               r"in-?n-?out|chick-?fil-?a|jeep|harley davidson"),
    # Bag and fashion houses. A tote market expands straight into them -
    # "tote bag coach" was the fourth phrase of the first real 'tote bag'
    # scan - and a tote named after one is the plainest trademark problem
    # there is. None of these is an ordinary word; Coach is, so it is in
    # CHECK below instead.
    ("bag & fashion", r"marc jacobs|michael kors|kate spade|longchamp|"
                      r"louis vuitton|\bgucci\b|\bprada\b|\bchanel\b|"
                      r"tory burch|\btelfar\b|\bbaggu\b|\bdior\b|"
                      r"\bhermes\b|herm[eè]s birkin"),
    ("pro sports", r"\bnfl\b|\bnba\b|\bmlb\b|\bnhl\b|super bowl|"
                   r"march madness|world series"),
]

# Ordinary words that are also properties. Flagged, never refused - refusing
# these would delete 'frozen hot chocolate sticker' and 'friends are like
# leaves' along with the infringing ones.
CHECK = [
    ("frozen", r"\bfrozen\b", "the film, or just cold?"),
    ("friends", r"\bfriends\b", "the sitcom, or just friends?"),
    ("the office", r"\bthe office\b", "the sitcom, or a workplace?"),
    ("stanley", r"\bstanley\b", "the tumbler, or the name?"),
    ("monopoly", r"\bmonopoly\b", "the board game, or the word?"),
    ("bratz / barbie", r"\bbratz\b|\bbarbie\b", "the doll line?"),
    ("dune", r"\bdune\b", "the film, or a sand dune?"),
    ("up", r"\bup house\b", "the Pixar film?"),
    ("coach", r"\bcoach\b", "the handbag brand, or a sports coach?"),
]

# Phrasings that are a trademark problem whatever the name attached to them.
# 'inspired by' is the one Etsy sellers reach for believing it is a defence.
# It is not.
PHRASING = [
    (r"\binspired by\b", "'inspired by' is not a defence - it names the "
                         "property it is copying"),
    (r"\bdupe\b", "'dupe' advertises that it imitates a branded product"),
    (r"\b(official|licen[cs]ed|authentic)\b",
     "claiming to be official or licensed when it is not"),
    (r"[™®]", "carries a ™ or ® symbol"),
]


def risky(phrase):
    """(tier, what, why) - tier is 'blocked', 'check' or None."""
    p = (phrase or "").lower()
    for pattern, why in PHRASING:
        if re.search(pattern, p, re.I):
            return "blocked", "trademark phrasing", why
    for what, pattern in BLOCKED:
        m = re.search(pattern, p, re.I)
        if m:
            return "blocked", what, f"'{m.group(0)}' belongs to someone else"
    for what, pattern, why in CHECK:
        m = re.search(pattern, p, re.I)
        if m:
            return "check", what, f"'{m.group(0)}' - {why}"
    return None, None, None


def main():
    if len(sys.argv) < 2:
        print('usage: ip-check.py "<phrase>" [...]', file=sys.stderr)
        return 2
    worst = 0
    for phrase in sys.argv[1:]:
        tier, what, why = risky(phrase)
        if tier == "blocked":
            print(f"  BLOCKED  {phrase}\n           {what}: {why}")
            worst = max(worst, 2)
        elif tier == "check":
            print(f"  CHECK    {phrase}\n           {what}: {why}")
            worst = max(worst, 1)
        else:
            print(f"  ok       {phrase}")
    if worst:
        print("\n  Not legal advice and not a complete list. A phrase that "
              "passes here\n  has not been cleared - only not recognised.")
    return worst


if __name__ == "__main__":
    sys.exit(main())
