# Orchestrator Recovery Reference

Quick reference for diagnosing and recovering from the most common failure states. For each scenario: confirm the diagnosis, run the fix, then verify.

---

## 1. Orchestrator is paused (`PAUSED` sentinel exists)

**Symptom:** TEAM_STATUS.md shows `⏸ ORCHESTRATOR PAUSED`. No tasks are processing.

**Cause:** A task hit `MAX_TASK_RETRIES`, a hard parse failure, or an empty agent response. The orchestrator halts until a human clears the sentinel.

**Fix:**

```bash
# Inspect why it paused
cat ai_team/messages/PAUSED

# Check the failed task
ls ai_team/messages/failed/

# Once you understand the failure and have addressed it:
rm ai_team/messages/PAUSED
```

---

## 2. Post-merge QA failure on `dev`

**Symptom:** `ai_team/messages/outbox_human/` contains a `merge_qa_failure_*.json` file. The orchestrator has already reverted the bad merge on `dev`.

**Cause:** An agent's changes passed per-branch QA but broke the integration on `dev` (typically a merge artifact — duplicate declarations, broken arrow function syntax, missing helper function).

**Diagnosis:**

```bash
# See what the lint errors are
docker compose -f docker-compose.dev.yml exec -T frontend npm run lint

# See what the backend errors are
docker compose -f docker-compose.dev.yml exec -T backend python manage.py check

# Confirm dev is clean (the merge was already reverted)
git log --oneline dev -5
```

**Fix:** The orchestrator has already kicked the task back to the agent. The agent will receive the error and resubmit. If the agent is not running, queue a repair task:

```bash
python ai_team/task.py -c "Fix post-merge QA failure: <paste error here>"
# or
python ai_team/task.py -g "Fix post-merge QA failure: <paste error here>"
```

If you need to fix it manually, edit the files on the agent branch, then let the orchestrator re-run its merge cycle naturally.

---

## 3. Merge conflict between agent branch and `dev`

**Symptom:** Orchestrator log shows `💥 MERGE CONFLICT`. A kickback task was created for the agent.

**Cause:** Both agent branches edited the same file in the same region. This should be rare given domain separation (`backend/` vs `frontend/`), but can happen on shared root-level files.

**Manual resolution:**

```bash
git checkout dev
git merge --no-ff backend-claude   # or frontend-gemini

# Resolve conflicts in your editor
git add <conflicted files>
git commit -m "Manual: resolve merge conflict"

# Verify
docker compose -f docker-compose.dev.yml exec -T backend python manage.py check
docker compose -f docker-compose.dev.yml exec -T frontend npm run lint
```

---

## 4. Agent branch has diverged badly from `dev`

**Symptom:** Repeated merge conflicts or an agent producing changes that reference stale state.

**Fix:** Reset the agent branch to current `dev` and let the orchestrator re-run the task from a clean slate:

```bash
git checkout backend-claude   # or frontend-gemini
git reset --hard dev
git checkout dev
```

The orchestrator will recreate the branch from `dev` on the next task run.

---

## 5. `dev` has bad commits that need to be removed

**Symptom:** `dev` QA fails even with no agent activity, or a human merge introduced broken code.

**Fix:** Find the last good commit and reset:

```bash
git log --oneline dev -10   # find the last good SHA

git checkout dev
git reset --hard <last-good-sha>
```

If `dev` is already pushed to a remote:

```bash
git push origin dev --force-with-lease
```

---

## 6. Task stuck in an agent inbox with no response

**Symptom:** A `.json` file has been sitting in `ai_team/messages/inbox_claude/` or `inbox_gemini/` for a long time.

**Check:**

```bash
# Is the orchestrator running?
pgrep -f orchestrator.py

# Is the agent on cooldown?
grep "PAUSING\|COOLDOWN\|rate limit" ai_team/logs/orchestrator.log | tail -5
```

**Fix:** If the orchestrator is running and the agent is on cooldown, wait out the cooldown period (shown in TEAM_STATUS.md). If the task file is corrupt:

```bash
cat ai_team/messages/inbox_claude/<task>.json   # inspect it
# If unreadable, move to failed manually
mv ai_team/messages/inbox_claude/<task>.json ai_team/messages/failed/
```

---

## 7. Promoting `dev` → `main`

Only do this when `dev` is fully stable and represents a clean, tested progression.

```bash
# Final verification before promoting
docker compose -f docker-compose.dev.yml exec -T backend python manage.py check
docker compose -f docker-compose.dev.yml exec -T frontend npm run lint

# Promote
git checkout main
git merge --no-ff dev -m "Release: <description of what this release includes>"
git push origin main   # if using a remote
```

**Do not merge if either QA check fails.** Fix on `dev` first.

---

## 8. After `claude update` — re-copy the binary

The Claude binary is a symlink that changes on update. Re-copy to `aidevteam` after every update:

```bash
sudo cp -L ~/.local/bin/claude /home/aidevteam/.local/bin/claude
sudo chown aidevteam:aidevteam /home/aidevteam/.local/bin/claude
sudo -u aidevteam /home/aidevteam/.local/bin/claude --version   # verify
```

---

## 9. Permission errors — AI can't write files

**Symptom:** TEAM_STATUS.md shows `🛑 PERMISSIONS ERROR`.

**Fix:**

```bash
sudo setfacl -R -m u:aidevteam:rwX /srv/aidev/flashquest
sudo setfacl -R -d -m u:aidevteam:rwX /srv/aidev/flashquest
```

---

## 10. Full reset — start orchestrator from scratch

Use only if the message queues or branch state are completely tangled.

```bash
# Stop the orchestrator (Ctrl+C or kill)

# Clear all queues
rm -f ai_team/messages/inbox_claude/*.json
rm -f ai_team/messages/inbox_gemini/*.json
rm -f ai_team/messages/inbox_tasks/*.json
rm -f ai_team/messages/PAUSED

# Reset both agent branches to current dev
git checkout backend-claude && git reset --hard dev
git checkout frontend-gemini && git reset --hard dev
git checkout dev

# Restart
sudo -u aidevteam bash -c 'cd /srv/aidev/flashquest && python ai_team/orchestrator.py'
```
