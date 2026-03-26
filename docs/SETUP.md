# AI Team Setup Guide

Complete setup for the Claude + Gemini autonomous development team orchestrator. Covers a fresh machine from zero to running.

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

If you ever see a `PERMISSIONS ERROR` in `TEAM_STATUS.md`, re-run the above command from the project root.

---

## 3. Claude Code CLI

Install Claude Code for your personal user, then copy the binary to `aidevteam`. A symlink does **not** work — the binary must be a real copy owned by `aidevteam`.

```bash
# Install Claude Code for your user (follow official Anthropic instructions)
npm install -g @anthropic-ai/claude-code   # or however you install it

# Copy to aidevteam
sudo mkdir -p /home/aidevteam/.local/bin
sudo cp ~/.local/bin/claude /home/aidevteam/.local/bin/claude
sudo chown aidevteam:aidevteam /home/aidevteam/.local/bin/claude
```

Create a minimal settings file for `aidevteam` so Claude runs non-interactively:

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

**Claude must be logged in.** Log in once as your personal user — authentication is tied to the binary, not the user account.

---

## 4. Gemini CLI

```bash
# Install Gemini CLI globally
sudo npm install -g @google/gemini-cli

# Log in (run as your personal user, interactive OAuth)
gemini
```

Gemini runs as `aidevteam` but uses the globally installed binary, so no per-user copy is needed. Confirm it is accessible:

```bash
sudo -u aidevteam which gemini
sudo -u aidevteam gemini --version
```

Configure Gemini's MCP and session settings in `/home/addohm/.gemini/settings.json` (or whatever your primary user is). The Gemini CLI reads settings from the **invoking user's** home, not `aidevteam`'s.

---

## 5. Playwright MCP

Both agents use the Playwright MCP server for live browser verification after each task.

### Project `.mcp.json`

Create `.mcp.json` at the project root. Claude Code auto-loads this when `enableAllProjectMcpServers` is true.

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
        "--executable-path",
        "/home/YOUR_USER/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"
      ],
      "env": {}
    }
  }
}
```

Replace `YOUR_USER` and the chromium path with your actual values. Find yours with:

```bash
find ~/.cache/ms-playwright -name "chrome" -type f 2>/dev/null
```

If the chromium binary isn't installed yet:

```bash
npx playwright install chromium
```

### Gemini MCP settings

Add the same playwright entry to `/home/YOUR_USER/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "@playwright/mcp@latest",
        "--browser", "chromium",
        "--executable-path", "/home/YOUR_USER/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"
      ],
      "env": {}
    }
  }
}
```

### Make chromium readable by aidevteam

```bash
sudo chmod o+rx /home/YOUR_USER/.cache
sudo chmod -R o+rx /home/YOUR_USER/.cache/ms-playwright
```

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

The orchestrator commits each agent's work to its own branch. Create these before first run:

```bash
git checkout -b backend-claude
git checkout main
git checkout -b frontend-gemini
git checkout main
```

The branch names are configured at the top of `orchestrator.py`:

```python
BRANCH_CLAUDE = "backend-claude"
BRANCH_GEMINI = "frontend-gemini"
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
| `GEMINI_PRIMARY` | `"gemini-3.1-pro-preview"` | Gemini model for main tasks |
| `GEMINI_FALLBACK_2` | `"gemini-2.5-flash"` | Fallback when primary is rate-limited |
| `BRANCH_CLAUDE` | `"backend-claude"` | Git branch for Claude's commits |
| `BRANCH_GEMINI` | `"frontend-gemini"` | Git branch for Gemini's commits |
| `AGENT_TIMEOUT` | `7200` | Seconds before an agent process is killed |
| `MAX_BATON_DEPTH` | `6` | Max handoff chain before circuit breaker |
| `GEMINI_REVIEW_ENABLED` | `True` | Claude reviews Gemini's diffs before commit |
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

# Skip code review
python ai_team/task.py "quick fix" --no-review

# Audit changed files with Qwen
python ai_team/task.py -a
python ai_team/task.py -a path/to/specific/file.py
```

Tasks are written as Markdown files to `ai_team/messages/inbox_tasks/` and picked up by the orchestrator on its next loop tick.

---

## 14. Verify Everything Works

Run these in order. Each one builds on the previous.

```bash
# 1. aidevteam can run claude
sudo -u aidevteam /home/aidevteam/.local/bin/claude --version

# 2. Claude runs like the orchestrator runs it
sudo -u aidevteam bash -c 'cd /path/to/project && \
  /home/aidevteam/.local/bin/claude \
  --model claude-sonnet-4-6 \
  --dangerously-skip-permissions \
  -p "Reply with only the word HELLO"'

# 3. Playwright MCP works end-to-end
sudo -u aidevteam bash -c 'cd /path/to/project && \
  /home/aidevteam/.local/bin/claude \
  --model claude-sonnet-4-6 \
  --dangerously-skip-permissions \
  -p "Use your playwright MCP browser tool to navigate to http://localhost:5173 and tell me the page title."'
```

All three passing means the full stack is operational.

---

## Directory Structure Reference

```
ai_team/
├── orchestrator.py          # Main daemon
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
