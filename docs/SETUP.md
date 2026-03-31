# AI Team Setup Guide

Complete setup for the Claude + Gemini autonomous development team orchestrator. Covers a fresh machine from zero to running.

---

## TL;DR — Automated Setup

Most of the setup can be handled by the provided script. Run it as your primary user with sudo:

```bash
sudo bash ai_team/setup.sh
```

The script handles: user creation, directory permissions, Claude binary copy, Chromium install, sandbox permissions, Gemini MCP settings, git branches, and Python dependencies. After it finishes, two interactive OAuth logins remain (see the script output for exact commands).

---

## Overview

The AI team consists of:
- **Claude** (Backend Lead) — Claude Code CLI, runs as `aidevteam`
- **Gemini** (Frontend Lead) — Gemini CLI, runs as `aidevteam`
- **Qwen** (Code Reviewer) — Ollama model, reviews diffs before commits
- **Orchestrator** — Python daemon that routes tasks, runs QA, manages handoffs
- **Discord Bot** — Optional monitor that streams logs and tasks to Discord channels

---

## 1. System User

Create a dedicated `aidevteam` user that the orchestrator runs as. This keeps AI-written files owned by a known account and separate from your personal user.

```bash
sudo useradd -m -s /bin/bash aidevteam
sudo usermod -aG docker aidevteam    # needed if AI runs docker commands
sudo usermod -aG ollama aidevteam    # needed for ollama access
```

---

## 2. Project Directory Permissions

The orchestrator and both AI agents must be able to read and write the project directory. Use ACLs to grant `aidevteam` access without changing base ownership.

```bash
# Grant aidevteam read/write/execute on all project files (recursively)
sudo setfacl -R -m u:aidevteam:rwX /path/to/project

# Set the default ACL so new files inherit the same permissions
sudo setfacl -R -d -m u:aidevteam:rwX /path/to/project
```

If you ever see a `PERMISSIONS ERROR` in `TEAM_STATUS.md`, re-run the above commands from the project root.

---

## 3. Claude Code CLI

Install Claude Code for your personal user, then copy the binary to `aidevteam`.

**Important:** The Claude binary at `~/.local/bin/claude` is a symlink to a versioned file. Use `cp -L` to copy the real binary — a plain `cp` or symlink will break for `aidevteam`.

```bash
# Install Claude Code for your user (follow official Anthropic instructions)
npm install -g @anthropic-ai/claude-code

# Copy the real binary (cp -L dereferences the symlink)
sudo mkdir -p /home/aidevteam/.local/bin
sudo cp -L ~/.local/bin/claude /home/aidevteam/.local/bin/claude
sudo chown aidevteam:aidevteam /home/aidevteam/.local/bin/claude
```

Add the binary to `aidevteam`'s PATH:

```bash
sudo bash -c 'echo "export PATH=\$PATH:/home/aidevteam/.local/bin" >> /home/aidevteam/.bashrc'
```

Create a settings file for `aidevteam` so Claude runs non-interactively:

```bash
sudo mkdir -p /home/aidevteam/.claude
sudo tee /home/aidevteam/.claude/settings.json > /dev/null <<'EOF'
{
  "skipDangerousModePermissionPrompt": true,
  "enableAllProjectMcpServers": true
}
EOF
sudo chown -R aidevteam:aidevteam /home/aidevteam/.claude
```

- `skipDangerousModePermissionPrompt` — allows `--dangerously-skip-permissions` without an interactive prompt
- `enableAllProjectMcpServers` — auto-approves MCP servers declared in the project's `.mcp.json`

**Authenticate Claude.** Switch to `aidevteam` and log in once interactively:

```bash
sudo -su aidevteam
/home/aidevteam/.local/bin/claude
exit
```

**After every `claude update`:** The versioned target changes, so re-copy the binary:

```bash
sudo cp -L ~/.local/bin/claude /home/aidevteam/.local/bin/claude
sudo chown aidevteam:aidevteam /home/aidevteam/.local/bin/claude
```

---

## 4. Gemini CLI

```bash
# Install Gemini CLI globally (available to all users)
sudo npm install -g @google/gemini-cli
```

Gemini reads its settings from the **home directory of the user running it** — in this case `aidevteam`. Write the settings directly there:

