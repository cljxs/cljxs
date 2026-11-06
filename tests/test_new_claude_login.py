"""new-claude-login.py: a Claude login made and saved with nothing copied.

The bug it replaced (2026-10-16): the 108-character login from `claude
setup-token`, copied on an iPad and pasted into set-credential.py, saved as 92
characters time after time - and the screenshot sent to ask why showed the
whole login. The login here is a stand-in, built at run time so no file holds
anything login-shaped.
"""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FAKE = "sk-" + "ant-oat01-" + "Zq9_wX-4" * 11 + "Abcd0AA"          # 108 characters, like a real one


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


NL = load("new_claude_login_test", "new-claude-login.py")
# What setup-token's screen looks like around the login: colour codes, then
# the login on a line of its own (claude 2.1.300, seen on the droplet).
SCREEN = ("\x1b[32m Your OAuth token (valid for 1 year):\x1b[39m\r\n\r\n "
          f"\x1b[33m{FAKE}\x1b[39m\r\n\r\n Store this token securely.\r\n").encode()


class TheScreen(unittest.TestCase):
    def test_the_login_never_reaches_the_screen_however_it_is_split(self):
        # Guards: the first half of a login shown because it arrived in one
        # read and the rest in the next.
        self.assertEqual(len(FAKE), 108)
        for cut in range(1, len(SCREEN)):
            r = NL.Redactor()
            shown = r.feed(SCREEN[:cut]) + r.feed(SCREEN[cut:]) + r.flush()
            self.assertNotIn(FAKE[13:30].encode(), shown, f"split at {cut}")
            self.assertNotIn(FAKE[-20:].encode(), shown, f"split at {cut}")
            self.assertIn(NL.MASK, shown)
            self.assertIn(b"Store this token securely.", shown)

    def test_every_other_word_gets_through(self):
        r = NL.Redactor()
        text = b"Paste code here if prompted > "
        self.assertEqual(r.feed(text) + r.flush(), text)

    def test_the_login_is_caught_from_the_raw_screen(self):
        self.assertEqual(NL.caught(b"noise\x1b[2K" + SCREEN), FAKE)
        self.assertIsNone(NL.caught(b"Login cancelled.\r\n"))

    def test_a_real_pseudo_terminal(self):
        # The stand-in prints setup-token's screen; nothing is typed.
        child = f"import sys,os;os.write(1,{SCREEN!r})"
        out_r, out_w = os.pipe()
        with open(os.devnull) as devnull:
            raw = NL.run_on_pty([sys.executable, "-c", child], dict(os.environ),
                                stdin_fd=devnull.fileno(), stdout_fd=out_w)
        os.close(out_w)
        shown = os.read(out_r, 65536)
        os.close(out_r)
        self.assertEqual(NL.caught(raw), FAKE)
        self.assertNotIn(FAKE[13:30].encode(), shown)
        self.assertIn(NL.MASK, shown)


class Saving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = unittest.mock.patch.dict(os.environ, {"AGENTROOM_HOME": self.tmp.name})
        self.env.start()
        self.sc = load("set_credential_for_test", "set-credential.py")
        self.wake = load("wake_for_test", "wake.py")
        self.wake.claude_bin = lambda: "/usr/bin/true"
        self.wake.plan_token = lambda: "agents-login-" + "x" * 95
        self.creds = Path(self.tmp.name) / "state" / "credentials.env"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_main(self, accepted, screen=SCREEN):
        self.wake.login_test = unittest.mock.MagicMock(return_value=(accepted, "answered 'OK'" if accepted
                                                                     else "401 Invalid bearer token"))
        mods = {"set_credential": self.sc, "wake": self.wake}
        with unittest.mock.patch.object(NL, "load", side_effect=lambda name, file: mods[name]), \
             unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = NL.main(["agentroom", "AGENTROOM_BUILDER_TOKEN"], capture=lambda argv, env: screen)
        return rc, out.getvalue()

    def test_an_accepted_login_is_saved_whole(self):
        rc, out = self.run_main(True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.wake.login_test.call_args[0][1], FAKE, "the caught login is the one tested")
        self.assertIn(f"AGENTROOM_BUILDER_TOKEN={FAKE}\n", self.creds.read_text())
        self.assertEqual(oct(self.creds.stat().st_mode)[-3:], "600")
        self.assertNotIn(FAKE[13:30], out, "it is never printed")

    def test_a_refused_login_is_not_saved(self):
        # Guards: a login Claude turns away saved anyway - the 92-character
        # one sat in the file through every check that followed.
        rc, out = self.run_main(False)
        self.assertEqual(rc, 1)
        self.assertFalse(self.creds.exists())
        self.assertIn("nothing saved", out)

    def test_no_login_made_saves_nothing(self):
        rc, _ = self.run_main(True, screen=b"Login cancelled.\r\n")
        self.assertEqual(rc, 1)
        self.assertFalse(self.creds.exists())


if __name__ == "__main__":
    unittest.main()
