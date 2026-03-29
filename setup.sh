#!/usr/bin/env bash
# ai_team/setup.sh
#
# Sets up the aidevteam user and all dependencies for the AI orchestrator.
# Run as your primary user (with sudo access) from the project root.
#
# Usage:
#   sudo bash ai_team/setup.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PRIMARY_USER="${SUDO_USER:-$(whoami)}"
PRIMARY_HOME=$(getent passwd "$PRIMARY_USER" | cut -d: -f6)
AIDEV_USER="aidevteam"
AIDEV_HOME="/home/$AIDEV_USER"
CLAUDE_DEST="$AIDEV_HOME/.local/bin/claude"

# Resolve claude binary — check user-local path first, then fall back to system PATH
CLAUDE_SRC=$(sudo -u "$PRIMARY_USER" bash -c 'which claude 2>/dev/null' || which claude 2>/dev/null || echo "$PRIMARY_HOME/.local/bin/claude")

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2; exit 1; }

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
info "Primary user : $PRIMARY_USER"
echo

# ── Step 1: Create aidevteam user ─────────────────────────────────────────────
info "Step 1: Creating $AIDEV_USER user..."
if id "$AIDEV_USER" &>/dev/null; then
  ok "$AIDEV_USER already exists, skipping."
else
  useradd -m -s /bin/bash "$AIDEV_USER"
  ok "Created user $AIDEV_USER."
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
    warn "Skipping Docker install. The AI team will not be able to run Docker commands."
  fi
fi
if getent group docker &>/dev/null; then
  usermod -aG docker "$AIDEV_USER"
  ok "Added $AIDEV_USER to group: docker"
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
    warn "Skipping Ollama install. The code reviewer will not be available."
  fi
fi
if getent group ollama &>/dev/null; then
  usermod -aG ollama "$AIDEV_USER"
  ok "Added $AIDEV_USER to group: ollama"
fi

if command -v ollama &>/dev/null; then
  # Ensure ollama service is running before pulling
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
      die "Cannot install npm automatically on this system. Install Node.js manually from https://nodejs.org then re-run."
    fi
    ok "Node.js and npm installed."
  else
    die "npm is required to install Claude and Gemini. Exiting."
  fi
fi

# ── Claude CLI ────────────────────────────────────────────────────────────────
if [[ ! -f "$CLAUDE_SRC" ]]; then
  warn "Claude Code not found at $CLAUDE_SRC."
  read -rp "  Install Claude Code for $PRIMARY_USER now? [y/N] " _ans
  if [[ "$_ans" =~ ^[Yy]$ ]]; then
    info "Installing Claude Code..."
    sudo -u "$PRIMARY_USER" bash -c 'npm install -g @anthropic-ai/claude-code'
    ok "Claude Code installed."
  else
    die "Claude binary is required. Exiting."
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
    warn "Skipping Gemini install. The frontend agent will not be available."
  fi
fi

# ── Step 2: Project directory permissions ─────────────────────────────────────
info "Step 2: Setting project directory ACL permissions..."
if ! command -v setfacl &>/dev/null; then
  if command -v apt &>/dev/null;    then PKG_CMD="apt install acl"
  elif command -v dnf &>/dev/null;  then PKG_CMD="dnf install acl"
  elif command -v pacman &>/dev/null; then PKG_CMD="pacman -S acl"
  else PKG_CMD="your package manager: install 'acl'"
  fi
  die "setfacl not found. Install it: sudo $PKG_CMD"
fi
setfacl -R -m u:${AIDEV_USER}:rwX "$PROJECT_ROOT"
setfacl -R -d -m u:${AIDEV_USER}:rwX "$PROJECT_ROOT"
ok "ACL permissions set on $PROJECT_ROOT"

# ── Step 3: Claude binary ─────────────────────────────────────────────────────
info "Step 3: Installing Claude binary for $AIDEV_USER..."
if [[ ! -f "$CLAUDE_SRC" ]]; then
  die "Claude binary still not found at $CLAUDE_SRC. Something went wrong with the install."
fi

mkdir -p "$AIDEV_HOME/.local/bin"
# -L dereferences the symlink so aidevteam gets the actual binary, not a broken symlink
cp -L "$CLAUDE_SRC" "$CLAUDE_DEST"
chown "$AIDEV_USER:$AIDEV_USER" "$CLAUDE_DEST"
ok "Claude binary copied to $CLAUDE_DEST"

if ! grep -q '.local/bin' "$AIDEV_HOME/.bashrc" 2>/dev/null; then
  echo 'export PATH=$PATH:/home/aidevteam/.local/bin' >> "$AIDEV_HOME/.bashrc"
  ok "Added .local/bin to $AIDEV_USER PATH"
fi