```bash
sudo mkdir -p /home/aidevteam/.gemini
sudo tee /home/aidevteam/.gemini/settings.json > /dev/null <<'EOF'
{
  "general": {
    "sessionRetention": {
      "enabled": true,
      "maxAge": "30d",
      "warningAcknowledged": true
    }
  },
  "security": {
    "auth": {
      "selectedType": "oauth-personal"
    }
  },
  "context": {
    "fileFiltering": {
      "respectGitIgnore": false
    }
  },
  "mcpServers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "@playwright/mcp@latest",
        "--browser",
        "chromium",
        "--headless"
      ],
      "env": {}
    }
  },
  "ide": {
    "hasSeenNudge": true,
    "enabled": true
  }
}
EOF
sudo chown -R aidevteam:aidevteam /home/aidevteam/.gemini
```

**Authenticate Gemini.** Switch to `aidevteam` and log in once interactively:

```bash
sudo -su aidevteam
gemini
exit
```

---

## 5. Playwright MCP

Both agents use the Playwright MCP server for live browser verification after each task. Chromium is installed per-user under `aidevteam` so there are no cross-user permission dependencies.

### Install Chromium for aidevteam

```bash
sudo -u aidevteam bash -c 'npx playwright install chromium'
```

Note: If prompted about missing system dependencies, ignore the warning and run without `--with-deps`. The system libraries are already present from your primary user's Chromium install.

### Fix the chrome_sandbox setuid bit

Playwright requires `chrome_sandbox` to be owned by root with the setuid bit set. Without this, Chromium silently fails to launch. Run this after every `npx playwright install chromium`:

```bash
# Find all installed sandbox binaries and fix them
sudo -u aidevteam find /home/aidevteam/.cache/ms-playwright -name chrome_sandbox | \
  xargs -I{} sudo bash -c 'chown root:root "{}" && chmod 4755 "{}"'
```

Verify they are correct (look for `rws` in the permissions):

```bash
sudo -u aidevteam find /home/aidevteam/.cache/ms-playwright -name chrome_sandbox \
  -exec ls -la {} \;
# Expected: -rwsr-xr-x 1 root root ...
```

### Project MCP config (`.mcp.json` and `.claude.json`)

Both files are already present in the repository with the correct configuration. Claude Code auto-loads `.mcp.json` when `enableAllProjectMcpServers` is true.

The working configuration uses `--headless` so Chromium never requires a display server:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "@playwright/mcp@latest",
        "--browser",
        "chromium",
        "--headless"
      ],
      "env": {}
    }
  }
}
```

### Chromium system library dependencies

On **Fedora/Nobara** the required libraries are typically pre-installed. On minimal Debian or Arch installs they may be missing, causing Chromium to fail silently after setup. Install them if needed:

```bash
# Debian / Ubuntu
sudo apt install libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2

# Arch
sudo pacman -S nss atk at-spi2-atk libcups libdrm libxkbcommon \
  libxcomposite libxdamage libxfixes libxrandr mesa alsa-lib
```

If you used `npx playwright install chromium` without `--with-deps` and Chromium fails to launch, installing the above packages is the first thing to try.

### Pre-cache the MCP package

On first use, `npx` downloads `@playwright/mcp` which causes a multi-minute hang. Pre-cache it for `aidevteam`:

```bash
sudo -u aidevteam bash -c 'npx @playwright/mcp@latest --version'
```

---

## 6. Ollama + Qwen Reviewer

The orchestrator uses a local Qwen model to review AI-written diffs before they are committed.

```bash
# Install ollama (https://ollama.com)
curl -fsSL https://ollama.com/install.sh | sh

# Enable and start the service
sudo systemctl enable ollama
sudo systemctl start ollama

# Pull the reviewer model
ollama pull qwen-reviewer
```

The orchestrator will offer to start ollama and pull the model automatically on first run if they are missing.

---

## 7. Python Dependencies

The orchestrator and Discord bot require a few packages:

```bash
pip install "discord.py>=2.0" python-dotenv certifi
```

---

## 8. Git Branch Setup

The project uses a three-tier branching model. All active development flows through `dev`; `main` is only touched for verified production releases.

```
main          ← production only; never directly committed to by agents or orchestrator
  └── dev     ← integration branch; receives all agent merges; base for agent branches
        ├── backend-claude   ← Claude's working branch (cut from dev, merged back into dev)
        └── frontend-gemini  ← Gemini's working branch (cut from dev, merged back into dev)
