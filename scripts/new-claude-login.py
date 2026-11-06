#!/usr/bin/env python3
"""
new-claude-login.py - make a Claude plan login and save it, with no copying.

    python3 scripts/new-claude-login.py agentroom AGENTROOM_BUILDER_TOKEN
    python3 scripts/new-claude-login.py plan CLAUDE_CODE_OAUTH_TOKEN

WHY THIS EXISTS. `claude setup-token` prints a 108-character login, and the
only way from there to credentials.env was to select it on an iPad, copy it
and paste it into set-credential.py. On 2026-10-16 that saved 92 characters,
attempt after attempt ("401 Invalid bearer token"), and the screenshot sent
to ask why showed the whole login.

So this runs `claude setup-token` itself, on a pseudo-terminal: its screens,
the link to open and the code to paste back all work as usual, but the login
line is caught, blanked out before it reaches the screen, tested with one tiny
run (wake.login_test, the test `wake.py check` uses) and saved only if Claude
accepts it - where set-credential.py would save it, the same way.

Standard library only.
"""

import importlib.util
import os
import re
import select
import signal
import struct
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# What setup-token prints: sk-ant-oat01- and the rest. Only ever matched in
# memory; nothing caught is written anywhere but credentials.env.
TOKEN = re.compile(rb"sk-ant-[a-z]{2,8}\d{0,3}-[A-Za-z0-9_-]{40,}")
TOKEN_CHAR = re.compile(rb"[A-Za-z0-9_-]")
START = b"sk-ant-"
MASK = b"[login caught - hidden]"
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")
MIN_COLS = 160          # wide enough that the login is never wrapped onto two lines


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Redactor:
    """Passes a terminal's output through with every login replaced by MASK.
    Output that might be the start of one is held back until it is clear -
    a login split between two reads must not show its first half."""

    def __init__(self):
        self.held = b""

    def feed(self, data):
        buf = self.held + data
        # The trailing run of characters a login is made of, if it could be
        # (the start of) one, waits for the next read.
        i = len(buf)
        while i > 0 and TOKEN_CHAR.match(buf[i - 1:i]):
            i -= 1
        tail = buf[i:]
        cut = len(buf)
        for j in range(len(tail)):
            rest = tail[j:]
            if rest.startswith(START) or START.startswith(rest):
                cut = i + j
                break
        self.held = buf[cut:]
        return hide(buf[:cut])

    def flush(self):
        out, self.held = hide(self.held), b""
        return out


def hide(data):
    # As wide as the login, so the screen's redraw leaves none of the line
    # behind it showing (2026-10-16: "[login caught - hidden]Term2, Fn in...").
    return TOKEN.sub(lambda m: MASK.ljust(len(m.group(0))), data)


def caught(raw):
    """The last login in a terminal's raw output, or None."""
    found = TOKEN.findall(ANSI.sub(b"", raw))
    return found[-1].decode() if found else None


def run_on_pty(argv, env, stdin_fd=0, stdout_fd=1):
    """Run argv on a pseudo-terminal, the person's keys in and its screen out
    (logins blanked). Returns everything it printed, raw, in memory only."""
    import pty
    import termios
    import tty
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    try:
        import fcntl
        rows, cols = 40, MIN_COLS
        if os.isatty(stdout_fd):
            rows, cols = struct.unpack("hh", fcntl.ioctl(stdout_fd, termios.TIOCGWINSZ, b"\0" * 4))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("hhhh", rows, max(cols, MIN_COLS), 0, 0))
    except OSError:
        pass
    old = termios.tcgetattr(stdin_fd) if os.isatty(stdin_fd) else None
    if old:
        tty.setraw(stdin_fd)
    seen, red, watch = bytearray(), Redactor(), [fd, stdin_fd]
    try:
        while fd in watch:
            r, _, _ = select.select(watch, [], [])
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    watch.remove(fd)
                else:
                    seen += data
                    os.write(stdout_fd, red.feed(data))
            if stdin_fd in r:
                data = os.read(stdin_fd, 1024)
                if data:
                    os.write(fd, data)
                else:
                    watch.remove(stdin_fd)
        os.write(stdout_fd, red.flush())
    finally:
        if old:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        os.close(fd)
    return bytes(seen)


def main(argv=None, capture=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: new-claude-login.py <agent> <KEY_NAME>\n"
              "  e.g. new-claude-login.py agentroom AGENTROOM_BUILDER_TOKEN", file=sys.stderr)
        return 2
    agent, name = argv
    sc, wake = load("set_credential", "set-credential.py"), load("wake", "wake.py")
    path, why = sc.credentials_path(agent)
    if why:
        print(why, file=sys.stderr)
        return 1
    binary = wake.claude_bin()
    if not binary:
        print("the claude program is not installed - see plan/README.md", file=sys.stderr)
        return 1
    print("Claude will show a link: open it, press Authorize, and paste the code it gives you back here.\n"
          "The login it makes is caught and saved for you - you do not copy anything.\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    raw = (capture or run_on_pty)([binary, "setup-token"], env)
    token = caught(raw)
    del raw
    if not token:
        print("\n\nNo login was made, so nothing was saved. Run this again to try once more.")
        return 1
    print(f"\n\nCaught a login of {len(token)} characters. Testing it with one tiny run...")
    if agent != "plan":
        try:
            agents = wake.plan_token()
        except Exception:
            agents = ""
        if agents and token == agents:
            print("That is the same login your agents use - nothing saved.")
            return 1
    ok, said = wake.login_test(binary, token)
    if not ok:
        print(f"Claude refused it ({said}) - nothing saved. Run this again to make another.")
        return 1
    count = sc.write(path, name, token)
    print(f"Claude accepted it: {said}")
    print(f"\nwrote {name} to {path}")
    print(f"  {sc.masked(token)}")
    print(f"  {count} key(s) in the file, mode {oct(path.stat().st_mode)[-3:]}")
    print("\nThe login was never shown and is not in your shell history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