mkdir -p "$AIDEV_HOME/.claude"
cat > "$AIDEV_HOME/.claude/settings.json" << 'EOF'
{
  "skipDangerousModePermissionPrompt": true,
  "enableAllProjectMcpServers": true
}
EOF
chown -R "$AIDEV_USER:$AIDEV_USER" "$AIDEV_HOME/.claude"
ok "Claude settings written."

# ── Step 4: Playwright Chromium for aidevteam ─────────────────────────────────
info "Step 4: Installing Playwright Chromium for $AIDEV_USER..."
# Install without --with-deps (system libs are already present from the primary user's install)
sudo -u "$AIDEV_USER" bash -c 'npx playwright install chromium' \
  || die "Chromium install failed. Check that npx is available for $AIDEV_USER."
ok "Chromium installed."

# chrome_sandbox must be owned by root with setuid bit or Chromium will refuse to launch
info "Fixing chrome_sandbox setuid permissions..."
sandbox_count=0
while IFS= read -r sandbox; do
  chown root:root "$sandbox"
  chmod 4755 "$sandbox"
  ok "Fixed: $sandbox"
  ((sandbox_count++))
done < <(sudo -u "$AIDEV_USER" find "$AIDEV_HOME/.cache/ms-playwright" -name chrome_sandbox 2>/dev/null)

if [[ $sandbox_count -eq 0 ]]; then
  warn "No chrome_sandbox files found. Chromium may not have installed correctly."
fi

# Pre-cache the @playwright/mcp package so the first agent task doesn't hang
info "Pre-caching @playwright/mcp for $AIDEV_USER..."
sudo -u "$AIDEV_USER" bash -c 'npx @playwright/mcp@latest --version' &>/dev/null \
  && ok "@playwright/mcp pre-cached." \
  || warn "@playwright/mcp pre-cache failed (non-fatal, will download on first use)."

# ── Step 5: Gemini MCP settings ───────────────────────────────────────────────
info "Step 5: Writing Gemini MCP settings for $AIDEV_USER..."
mkdir -p "$AIDEV_HOME/.gemini"
cat > "$AIDEV_HOME/.gemini/settings.json" << 'EOF'
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
chown -R "$AIDEV_USER:$AIDEV_USER" "$AIDEV_HOME/.gemini"
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

pass() { echo -e "  \033[1;32m✔\033[0m  $*"; ((TESTS_PASSED++)); }
fail() { echo -e "  \033[1;31m✘\033[0m  $*"; ((TESTS_FAILED++)); }

# User and groups
id "$AIDEV_USER" &>/dev/null \
  && pass "aidevteam user exists" \
  || fail "aidevteam user not found"

id -nG "$AIDEV_USER" | grep -qw docker \
  && pass "aidevteam is in docker group" \
  || fail "aidevteam is NOT in docker group"

id -nG "$AIDEV_USER" | grep -qw ollama \
  && pass "aidevteam is in ollama group" \
  || fail "aidevteam is NOT in ollama group"

command -v ollama &>/dev/null && ollama list 2>/dev/null | grep -q "qwen-reviewer" \
  && pass "qwen-reviewer model is present" \
  || fail "qwen-reviewer model not found (code review unavailable)"

# ACL permissions
getfacl "$PROJECT_ROOT" 2>/dev/null | grep -q "user:aidevteam:rwx" \
  && pass "ACL permissions set on project root" \
  || fail "ACL permissions missing on project root"

# Claude
[[ -x "$CLAUDE_DEST" ]] \
  && pass "Claude binary exists and is executable ($CLAUDE_DEST)" \
  || fail "Claude binary missing or not executable ($CLAUDE_DEST)"

sudo -u "$AIDEV_USER" "$CLAUDE_DEST" --version &>/dev/null \
  && pass "Claude runs as aidevteam" \
  || fail "Claude failed to run as aidevteam"

[[ -f "$AIDEV_HOME/.claude/settings.json" ]] \
  && pass "Claude settings.json exists" \
  || fail "Claude settings.json missing"

# Gemini
sudo -u "$AIDEV_USER" bash -c 'which gemini' &>/dev/null \
  && pass "Gemini CLI is in aidevteam PATH" \
  || fail "Gemini CLI not found in aidevteam PATH"

[[ -f "$AIDEV_HOME/.gemini/settings.json" ]] \
  && pass "Gemini settings.json exists" \
  || fail "Gemini settings.json missing"

python3 -c "import json; json.load(open('$AIDEV_HOME/.gemini/settings.json'))" 2>/dev/null \
  && pass "Gemini settings.json is valid JSON" \
  || fail "Gemini settings.json contains invalid JSON"

# Playwright / Chromium
sandbox_ok=true
while IFS= read -r sandbox; do
  owner=$(stat -c '%U' "$sandbox")
  perms=$(stat -c '%a' "$sandbox")
  if [[ "$owner" == "root" && "$perms" == "4755" ]]; then
    pass "chrome_sandbox setuid OK: $sandbox"
  else
    fail "chrome_sandbox wrong perms (owner=$owner perms=$perms): $sandbox"
    sandbox_ok=false
  fi
