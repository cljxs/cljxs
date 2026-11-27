# The Claude plan

Agents listed with `"engine": "claude"` in `tasks/engines.json` wake on the
owner's Claude subscription through the Claude Code CLI, instead of on
OpenRouter. `scripts/wake.py` does it; its docstring says how and why.

`credentials.env` here holds the plan's login token, `CLAUDE_CODE_OAUTH_TOKEN`,
mode 600. It is never committed (`.gitignore`: `**/credentials.env`).

Set up, on the droplet, one line at a time:

    curl -fsSL https://claude.ai/install.sh | bash
    ~/.local/bin/claude setup-token
    python3 scripts/set-credential.py plan CLAUDE_CODE_OAUTH_TOKEN
    python3 scripts/wake.py check

`setup-token` prints a link: open it, approve, and it prints a long token that
starts `sk-ant-oat01-`. Copy it and paste it at set-credential's prompt, which
does not echo it.

What moves to the plan: the agents' wakes (Ace, Belfort, Scout, Paul, and
Emily's builds). What stays on OpenRouter: everything a script calls directly
- Emily's artwork and TikTok scenes, Clip's captions and hooks, the Agent
Room's three models.
