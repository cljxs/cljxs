#!/usr/bin/env python3
"""
agentroom-guard.py — the Agent Room builder's gatekeeper: every tool call the
builder makes is shown to this first, and refused when it is dangerous.

Claude Code runs it as a PreToolUse hook (owner, 2026-10-15). It reads the
call as JSON on stdin and answers with permissionDecision "deny" and a reason
the builder reads, or with nothing (allowed). The builder cannot switch it
off: agentroom.py starts Claude with --settings pointing at a root-owned copy
of this file and --setting-sources "" (no user or project settings, which
could set disableAllHooks), and the copy lives under
/usr/local/lib/agentroom-builder, read-only to the builder.

WHAT IT REFUSES
  * Anything outside the project folder: writes and edits elsewhere; reading
    /root, other homes, /proc, the builder's own Claude and npm settings.
  * Becoming more powerful: sudo, su, setuid bits, setcap.
  * Staying behind: cron, systemd, at, nohup, setsid, disown, screen, tmux.
  * Reaching out: curl, wget, ssh, nc, socat, /dev/tcp, git clone/push/fetch.
  * Digging for the login: env, printenv, /proc, any *_TOKEN or ANTHROPIC_
    variable.
  * Running code fetched from the internet: `| sh`, `$(curl ...)`, npx,
    pnpm/yarn dlx, pip/apt/gem/cargo/go installs.
  * npm with its safety off: --ignore-scripts=false, --unsafe-perm, -g, a
    custom registry or config, npm config / publish / exec / login.

PACKAGES (the booby-trap rules). `npm install <name>` is allowed only when
every package passes vet(): it exists on the npm registry, the version to be
installed was published at least MIN_VERSION_AGE_DAYS ago (hijacked versions
are found and pulled within days), the package itself is older than
MIN_PACKAGE_AGE_DAYS, it is not deprecated, and it is downloaded at least
MIN_WEEKLY_DOWNLOADS times a week. A bare `npm install` / `npm ci` vets
everything package.json asks for - package.json is a file the builder writes,
so it is checked the same way. Git, URL and tarball specs are refused. Install
scripts never run at all: npm's ignore-scripts is forced on by agentroom.py.
Nothing is cached between calls: a cache in the builder's home would be a
file the builder could edit.

Every decision is printed to stderr as one line, which agentroom.py keeps
with the build's log for the watchdog to read.

Standard library only.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MIN_VERSION_AGE_DAYS = 7
MIN_PACKAGE_AGE_DAYS = 30
MIN_WEEKLY_DOWNLOADS = 500
REGISTRY = "https://registry.npmjs.org/"
DOWNLOADS = "https://api.npmjs.org/downloads/point/last-week/"

# (pattern, why). Matched against the whole command, case-sensitive where
# case matters. Word boundaries keep `subject` from matching `su`.
BASH_RULES = [
    (r"(^|[;&|(\s`])(sudo|su|doas|pkexec|runuser|chroot|nsenter)\b", "running as another user is not allowed"),
    (r"\bchmod\s+[^;&|]*[ugoa]*\+[rwx]*s|\bchmod\s+[0-7]*[4-7][0-7]{3}\b|\bsetcap\b", "setuid bits and capabilities are not allowed"),
    (r"\b(crontab|systemctl|launchctl)\b|(^|[;&|]\s*)(at|batch|service)\s", "scheduling or services are not allowed - nothing may outlive the session"),
    (r"\b(nohup|setsid|disown|screen|tmux)\b", "nothing may keep running after the session"),
    (r"&\s*($|[;)])", "running things in the background is not allowed"),
    (r"\b(curl|wget|nc|ncat|netcat|socat|ssh|scp|sftp|rsync|telnet|ftp|aria2c)\b", "network tools are not allowed - packages come through npm, vetted"),
    (r"/dev/(tcp|udp)/", "raw network connections are not allowed"),
    (r"\bgit\s+(clone|push|pull|fetch|remote|submodule|ls-remote|config\s+--global)\b", "git may only be used locally"),
    (r"(^|[;&|(\s`])(env|printenv)(\s|$|[;&|)])|(^|[;&|(\s`])(export\s+-p|declare\s+-x|set)\s*($|[;&|)])",
     "listing the environment is not allowed"),
    (r"/proc\b|environ\b", "/proc is not allowed"),
    (r"\$\{?[A-Z_]*(TOKEN|SECRET|PASSWORD|API_KEY|OAUTH)[A-Z_]*\}?|\bANTHROPIC_|CLAUDE_CODE_", "secrets and Claude's own settings are off limits"),
    (r"/root\b|~root\b|/home/(?!agentbuilder\b)|/etc/(shadow|sudoers|ssh)", "that folder is off limits"),
    (r"\.claude(/|\.json|\b)|\.npmrc|NPM_CONFIG|npm_config_", "the builder's own Claude and npm settings are off limits"),
    (r"\|\s*(ba|z|da|k)?sh\b|\b(ba|z)?sh\s+-c\s+[\"']?\$\(|\beval\b|base64\s+(-d|--decode)", "running code built at run time is not allowed"),
    (r"\b(npx|bunx|pnpx)\b|\b(pnpm|yarn)\s+dlx\b|\bnpm\s+(exec|x)\b", "running packages straight from the internet is not allowed"),
    (r"\b(pip|pip3|pipx|poetry|conda|uv|apt|apt-get|dpkg|snap|gem|cargo|brew)\s+(install|add|i)\b|\bgo\s+(install|get)\b",
     "only npm packages, vetted, may be installed"),
    (r"\bnpm\s+(publish|config|set|login|adduser|token|owner|link|unpublish|deprecate|access|whoami)\b",
     "that npm command is not allowed"),
    (r"--ignore-scripts(=|\s+)false|--unsafe-perm|--foreground-scripts|--registry\b|--userconfig\b|--globalconfig\b|(\s|^)(-g|--global)\b|--location[= ]global",
     "npm's safety settings may not be changed"),
    (r"\b(mount|umount|iptables|ufw|useradd|usermod|passwd|kill\s+-9\s+1\b|reboot|shutdown|halt)\b", "system administration is not allowed"),
    (r"\b(docker|podman|kubectl|lxc)\b", "containers are not allowed"),
    (r"\b(xmrig|minerd|cpuminer|ethminer)\b|stratum\+tcp", "that is a crypto miner"),
]

NPM_INSTALL = re.compile(r"\bnpm\s+(install|i|add|ci|update|up)\b([^;&|]*)")


def project():
    return Path(os.environ.get("AGENTROOM_PROJECT") or os.getcwd()).resolve()


def inside(path, root):
    try:
        p = Path(path)
        p = (root / p) if not p.is_absolute() else p
        p = p.resolve()
        return p == root or root in p.parents
    except (OSError, ValueError):
        return False


def path_rule(tool, path, root):
    """Why a file tool may not touch `path`, or None."""
    if not path:
        return None
    s = str(path)
    if re.search(r"(^|/)\.claude(/|$)|\.claude\.json|\.npmrc$|(^|/)\.git/hooks(/|$)", s):
        return "the builder's own Claude, npm and git-hook settings are off limits"
    if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        return None if inside(s, root) else "files may only be written inside the project folder"
    if re.match(r"^(/root|/proc|/etc/(shadow|sudoers|ssh)|/home/(?!agentbuilder\b))", str(Path(s).expanduser())):
        return "that folder is off limits"
    return None


def specs(argline):
    """The package specs on an npm install line - flags dropped."""
    out = []
    for tok in argline.split():
        if tok.startswith("-"):
            continue
        out.append(tok.strip("'\""))
    return out


def split_spec(spec):
    """('name', 'version or tag or None') from name, name@1.2.3, @scope/name@^1."""
    if spec.startswith("@"):
        m = re.match(r"^(@[^/@\s]+/[^@\s]+)(?:@(.+))?$", spec)
    else:
        m = re.match(r"^([^@\s]+)(?:@(.+))?$", spec)
    return (m.group(1), m.group(2)) if m else (spec, None)


def _get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def _days(stamp, now):
    t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return (now - t).total_seconds() / 86400


def pick_version(doc, want):
    """The version npm would install for `want`: an exact version, a tag, or
    the newest published version matching a simple ^/~/>= range; None if
    that cannot be worked out here - which is a refusal, not a guess."""
    versions = list((doc.get("versions") or {}).keys())
    tags = doc.get("dist-tags") or {}
    if not want or want in tags:
        return tags.get(want or "latest")
    if want in versions:
        return want
    m = re.match(r"^([\^~]|>=)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$", want)
    if not m:
        return None
    op, a, b, c = m.group(1) or "", int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0)

    def key(v):
        p = re.match(r"^(\d+)\.(\d+)\.(\d+)$", v)
        return tuple(map(int, p.groups())) if p else None
    ok = []
    for v in versions:
        k = key(v)
        if k is None or k < (a, b, c):
            continue
        if op == "^" and k[0] != a:
            continue
        if op == "~" and k[:2] != (a, b):
            continue
        if op == "" and k != (a, b, c):
            continue
        ok.append(k)
    return ".".join(map(str, max(ok))) if ok else None


def vet(spec, get=_get, now=None):
    """None when `spec` may be installed, else why not."""
    now = now or datetime.now(timezone.utc)
    if re.match(r"^(git\+|git:|github:|gitlab:|bitbucket:|https?:|file:|link:|\.|/)", spec) or spec.endswith((".tgz", ".tar.gz")):
        return f"{spec}: only packages from the npm registry, by name"
    name, want = split_spec(spec)
    if not re.match(r"^(@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", name):
        return f"{spec}: not a package name"
    try:
        doc = get(REGISTRY + urllib.parse.quote(name, safe="@"))
    except Exception as e:
        return f"{name}: could not be checked on the npm registry ({str(e)[:60]}) - not installing unchecked"
    times = doc.get("time") or {}
    version = pick_version(doc, want)
    if not version or version not in times:
        return f"{spec}: could not tell which version would be installed - name an exact version"
    if (doc.get("versions") or {}).get(version, {}).get("deprecated"):
        return f"{name}@{version} is deprecated"
    if _days(times[version], now) < MIN_VERSION_AGE_DAYS:
        return (f"{name}@{version} was published less than {MIN_VERSION_AGE_DAYS} days ago - "
                "new versions are when hijacked packages strike; pin an older version")
    if times.get("created") and _days(times["created"], now) < MIN_PACKAGE_AGE_DAYS:
        return f"{name} is less than {MIN_PACKAGE_AGE_DAYS} days old"
    try:
        n = int(get(DOWNLOADS + urllib.parse.quote(name, safe="@")).get("downloads") or 0)
    except Exception:
        return f"{name}: its download count could not be checked - not installing unchecked"
    if n < MIN_WEEKLY_DOWNLOADS:
        return f"{name} is downloaded only {n} times a week - too little used to trust"
    return None


def package_json_specs(root):
    try:
        d = json.loads((root / "package.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for k in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, want in (d.get(k) or {}).items():
            out.append(f"{name}@{want}" if want else name)
    return out


def bash_rule(cmd, root, get=_get):
    """Why a shell command may not run, or None."""
    for pattern, why in BASH_RULES:
        if re.search(pattern, cmd):
            return why
    for m in NPM_INSTALL.finditer(cmd):
        named = specs(m.group(2))
        todo = named or package_json_specs(root)
        for s in todo:
            why = vet(s, get=get)
            if why:
                return "package refused: " + why
    return None


def decide(call, get=_get):
    """(allowed, why) for one tool call."""
    tool = call.get("tool_name") or ""
    inp = call.get("tool_input") or {}
    root = project()
    if tool == "Bash":
        why = bash_rule(str(inp.get("command") or ""), root, get=get)
    elif tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        why = path_rule(tool, inp.get("file_path") or inp.get("notebook_path"), root)
    elif tool in ("Glob", "Grep"):
        why = path_rule(tool, inp.get("path"), root)
    elif tool in ("WebFetch", "WebSearch", "Task", "Agent"):
        why = "that tool is not allowed for the builder"
    else:
        why = None
    return why is None, why


def main():
    try:
        call = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        call = {}
    ok, why = decide(call)
    tool = call.get("tool_name") or "?"
    what = (call.get("tool_input") or {})
    shown = str(what.get("command") or what.get("file_path") or what.get("path") or "")[:300]
    print(f"agentroom-guard: {'ALLOW' if ok else 'DENY'} {tool}: {shown}" + ("" if ok else f" - {why}"),
          file=sys.stderr)
    if not ok:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"Blocked by the Agent Room's guard: {why}."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