done < <(sudo -u "$AIDEV_USER" find "$AIDEV_HOME/.cache/ms-playwright" -name chrome_sandbox 2>/dev/null)
$sandbox_ok || fail "One or more chrome_sandbox binaries have incorrect permissions"

sudo -u "$AIDEV_USER" bash -c 'npx @playwright/mcp@latest --version' &>/dev/null \
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

# ── Done — manual auth steps ──────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════════════════════"
echo "  ACTION REQUIRED — OAuth authentication"
echo "══════════════════════════════════════════════════════════"
echo
echo "  Claude and Gemini must each be authenticated once"
echo "  interactively. This cannot be automated."
echo
echo "  You must do this as the aidevteam user — NOT as root"
echo "  and NOT as yourself. Open a NEW terminal and run:"
echo
echo "    sudo -su aidevteam"
echo
echo "  Then inside that aidevteam session, authenticate each CLI:"
echo
echo "    Step 1 — Claude (a browser window will open):"
echo "    $CLAUDE_DEST"
echo
echo "    Step 2 — Gemini (a browser window will open):"
echo "    gemini"
echo
echo "    Step 3 — Return to your user:"
echo "    exit"
echo
echo "  Once both logins are complete, come back here and"
echo "  press ENTER to run the live Playwright tests."
echo
echo "  NOTE: Re-run after every 'claude update':"
echo "    sudo cp -L $CLAUDE_SRC $CLAUDE_DEST"
echo "    sudo chown $AIDEV_USER:$AIDEV_USER $CLAUDE_DEST"
echo
echo "══════════════════════════════════════════════════════════"
echo
read -rp "  Press ENTER when both logins are complete (or Ctrl+C to exit)..."
echo

# ── Live agent tests ───────────────────────────────────────────────────────────
read -rp "  Run live Claude + Gemini Playwright tests now? [y/N] " _ans
if [[ "$_ans" =~ ^[Yy]$ ]]; then
  echo
  echo "╔══════════════════════════════════════════════════╗"
  echo "║        Live agent + Playwright tests             ║"
  echo "╚══════════════════════════════════════════════════╝"
  echo

  # Determine test URL — use localhost:5173 if the dev server is up, else example.com
  if curl -sf --max-time 2 http://localhost:5173 &>/dev/null; then
    TEST_URL="http://localhost:5173"
    info "Dev server detected — testing against $TEST_URL"
  else
    TEST_URL="https://example.com"
    warn "Dev server not running — testing against $TEST_URL instead"
  fi

  PLAYWRIGHT_PROMPT="Use your playwright MCP browser tool to navigate to $TEST_URL and tell me the page title. Reply with only the page title, nothing else."

  # Claude agent test
  info "Testing Claude + Playwright MCP..."
  CLAUDE_RESULT=$(sudo -u "$AIDEV_USER" bash -c "cd $PROJECT_ROOT && \
    $CLAUDE_DEST --model claude-sonnet-4-6 --dangerously-skip-permissions \
    -p \"$PLAYWRIGHT_PROMPT\"" 2>/dev/null)
  if echo "$CLAUDE_RESULT" | grep -qiv "error\|failed\|unable\|cannot\|don't have"; then
    pass "Claude Playwright test — got: $(echo "$CLAUDE_RESULT" | tail -1)"
  else
    fail "Claude Playwright test — response: $(echo "$CLAUDE_RESULT" | tail -1)"
  fi

  # Gemini agent test
  info "Testing Gemini + Playwright MCP..."
  GEMINI_RESULT=$(sudo -u "$AIDEV_USER" bash -c "cd $PROJECT_ROOT && \
    gemini -y -m gemini-2.5-pro \
    -p \"$PLAYWRIGHT_PROMPT\"" 2>/dev/null)
  if echo "$GEMINI_RESULT" | grep -qiv "error\|failed\|unable\|cannot\|don't have"; then
    pass "Gemini Playwright test — got: $(echo "$GEMINI_RESULT" | tail -1)"
  else
    fail "Gemini Playwright test — response: $(echo "$GEMINI_RESULT" | tail -1)"
  fi

  # Final summary
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
  echo "    sudo -su aidevteam bash -c 'cd $PROJECT_ROOT && \\"
  echo "      $CLAUDE_DEST --model claude-sonnet-4-6 --dangerously-skip-permissions \\"
  echo "      -p \"Use your playwright MCP browser tool to navigate to http://localhost:5173 and tell me the page title.\"'"
  echo
  echo "    sudo -su aidevteam bash -c 'cd $PROJECT_ROOT && \\"
  echo "      gemini -y -m gemini-2.5-pro \\"
  echo "      -p \"Use your playwright MCP browser tool to navigate to http://localhost:5173 and tell me the page title.\"'"
  echo
fi