```

### Initial setup

If `dev` does not exist yet:

```bash
git checkout main
git checkout -b dev
git push -u origin dev   # if you have a remote
```

Create the agent branches from `dev`:

```bash
git checkout dev
git checkout -b backend-claude
git checkout dev
git checkout -b frontend-gemini
git checkout dev   # return to dev before starting the orchestrator
```

### Orchestrator-managed branching (automatic)

Once the orchestrator is running, **branch management is fully automatic**:

| Event | What the orchestrator does |
|---|---|
| Task starts | Checks out the agent branch; merges latest `dev` into it so the agent starts from current state |
| Branch missing | Creates it from `dev` automatically |
| Per-agent QA passes | Commits to the agent branch |
| After commit | Merges agent branch → `dev` via `--no-ff`; runs full post-merge QA (both `manage.py check` and `npm run lint`) |
| Post-merge QA passes | Logs success; updates `dev`; routes any handoff |
| Post-merge QA fails | Hard-resets `dev` to pre-merge state; kickbacks the agent with the error |
| Merge conflict | Aborts merge; kickbacks the agent with conflict details |

### Promoting dev → main (manual)

Only promote when `dev` is stable and represents a clean, tested progression. The orchestrator does **not** push to `main` automatically.

```bash
git checkout main
git merge --no-ff dev -m "Release: <description>"
git push origin main
```

### Branch name constants

The branch names are configured at the top of `orchestrator.py`:

```python
BRANCH_MAIN   = "main"             # production — never auto-committed to
BRANCH_DEV    = "dev"              # integration — all agent merges land here
BRANCH_CLAUDE = "backend-claude"   # Claude's working branch
BRANCH_GEMINI = "frontend-gemini"  # Gemini's working branch
```

Change them to match your project's naming convention.

---

## 9. Discord Bot (Optional)

The bot streams orchestrator logs, new tasks, and test-flight tickets to Discord in real time.

**Create a Discord bot:**
1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. New Application → Bot → copy the token
3. Enable **Message Content Intent** under Privileged Gateway Intents
4. Invite the bot to your server with `Send Messages` + `Manage Channels` permissions

**Create four channels** in your server: `#logs`, `#tasks`, `#todos`, `#status`

**Add to your `.env`:**

```env
DISCORD_BOT_TOKEN=your_token_here
DISCORD_LOGS_CHANNEL_ID=111111111111111111
DISCORD_TASKS_CHANNEL_ID=222222222222222222
DISCORD_TODOS_CHANNEL_ID=333333333333333333
DISCORD_STATUS_CHANNEL_ID=444444444444444444
```

The orchestrator will offer to start the Discord bot on launch if it is not already running.

---

## 10. Orchestrator Configuration

Key constants at the top of `ai_team/orchestrator.py` to review for each project:

| Constant | Default | Description |
|---|---|---|
| `CLAUDE_PRIMARY` | `"opus"` | Claude model for main tasks |
| `GEMINI_PRIMARY` | `"gemini-2.5-pro"` | Gemini model for main tasks |
| `GEMINI_FALLBACK_2` | `"gemini-2.5-flash"` | Fallback when primary is rate-limited; also used after substantive replies that can't emit JSON |
| `BRANCH_MAIN` | `"main"` | Production branch — orchestrator never commits here directly |
| `BRANCH_DEV` | `"dev"` | Integration branch — all agent merges land here; post-merge QA runs against this |
| `BRANCH_CLAUDE` | `"backend-claude"` | Claude's working branch (cut from `dev`, merged back into `dev`) |
| `BRANCH_GEMINI` | `"frontend-gemini"` | Gemini's working branch (cut from `dev`, merged back into `dev`) |
| `AGENT_TIMEOUT` | `7200` | Seconds before an agent process is killed |
| `MAX_TASK_RETRIES` | `3` | Max QA fix attempts before a task is permanently failed |
| `MAX_BATON_DEPTH` | `6` | Max handoff chain before circuit breaker |
| `SUBSTANTIVE_THRESHOLD` | `3000` | Reply length (chars) above which a rate-limit at end-of-run skips full re-run and goes straight to JSON-only retry |
| `FULL_REVIEW_THRESHOLD` | `10` | Full-file Qwen review every N diff reviews |

---

## 11. CLAUDE.md and GEMINI.md

Each agent reads a role file at the project root that defines its domain, responsibilities, and workflow rules. Copy and adapt from this project:

- `CLAUDE.md` — Backend lead instructions (Django, API, models, tests)
- `GEMINI.md` — Frontend lead instructions (React, components, styling)

