# AI Team — Usage Instructions

Day-to-day operations guide for the AI development team orchestrator. For initial setup, see `SETUP.md`.

---

## Starting the Stack

```bash
# 1. Start the Docker dev stack (if not already running)
docker compose -f docker-compose.dev.yml up -d

# 2. Start ollama (code reviewer)
sudo systemctl start ollama

# 3. Start the orchestrator (always run as aidevteam)
sudo -u aidevteam bash -c 'cd /path/to/project && python ai_team/orchestrator.py'

# 4. (Optional) Start the Discord monitor
sudo -u aidevteam bash -c 'cd /path/to/project && python ai_team/discord_bot.py'
```

The orchestrator offers to start ollama and the Discord bot automatically on launch if they are not running.

---

## Sending Tasks

### CLI (recommended)

```bash
# Route to the backend agent (default)
python ai_team/task.py "your task here"
python ai_team/task.py -c "backend task"

# Route to the frontend agent
python ai_team/task.py -g "frontend task"

# Skip Qwen code review for this task
python ai_team/task.py "quick fix" --no-review
python ai_team/task.py "quick fix" -nr

# Skip the reviewer agent's UI/UX review for this task
python ai_team/task.py -g "small frontend tweak" -dcr
python ai_team/task.py -g "small frontend tweak" --disable-ui-review
```

Routing flags are derived from `agents_config.py` — the first letter of each agent's name becomes the short flag. For the default `claude`/`gemini` config: `-c` → Claude, `-g` → Gemini.

### Discord bot

```
!task -c <text>    → route to backend agent (Claude)
!task -g <text>    → route to frontend agent (Gemini)
!task -c -nr <text>  → skip code review
!task -g -dcr <text> → skip UI/UX review
```

The bot will confirm before queuing. Reply `y` or `n` in the channel.

### Manual file drop

Drop a `.md` file into `ai_team/messages/inbox_tasks/` and the orchestrator picks it up on the next loop tick. Supported headers:

```markdown
To: Claude
No-Review: true
Skip-UI-Review: true

Your full task description here.
Multi-line is fine.
```

| Header | Values | Effect |
|---|---|---|
| `To:` | any configured agent name (e.g. `Claude`, `Gemini`) | Route to a specific agent |
| `No-Review: true` | — | Skip Qwen diff review |
| `Skip-UI-Review: true` | — | Skip reviewer agent's UI/UX check |

If `To:` is absent, the task routes to the first configured agent (backend by default).

---

## Monitoring

### Status dashboard

`ai_team/TEAM_STATUS.md` — auto-updated every few seconds by the orchestrator. Shows:
- Current task per agent (with elapsed time)
- Queue depths
- Recent kickbacks and hard failures
- Session work time per agent
- Idle QA status

### Logs

```bash
# Live orchestrator log
tail -f ai_team/logs/orchestrator.log

# Per-agent logs
tail -f ai_team/logs/claude.log
tail -f ai_team/logs/gemini.log
```

### Discord channels (if bot is running)

| Channel | Content |
|---|---|
| `#logs` | Streamed log lines from all agents and orchestrator |
| `#tasks` | New tasks as they land in each agent's inbox |
| `#todos` | Test-flight tickets for human verification (react with ✅ to dismiss) |
| `#status` | Live TEAM_STATUS.md dashboard (channel icon changes with state) |

---

## Pausing and Resuming

```bash
# Pause the orchestrator (it will finish the current task, then halt)
touch ai_team/messages/PAUSED

# Resume
rm ai_team/messages/PAUSED
```

The orchestrator checks for the sentinel file between every queue poll. Active agent processes are not interrupted — the pause takes effect after the current task completes.

---

## Toggling UI/UX Review

The reviewer agent (Claude by default) reviews the reviewed agent's (Gemini's) frontend diff before every commit. This catches UI regressions without human intervention.

**Disable for one session** (via CLI):
```bash
python ai_team/orchestrator.py --disable-claude-review
```
This creates `ai_team/messages/DISABLE_CLAUDE_REVIEW`. The review is re-enabled automatically next time the orchestrator starts without that flag.

**Toggle at runtime** (via Discord):
```
!dcr
```

**Disable per-task** (skip review for one specific task):
```bash
python ai_team/task.py -g "minor change" -dcr
```

---

## Task Lifecycle

```
Human sends task
      ↓
inbox_tasks/task_NNNN.md
      ↓
Orchestrator routes to agent inbox (inbox_claude/ or inbox_gemini/)
      ↓
Agent executes task
      ↓
Qwen reviews the diff (unless --no-review)
      ↓
Per-agent QA (manage.py check or npm run lint)
      ↓
Reviewer agent checks UI/UX diff (frontend tasks only, unless -dcr)
      ↓
Git commit on agent branch
      ↓
Merge agent branch → dev
      ↓
Post-merge QA (both backend + frontend)
      ↓
Task moved to processed/  ← success
  OR kickback to agent    ← QA failed, merge conflict, or review rejected
  OR moved to failed/     ← max retries exceeded
```

---

## Recovering from Failures

### Check what failed

```bash
ls ai_team/messages/failed/
```

Failed tasks have a `.json` file (the original task) and a `.md` sidecar (the failure reason).

### Re-queue a failed task

Copy the `.json` back to the appropriate inbox:
```bash
cp ai_team/messages/failed/task_NNNN.json ai_team/messages/inbox_claude/
```

Or edit the task and re-send it with `task.py`.

### Hard failure (max retries exceeded)

The orchestrator creates a `PAUSED` sentinel automatically when it detects repeated failures or API auth errors. Check `TEAM_STATUS.md` for the reason, fix the underlying issue (quota, auth, code error), then:
```bash
rm ai_team/messages/PAUSED
```

---

## Swapping an Agent

To replace Claude with DeepSeek (or any other model):

1. Edit `ai_team/agents_config.py`:
   ```python
   AGENT_BACKEND = AgentConfig(
       name          = "deepseek",           # must be unique, lowercase
       role          = "Backend Lead",
       domain        = "backend/",
       branch        = "backend-deepseek",   # rename as needed
       cli_cmd       = ["deepseek", "--model", "deepseek-chat", "-p"],
       qa_cmd        = [...],                # same QA command as before
       model_primary = "deepseek-chat",
   )
   ```

2. Create the inbox directory:
   ```bash
   mkdir ai_team/messages/inbox_deepseek
   ```

3. Create the log file:
   ```bash
   touch ai_team/logs/deepseek.log
   ```

4. Create the git branch:
   ```bash
   git checkout dev && git checkout -b backend-deepseek
   ```

5. Update the agent's `.md` guidance file (rename `CLAUDE.md` → `DEEPSEEK.md` or update the identity header).

6. Set up the new CLI binary for `aidevteam` (auth, PATH, etc.) — see `SETUP.md` for the pattern.

Nothing in `orchestrator.py`, `task.py`, or `discord_bot.py` needs to change.

---

## Common Commands Reference

```bash
# Send a task
python ai_team/task.py "your task"
python ai_team/task.py -c "backend task"
python ai_team/task.py -g "frontend task"
python ai_team/task.py -g "task" -nr        # skip code review
python ai_team/task.py -g "task" -dcr       # skip UI review

# Monitor
tail -f ai_team/logs/orchestrator.log
cat ai_team/TEAM_STATUS.md

# Control
touch ai_team/messages/PAUSED              # pause
rm ai_team/messages/PAUSED                 # resume

# Audit code with Qwen
python ai_team/task.py -a                  # all changed files
python ai_team/task.py -a backend/core/api.py  # specific file
```
