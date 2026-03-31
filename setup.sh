#!/usr/bin/env bash
# ai_team/setup.sh
#
# Sets up all dependencies for the AI orchestrator.
# Run as your primary user (with sudo access) from the project root.
#
# Usage:
#   sudo bash ai_team/setup.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_PARENT="$(dirname "$PROJECT_ROOT")"
INVOKING_USER="${SUDO_USER:-}"

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2; exit 1; }
pass()  { echo -e "  \033[1;32m✔\033[0m  $*"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
fail()  { echo -e "  \033[1;31m✘\033[0m  $*"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

# Run a command as the chosen AI team user
run_as() {
  if [[ "$RUN_USER" == "root" ]]; then
    bash -c "$*"
  else
    sudo -u "$RUN_USER" bash -c "$*"
  fi
}

# ── Root check ────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  die "Run this script with sudo: sudo bash ai_team/setup.sh"
fi

echo
echo "╔══════════════════════════════════════════════════╗"
echo "║        AI Team Setup                             ║"
echo "╚══════════════════════════════════════════════════╝"
echo
info "Project root : $PROJECT_ROOT"
info "Parent dir   : $PROJECT_PARENT"
if [[ -n "$INVOKING_USER" ]]; then
  info "Invoking user: $INVOKING_USER"
else
  warn "Could not detect invoking user (SUDO_USER not set). Run with sudo, not as root directly."
fi
echo

# ── Mode selection ────────────────────────────────────────────────────────────
echo "  Choose which user the AI orchestrator will run as:"
echo
echo "    1) root       — Simpler setup. Orchestrator runs with full"
echo "                    system access. Recommended for single-user"
echo "                    machines or isolated VMs."
echo
echo "    2) aidevteam  — Dedicated service account. AI-written files"
echo "                    are owned separately. Recommended for shared"
echo "                    systems or when you want an isolation layer."
echo
read -rp "  Choice [1/2, default=1]: " _mode_choice
echo

if [[ "$_mode_choice" == "2" ]]; then
  RUN_USER="aidevteam"
  RUN_HOME="/home/aidevteam"
  USE_AIDEVTEAM=true
  info "Mode: aidevteam"
else
  RUN_USER="root"
  RUN_HOME="/root"
  USE_AIDEVTEAM=false
  info "Mode: root"
fi
echo

# ── Step 1: aidevteam user (aidevteam mode only) ──────────────────────────────
if $USE_AIDEVTEAM; then
  info "Step 1: Creating $RUN_USER user..."
  if id "$RUN_USER" &>/dev/null; then
    ok "$RUN_USER already exists, skipping."
  else
    useradd -m -s /bin/bash "$RUN_USER"
    ok "Created user $RUN_USER."
  fi
else
  info "Step 1: Skipped (running as root, no service account needed)."
fi

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  warn "Docker is not installed."
  read -rp "  Install Docker now? [y/N] " _ans
  if [[ "$_ans" =~ ^[Yy]$ ]]; then
    info "Installing Docker via official install script..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    ok "Docker installed and started."
  else
    warn "Skipping Docker. The AI team will not be able to run Docker commands."
  fi
fi
if $USE_AIDEVTEAM && getent group docker &>/dev/null; then
  usermod -aG docker "$RUN_USER"
  ok "Added $RUN_USER to group: docker"
fi

# ── Ollama ────────────────────────────────────────────────────────────────────
if ! command -v ollama &>/dev/null; then
  warn "Ollama is not installed."
  read -rp "  Install Ollama now? [y/N] " _ans
  if [[ "$_ans" =~ ^[Yy]$ ]]; then
    info "Installing Ollama via official install script..."
    curl -fsSL https://ollama.com/install.sh | sh
    ok "Ollama installed."
  else
    warn "Skipping Ollama. The code reviewer will not be available."
  fi
fi
if $USE_AIDEVTEAM && getent group ollama &>/dev/null; then
  usermod -aG ollama "$RUN_USER"
  ok "Added $RUN_USER to group: ollama"
fi

if command -v ollama &>/dev/null; then
  if ! systemctl is-active --quiet ollama 2>/dev/null; then
    info "Starting ollama service..."
    systemctl enable --now ollama
  fi
  if ollama list 2>/dev/null | grep -q "qwen-reviewer"; then
    ok "qwen-reviewer model already present."
  else
    warn "qwen-reviewer model is not downloaded."
    read -rp "  Download qwen-reviewer now? (large download) [y/N] " _ans
    if [[ "$_ans" =~ ^[Yy]$ ]]; then
      info "Pulling qwen-reviewer..."
      ollama pull qwen-reviewer
      ok "qwen-reviewer downloaded."
    else
      warn "Skipping qwen-reviewer. Code review will not be available until it is pulled."
    fi
  fi
fi

# ── npm ───────────────────────────────────────────────────────────────────────
if ! command -v npm &>/dev/null; then
  warn "npm is not installed."
  read -rp "  Install Node.js and npm now? [y/N] " _ans
  if [[ "$_ans" =~ ^[Yy]$ ]]; then
    info "Installing Node.js and npm..."
    if command -v apt &>/dev/null; then
      apt install -y nodejs npm
    elif command -v dnf &>/dev/null; then
      dnf install -y nodejs npm
    elif command -v pacman &>/dev/null; then
      pacman -S --noconfirm nodejs npm
    else
      die "Cannot install npm automatically. Install Node.js from https://nodejs.org then re-run."
    fi
    ok "Node.js and npm installed."
  else
    die "npm is required to install Claude and Gemini. Exiting."
  fi
fi

# ── Claude CLI ────────────────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
  warn "Claude Code not found."
  read -rp "  Install Claude Code globally now? [y/N] " _ans
  if [[ "$_ans" =~ ^[Yy]$ ]]; then
    info "Installing Claude Code globally..."
    npm install -g @anthropic-ai/claude-code
    ok "Claude Code installed."
  else
    die "Claude is required. Exiting."
  fi
fi

# ── Gemini CLI ────────────────────────────────────────────────────────────────
if ! command -v gemini &>/dev/null; then
  warn "Gemini CLI not found."
  read -rp "  Install Gemini CLI now? [y/N] " _ans
  if [[ "$_ans" =~ ^[Yy]$ ]]; then
    info "Installing Gemini CLI..."
    npm install -g @google/gemini-cli
    ok "Gemini CLI installed."
  else
    warn "Skipping Gemini. The frontend agent will not be available."
  fi
fi

# ── Step 2: Project directory permissions ─────────────────────────────────────
info "Step 2: Setting project directory permissions..."
if ! command -v setfacl &>/dev/null; then
  if command -v apt &>/dev/null;      then PKG_CMD="apt install acl"
  elif command -v dnf &>/dev/null;    then PKG_CMD="dnf install acl"
  elif command -v pacman &>/dev/null; then PKG_CMD="pacman -S acl"
  else PKG_CMD="your package manager: install 'acl'"
  fi
  die "setfacl not found. Install it: sudo $PKG_CMD"
fi

# Grant the AI team user rwX on the project root
if $USE_AIDEVTEAM; then
  setfacl -R -m u:${RUN_USER}:rwX "$PROJECT_ROOT"
  setfacl -R -d -m u:${RUN_USER}:rwX "$PROJECT_ROOT"
  ok "ACL: $RUN_USER → rwX on $PROJECT_ROOT"
fi

# Grant the invoking user rwX on the project root and parent so they can
# navigate, edit, and use the same files the AI team works on
if [[ -n "$INVOKING_USER" ]]; then
  setfacl -R -m u:${INVOKING_USER}:rwX "$PROJECT_ROOT"
  setfacl -R -d -m u:${INVOKING_USER}:rwX "$PROJECT_ROOT"
  ok "ACL: $INVOKING_USER → rwX on $PROJECT_ROOT"

  # Parent directory: execute + read so the user can cd into and list the project
  setfacl -m u:${INVOKING_USER}:rx "$PROJECT_PARENT"
  ok "ACL: $INVOKING_USER → rx on $PROJECT_PARENT"
else
  warn "Skipping invoking-user ACL (SUDO_USER not set)."
fi

# setgid on the project root so new files/dirs inherit the owning group,
# keeping group-level access consistent across users
chmod g+s "$PROJECT_ROOT"
ok "setgid set on $PROJECT_ROOT (new files inherit group)"

# ── Step 3: Claude settings ───────────────────────────────────────────────────
info "Step 3: Writing Claude settings for $RUN_USER..."
CLAUDE_EXEC=$(which claude)
mkdir -p "$RUN_HOME/.claude"
cat > "$RUN_HOME/.claude/settings.json" << 'EOF'
{
  "skipDangerousModePermissionPrompt": true,
  "enableAllProjectMcpServers": true
}
EOF
if $USE_AIDEVTEAM; then
  chown -R "$RUN_USER:$RUN_USER" "$RUN_HOME/.claude"
fi
ok "Claude settings written. Binary: $CLAUDE_EXEC"

# ── Step 4: Playwright Chromium ───────────────────────────────────────────────
info "Step 4: Installing Playwright Chromium for $RUN_USER..."
run_as 'npx playwright install chromium' \
  || die "Chromium install failed."
ok "Chromium installed."

info "Fixing chrome_sandbox setuid permissions..."
sandbox_count=0
while IFS= read -r sandbox; do
  chown root:root "$sandbox"
  chmod 4755 "$sandbox"
  ok "Fixed: $sandbox"
  sandbox_count=$((sandbox_count + 1))
done < <(run_as "find $RUN_HOME/.cache/ms-playwright -name chrome_sandbox 2>/dev/null")

if [[ $sandbox_count -eq 0 ]]; then
  warn "No chrome_sandbox files found. Chromium may not have installed correctly."
fi

info "Pre-caching @playwright/mcp for $RUN_USER..."
run_as 'npx @playwright/mcp@latest --version' &>/dev/null \
  && ok "@playwright/mcp pre-cached." \
  || warn "@playwright/mcp pre-cache failed (non-fatal, will download on first use)."

# ── Step 5: Gemini MCP settings ───────────────────────────────────────────────
info "Step 5: Writing Gemini MCP settings for $RUN_USER..."
mkdir -p "$RUN_HOME/.gemini"
cat > "$RUN_HOME/.gemini/settings.json" << 'EOF'
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
if $USE_AIDEVTEAM; then
  chown -R "$RUN_USER:$RUN_USER" "$RUN_HOME/.gemini"
fi
ok "Gemini settings written."

# ── Step 6: Git branches ──────────────────────────────────────────────────────
info "Step 6: Ensuring git branches exist..."
cd "$PROJECT_ROOT"
DEFAULT_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")
for branch in backend-claude frontend-gemini; do
  if git show-ref --quiet "refs/heads/$branch"; then
    ok "Branch already exists: $branch"
  else
    git checkout -b "$branch"
    git checkout "$DEFAULT_BRANCH"
    ok "Created branch: $branch"
  fi
done

# ── Step 7: Python dependencies ───────────────────────────────────────────────
info "Step 7: Installing Python dependencies..."
pip install "discord.py>=2.0" python-dotenv certifi -q
ok "Python dependencies installed."

# ── Tests ─────────────────────────────────────────────────────────────────────
echo
echo "╔══════════════════════════════════════════════════╗"
echo "║        Running post-install tests                ║"
echo "╚══════════════════════════════════════════════════╝"
echo

TESTS_PASSED=0
TESTS_FAILED=0

# User (aidevteam mode only)
if $USE_AIDEVTEAM; then
  id "$RUN_USER" &>/dev/null \
    && pass "$RUN_USER user exists" \
    || fail "$RUN_USER user not found"

  id -nG "$RUN_USER" | grep -qw docker \
    && pass "$RUN_USER is in docker group" \
    || fail "$RUN_USER is NOT in docker group"

  id -nG "$RUN_USER" | grep -qw ollama \
    && pass "$RUN_USER is in ollama group" \
    || fail "$RUN_USER is NOT in ollama group"

  getfacl "$PROJECT_ROOT" 2>/dev/null | grep -q "user:$RUN_USER:rwx" \
    && pass "ACL permissions set on project root" \
    || fail "ACL permissions missing on project root"
fi

# Invoking user access
if [[ -n "$INVOKING_USER" ]]; then
  getfacl "$PROJECT_ROOT" 2>/dev/null | grep -q "user:$INVOKING_USER:rwx" \
    && pass "ACL: $INVOKING_USER has rwx on project root" \
    || fail "ACL: $INVOKING_USER is missing rwx on project root"
fi

# Qwen
command -v ollama &>/dev/null && ollama list 2>/dev/null | grep -q "qwen-reviewer" \
  && pass "qwen-reviewer model is present" \
  || fail "qwen-reviewer model not found (code review unavailable)"

# Claude
[[ -x "$CLAUDE_EXEC" ]] \
  && pass "Claude binary exists and is executable ($CLAUDE_EXEC)" \
  || fail "Claude binary missing or not executable ($CLAUDE_EXEC)"

run_as "$CLAUDE_EXEC --version" &>/dev/null \
  && pass "Claude runs as $RUN_USER" \
  || fail "Claude failed to run as $RUN_USER"

[[ -f "$RUN_HOME/.claude/settings.json" ]] \
  && pass "Claude settings.json exists" \
  || fail "Claude settings.json missing"

# Gemini
run_as 'which gemini' &>/dev/null \
  && pass "Gemini CLI is accessible to $RUN_USER" \
  || fail "Gemini CLI not found for $RUN_USER"

[[ -f "$RUN_HOME/.gemini/settings.json" ]] \
  && pass "Gemini settings.json exists" \
  || fail "Gemini settings.json missing"

python3 -c "import json; json.load(open('$RUN_HOME/.gemini/settings.json'))" 2>/dev/null \
  && pass "Gemini settings.json is valid JSON" \
  || fail "Gemini settings.json contains invalid JSON"

# Playwright / Chromium
while IFS= read -r sandbox; do
  owner=$(stat -c '%U' "$sandbox")
  perms=$(stat -c '%a' "$sandbox")
  if [[ "$owner" == "root" && "$perms" == "4755" ]]; then
    pass "chrome_sandbox setuid OK: $sandbox"
  else
    fail "chrome_sandbox wrong perms (owner=$owner perms=$perms): $sandbox"
  fi
done < <(run_as "find $RUN_HOME/.cache/ms-playwright -name chrome_sandbox 2>/dev/null")

run_as 'npx @playwright/mcp@latest --version' &>/dev/null \
  && pass "@playwright/mcp package is cached" \
  || fail "@playwright/mcp package not cached (will download on first use)"

# Git branches
for branch in backend-claude frontend-gemini; do
  git -C "$PROJECT_ROOT" show-ref --quiet "refs/heads/$branch" \
    && pass "Git branch exists: $branch" \
    || fail "Git branch missing: $branch"
done

# Python dependencies
for pkg in discord dotenv certifi; do
  python3 -c "import $pkg" 2>/dev/null \
    && pass "Python package available: $pkg" \
    || fail "Python package missing: $pkg"
done

# Summary
echo
echo "──────────────────────────────────────────────────"
echo -e "  Results: \033[1;32m$TESTS_PASSED passed\033[0m  \033[1;31m$TESTS_FAILED failed\033[0m"
echo "──────────────────────────────────────────────────"
echo
if [[ $TESTS_FAILED -gt 0 ]]; then
  warn "$TESTS_FAILED test(s) failed. Review the output above before continuing."
else
  ok "All tests passed."
fi

# ── Auth instructions ─────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════════════════"
echo "  ACTION REQUIRED — OAuth authentication"
echo "══════════════════════════════════════════════════════════"
echo
echo "  Claude and Gemini must each be authenticated once"
echo "  interactively. This cannot be automated."
echo

if $USE_AIDEVTEAM; then
  echo "  You must authenticate as $RUN_USER — NOT as root and"
  echo "  NOT as yourself. Open a NEW terminal and run:"
  echo
  echo "    sudo -su $RUN_USER"
  echo
  echo "  Then inside that $RUN_USER session:"
  echo
  echo "    Step 1 — Claude (a browser window will open):"
  echo "    $CLAUDE_EXEC"
  echo
  echo "    Step 2 — Gemini (a browser window will open):"
  echo "    gemini"
  echo
  echo "    Step 3 — Return to your user:"
  echo "    exit"
  echo
  echo "  Once both logins are complete, come back here and"
  echo "  press ENTER to run the live Playwright tests."
else
  echo "  You are already running as root. Open a NEW terminal"
  echo "  as root (or use this one after the script exits) and run:"
  echo
  echo "    Step 1 — Claude (a browser window will open):"
  echo "    $CLAUDE_EXEC"
  echo
  echo "    Step 2 — Gemini (a browser window will open):"
  echo "    gemini"
  echo
  echo "  Once both logins are complete, come back here and"
  echo "  press ENTER to run the live Playwright tests."
fi

echo
echo "══════════════════════════════════════════════════════════"
echo
read -rp "  Press ENTER when both logins are complete (or Ctrl+C to exit)..."
echo

# ── Live agent tests ──────────────────────────────────────────────────────────
read -rp "  Run live Claude + Gemini Playwright tests now? [y/N] " _ans
if [[ "$_ans" =~ ^[Yy]$ ]]; then
  echo
  echo "╔══════════════════════════════════════════════════╗"
  echo "║        Live agent + Playwright tests             ║"
  echo "╚══════════════════════════════════════════════════╝"
  echo

  if curl -sf --max-time 2 http://localhost:5173 &>/dev/null; then
    TEST_URL="http://localhost:5173"
    info "Dev server detected — testing against $TEST_URL"
  else
    TEST_URL="https://example.com"
    warn "Dev server not running — testing against $TEST_URL instead"
  fi

  PLAYWRIGHT_PROMPT="Use your playwright MCP browser tool to navigate to $TEST_URL and tell me the page title. Reply with only the page title, nothing else."

  info "Testing Claude + Playwright MCP..."
  CLAUDE_RESULT=$(run_as "cd $PROJECT_ROOT && \
    $CLAUDE_EXEC --model claude-sonnet-4-6 --dangerously-skip-permissions \
    -p \"$PLAYWRIGHT_PROMPT\"" 2>/dev/null)
  if echo "$CLAUDE_RESULT" | grep -qiv "error\|failed\|unable\|cannot\|don't have"; then
    pass "Claude Playwright test — got: $(echo "$CLAUDE_RESULT" | tail -1)"
  else
    fail "Claude Playwright test — response: $(echo "$CLAUDE_RESULT" | tail -1)"
  fi

  info "Testing Gemini + Playwright MCP..."
  GEMINI_RESULT=$(run_as "cd $PROJECT_ROOT && \
    gemini -y -m gemini-2.5-pro \
    -p \"$PLAYWRIGHT_PROMPT\"" 2>/dev/null)
  if echo "$GEMINI_RESULT" | grep -qiv "error\|failed\|unable\|cannot\|don't have"; then
    pass "Gemini Playwright test — got: $(echo "$GEMINI_RESULT" | tail -1)"
  else
    fail "Gemini Playwright test — response: $(echo "$GEMINI_RESULT" | tail -1)"
  fi

  echo
  echo "──────────────────────────────────────────────────"
  echo -e "  Final results: \033[1;32m$TESTS_PASSED passed\033[0m  \033[1;31m$TESTS_FAILED failed\033[0m"
  echo "──────────────────────────────────────────────────"
  echo
  if [[ $TESTS_FAILED -gt 0 ]]; then
    warn "$TESTS_FAILED test(s) failed. Review the output above."
  else
    ok "All tests passed. The AI team is fully operational."
  fi
else
  echo
  info "Skipping live agent tests. Run them manually when ready:"
  echo
  if $USE_AIDEVTEAM; then
    echo "    sudo -su $RUN_USER bash -c 'cd $PROJECT_ROOT && \\"
  else
    echo "    bash -c 'cd $PROJECT_ROOT && \\"
  fi
  echo "      $CLAUDE_EXEC --model claude-sonnet-4-6 --dangerously-skip-permissions \\"
  echo "      -p \"Use your playwright MCP browser tool to navigate to http://localhost:5173 and tell me the page title.\"'"
  echo
  if $USE_AIDEVTEAM; then
    echo "    sudo -su $RUN_USER bash -c 'cd $PROJECT_ROOT && \\"
  else
    echo "    bash -c 'cd $PROJECT_ROOT && \\"
  fi
  echo "      gemini -y -m gemini-2.5-pro \\"
  echo "      -p \"Use your playwright MCP browser tool to navigate to http://localhost:5173 and tell me the page title.\"'"
  echo
fi

echo
info "To start the orchestrator:"
if $USE_AIDEVTEAM; then
  echo "    sudo -u $RUN_USER bash -c 'cd $PROJECT_ROOT && python ai_team/orchestrator.py'"
else
  echo "    cd $PROJECT_ROOT && python ai_team/orchestrator.py"
fi
echo