At minimum each file must define:
- The agent's domain (which directories it owns)
- The mandatory QA command to run before handoff
- The JSON output structure the orchestrator expects
- Protected paths the agent must never write to

---

## 12. Running the Orchestrator

```bash
# Always run as aidevteam so file ownership is consistent
sudo -u aidevteam bash -c 'cd /path/to/project && python ai_team/orchestrator.py'
```

On first launch the orchestrator will:
1. Check and optionally start ollama
2. Pull the qwen-reviewer model if missing
3. Check and optionally start the Discord bot
4. Run a pre-flight permission check
5. Enter the main task loop

---

## 13. Sending Tasks

```bash
# Default — routes to Claude
python ai_team/task.py "your task description here"

# Explicit routing
python ai_team/task.py -c "backend task for Claude"
python ai_team/task.py -g "frontend task for Gemini"

# Skip Qwen code review
python ai_team/task.py "quick fix" --no-review

# Skip Claude's UI/UX review of Gemini's output (per-task)
python ai_team/task.py -g "frontend tweak" -dcr
python ai_team/task.py -g "frontend tweak" --disable-claude-review

# Audit changed files with Qwen
python ai_team/task.py -a
python ai_team/task.py -a path/to/specific/file.py
```

The same `-dcr` flag is available in the Discord bot: `!task -g -dcr <text>`

Tasks are written as Markdown files to `ai_team/messages/inbox_tasks/` and picked up by the orchestrator on its next loop tick.

### Dropping tasks manually

You can also drop a plain `.md` file directly into `ai_team/messages/inbox_tasks/`. The orchestrator auto-numbers it via the same counter as `task.py`. Supported headers at the top of the file:

```markdown
To: Claude
No-Review: true
Skip-Claude-UI-Review: true

Your task text here...
```

| Header | Values | Effect |
|---|---|---|
| `To:` | `Claude` or `Gemini` | Route to a specific agent (defaults to Claude if absent) |
| `No-Review: true` | — | Skip Qwen diff review for this task |
| `Skip-Claude-UI-Review: true` | — | Skip Claude's UI/UX supervisor review for this Gemini task |

---

## 14. Verify Everything Works

Run these in order. Each one builds on the previous.

```bash
# 1. aidevteam can find and run claude
sudo -u aidevteam /home/aidevteam/.local/bin/claude --version

# 2. aidevteam can find and run gemini
sudo -u aidevteam gemini --version

# 3. Claude runs as the orchestrator runs it (from project root, with permissions flag)
sudo -su aidevteam bash -c 'cd /path/to/project && \
  /home/aidevteam/.local/bin/claude \
  --model claude-sonnet-4-6 \
  --dangerously-skip-permissions \
  -p "Reply with only the word HELLO"'

# 4. Gemini runs as the orchestrator runs it
sudo -su aidevteam bash -c 'cd /path/to/project && \
  gemini -y -m gemini-2.5-pro \
  -p "Reply with only the word HELLO"'

# 5. Claude Playwright MCP works end-to-end
sudo -su aidevteam bash -c 'cd /path/to/project && \
  /home/aidevteam/.local/bin/claude \
  --model claude-sonnet-4-6 \
  --dangerously-skip-permissions \
  -p "Use your playwright MCP browser tool to navigate to http://localhost:5173 and tell me the page title."'

# 6. Gemini Playwright MCP works end-to-end
sudo -su aidevteam bash -c 'cd /path/to/project && \
  gemini -y -m gemini-2.5-pro \
  -p "Use your playwright MCP browser tool to navigate to http://localhost:5173 and tell me the page title."'
```

All six passing means the full stack is operational.

---

## Directory Structure Reference

```
ai_team/
├── orchestrator.py          # Main daemon
├── setup.sh                 # Automated setup script (run with sudo)
├── task.py                  # CLI for sending tasks
├── discord_bot.py           # Optional Discord monitor
├── TEAM_STATUS.md           # Live status dashboard (auto-updated)
├── docs/                    # This file and other documentation
├── logs/                    # orchestrator.log, claude.log, gemini.log
└── messages/
    ├── inbox_tasks/         # Human → orchestrator (drop .md files here)
    ├── inbox_claude/        # Orchestrator → Claude
    ├── inbox_gemini/        # Orchestrator → Gemini
    ├── outbox_human/        # Test-flight tickets for the human
    ├── processed/           # Completed tasks (+ .md sidecars)
    └── failed/              # Failed tasks (+ .md sidecars)
```
