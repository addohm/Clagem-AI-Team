#!/usr/bin/env bash
# ai_team/setup.sh
#
# Sets up all dependencies for the AI orchestrator.
# Run as your primary user (with sudo access) from the project root.
#
# Usage:
#   sudo bash ai_team/setup.sh              # interactive
#   sudo bash ai_team/setup.sh --express    # install everything, no prompts
#   sudo bash ai_team/setup.sh -y           # same as --express
#   sudo bash ai_team/setup.sh --verbose    # show full command output on screen (always logged)

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_PARENT="$(dirname "$PROJECT_ROOT")"
INVOKING_USER="${SUDO_USER:-}"
mkdir -p "$SCRIPT_DIR/logs"
SETUP_LOG="$SCRIPT_DIR/logs/setup_$(date +%Y%m%d_%H%M%S).log"

# ── Argument parsing ──────────────────────────────────────────────────────────
EXPRESS=false
VERBOSE=false
for _arg in "$@"; do
    case "$_arg" in
        --express|-y) EXPRESS=true ;;
        --verbose|-v) VERBOSE=true ;;
    esac
done

# ── Logging setup ─────────────────────────────────────────────────────────────
# Always write full output to the log file.
# In verbose mode, also stream everything to the terminal.
# In normal mode, only the curated info/ok/warn/fail messages appear on screen.
echo "[setup.sh started at $(date)]" > "$SETUP_LOG"
if $VERBOSE; then
    exec > >(tee -a "$SETUP_LOG") 2>&1
    info "Verbose mode — full output shown on screen and logged to: $SETUP_LOG"
else
    # Redirect fd 3 to the log; commands that should be quiet redirect there.
    exec 3>>"$SETUP_LOG"
    # Capture stderr to log only (not terminal) unless it's our own die/warn calls.
    exec 2>>"$SETUP_LOG"
    info "Logging to: $SETUP_LOG (run with --verbose to see full output)"
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2; exit 1; }
pass()  { echo -e "  \033[1;32m✔\033[0m  $*"; TESTS_PASSED=$((TESTS_PASSED + 1)); }
fail()  { echo -e "  \033[1;31m✘\033[0m  $*"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

# Prompt yes/no — loops until exactly y/Y/n/N or empty (default N) is entered.
# In express mode, always returns yes without prompting.
# Usage: prompt_yn "Question text" && <do if yes>
prompt_yn() {
    local _q="$1" _r
    if $EXPRESS; then
        info "$_q → yes (express)"
        return 0
    fi
    while true; do
        read -rp "  $_q [y/N] " _r
        case "$_r" in
            [Yy]) return 0 ;;
            [Nn]|"") return 1 ;;
            *) warn "Please enter y or n." ;;
        esac
    done
}

# Run a command as the chosen AI team user.
# In aidevteam mode the user-level npm bin dir AND the directory containing
# the node binary are prepended to PATH so that npm can find node even when
# node was installed via nvm (which only activates via shell profile).
run_as() {
    if [[ "$RUN_USER" == "root" ]]; then
        bash -c "$*"
    else
        local _path_prefix=""
        local _extra_paths="${NPM_BIN_DIR:-}"
        # If node was installed via nvm its bin dir won't be on the bare shell
        # PATH. Resolve it now (as root, where it is visible) and inject it.
        local _node_dir
        _node_dir=$(dirname "$(command -v node 2>/dev/null)" 2>/dev/null) || true
        [[ -n "$_node_dir" && "$_node_dir" != "." ]] && _extra_paths="${_extra_paths:+${_extra_paths}:}${_node_dir}"
        [[ -n "$_extra_paths" ]] && _path_prefix="export PATH='${_extra_paths}:\$PATH'; "
        sudo -u "$RUN_USER" env NO_UPDATE_NOTIFIER=1 NPM_CONFIG_FUND=false bash -c "${_path_prefix}$*"
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

# Pre-flight checks for the summary table
_chk() { command -v "$1" &>/dev/null && echo "✔" || echo "✘"; }
# Check Python packages in the correct user context (aidevteam's --user prefix
# is invisible to root's Python, so we must check as the target user).
_chk_pkg() {
    if [[ "${USE_AIDEVTEAM:-false}" == "true" ]]; then
        sudo -u "$RUN_USER" python3 -c "import $1" 2>/dev/null && echo "✔" || echo "✘"
    else
        python3 -c "import $1" 2>/dev/null && echo "✔" || echo "✘"
    fi
}

C_npm=$(_chk npm)
C_node=$(_chk node)
C_claude=$(_chk claude)
C_gemini=$(_chk gemini)
C_python=$(_chk python3)
C_pip=$(command -v pip3 &>/dev/null || python3 -m pip --version &>/dev/null 2>&1 && echo "✔" || echo "✘")
C_setfacl=$(_chk setfacl)
C_chromium=$(find /root/.cache/ms-playwright /home/*/.cache/ms-playwright -name "chrome" -type f 2>/dev/null | grep -q . && echo "✔" || echo "✘")
C_docker=$(_chk docker)
C_ollama=$(_chk ollama)
C_qwen=$(command -v ollama &>/dev/null && ollama list 2>/dev/null | grep -q "qwen2.5-coder:14b" && echo "✔" || echo "✘")
C_discord=$(_chk_pkg discord)

_icon() { [[ "$1" == "✔" ]] && echo -e "\033[1;32m✔\033[0m" || echo -e "\033[1;31m✘\033[0m"; }

echo
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │  Pre-flight check                                        │"
echo "  ├───────────────────────────────┬──────────────────────────┤"
echo "  │  REQUIRED                     │  OPTIONAL                │"
echo "  ├───────────────────────────────┼──────────────────────────┤"
printf "  │  [%b] Node.js                  │  [%b] Docker              │\n" "$(_icon $C_node)" "$(_icon $C_docker)"
printf "  │  [%b] npm                      │  [%b] Ollama              │\n" "$(_icon $C_npm)" "$(_icon $C_ollama)"
printf "  │  [%b] Claude Code CLI          │  [%b] qwen2.5-coder:14b   │\n" "$(_icon $C_claude)" "$(_icon $C_qwen)"
printf "  │  [%b] Gemini CLI               │  [%b] Discord bot         │\n" "$(_icon $C_gemini)" "$(_icon $C_discord)"
printf "  │  [%b] Python 3                 │                          │\n" "$(_icon $C_python)"
printf "  │  [%b] pip                      │                          │\n"    "$(_icon $C_pip)"
printf "  │  [%b] setfacl (ACL tools)      │                          │\n"    "$(_icon $C_setfacl)"
printf "  │  [%b] Playwright + Chromium    │                          │\n"    "$(_icon $C_chromium)"
echo "  ├───────────────────────────────┴──────────────────────────┤"
if $EXPRESS; then
echo "  │  Express mode: everything will be installed automatically.│"
else
echo "  │  You will be asked before anything is installed.         │"
echo "  │  Declining a required item will exit the script.         │"
fi
echo "  └──────────────────────────────────────────────────────────┘"
echo

# ── Install mode (interactive only — CLI flag already handled above) ───────────
if ! $EXPRESS; then
    echo "  Install mode:"
    echo "    1) Interactive — you will be asked before each component is installed"
    echo "    2) Express     — install everything needed without further prompts"
    echo
    while true; do
        read -rp "  Choice [1/2, default=1]: " _install_choice
        case "${_install_choice:-1}" in
            1) break ;;
            2) EXPRESS=true; break ;;
            *) warn "Please enter 1 or 2." ;;
        esac
    done
    echo
fi

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
while true; do
    read -rp "  Choice [1/2, default=1]: " _mode_choice
    case "${_mode_choice:-1}" in
        1|2) break ;;
        *) warn "Please enter 1 or 2." ;;
    esac
done
echo

if [[ "${_mode_choice:-1}" == "2" ]]; then
    RUN_USER="aidevteam"
    RUN_HOME="/home/aidevteam"
    USE_AIDEVTEAM=true
    NPM_GLOBAL_PREFIX="$RUN_HOME/.npm-global"
    NPM_BIN_DIR="$NPM_GLOBAL_PREFIX/bin"
    info "Mode: aidevteam"
else
    RUN_USER="root"
    RUN_HOME="/root"
    USE_AIDEVTEAM=false
    NPM_GLOBAL_PREFIX=""   # use system npm default
    NPM_BIN_DIR="/usr/local/bin"
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
    if prompt_yn "Install Docker now?"; then
        info "Installing Docker..."
        if command -v apt &>/dev/null; then
            # Debian / Ubuntu — official GPG + apt repo method
            apt-get update -qq
            apt-get install -y ca-certificates curl gnupg lsb-release
            install -m 0755 -d /etc/apt/keyrings
            curl -fsSL "https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg" \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            chmod a+r /etc/apt/keyrings/docker.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
            $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
            | tee /etc/apt/sources.list.d/docker.list > /dev/null
            apt-get update -qq
            apt-get install -y docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin
            elif command -v dnf &>/dev/null; then
            # Fedora — official dnf repo method
            dnf -y install dnf-plugins-core
            dnf config-manager addrepo \
            --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
            dnf install -y docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin
            elif command -v pacman &>/dev/null; then
            # Arch Linux
            pacman -Sy --noconfirm docker docker-compose
        else
            # Generic fallback
            info "Unknown distro — falling back to get.docker.com install script..."
            curl -fsSL https://get.docker.com | sh
        fi
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
    if prompt_yn "Install Ollama now?"; then
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
    if ollama list 2>/dev/null | grep -q "qwen2.5-coder:14b"; then
        ok "qwen2.5-coder:14b already present."
    else
        warn "qwen2.5-coder:14b is not downloaded."
        if prompt_yn "Download qwen2.5-coder:14b now? (~9 GB download)"; then
            info "Pulling qwen2.5-coder:14b..."
            ollama pull qwen2.5-coder:14b
            ok "qwen2.5-coder:14b ready."
        else
            warn "Skipping qwen2.5-coder:14b. Use --no-review when sending tasks, or touch ai_team/messages/DISABLE_CLAUDE_REVIEW to disable globally."
        fi
    fi
fi

# ── npm ───────────────────────────────────────────────────────────────────────
if ! command -v npm &>/dev/null; then
    warn "npm is not installed."
    if prompt_yn "Install Node.js and npm now?"; then
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

# ── Resolve npm absolute path ─────────────────────────────────────────────────
# run_as executes via "sudo -u user bash -c" which opens a bare shell without
# loading the user's profile. If npm was installed via nvm or a non-standard
# prefix it may not be on the bare shell's PATH. Using the absolute path
# resolved here (as root, where npm is visible) avoids that problem.
NPM_CMD=$(command -v npm)

# ── npm prefix for user-mode installs ────────────────────────────────────────
if $USE_AIDEVTEAM; then
    info "Configuring user-level npm prefix for $RUN_USER..."
    mkdir -p "$NPM_GLOBAL_PREFIX"
    chown -R "$RUN_USER:$RUN_USER" "$NPM_GLOBAL_PREFIX"
    run_as "$NPM_CMD config set prefix '$NPM_GLOBAL_PREFIX'"
    # Add ~/.npm-global/bin to PATH in RUN_USER's shell profile
    for _profile in "$RUN_HOME/.bashrc" "$RUN_HOME/.profile"; do
        grep -q 'npm-global' "$_profile" 2>/dev/null || \
            echo "export PATH=\"\$HOME/.npm-global/bin:\$PATH\"" >> "$_profile"
    done
    ok "npm prefix set to $NPM_GLOBAL_PREFIX"
fi

# ── Claude CLI ────────────────────────────────────────────────────────────────
if ! run_as "command -v claude" &>/dev/null; then
    warn "Claude Code not found."
    if prompt_yn "Install Claude Code now?"; then
        info "Installing Claude Code for $RUN_USER..."
        if $USE_AIDEVTEAM; then
            run_as "$NPM_CMD install -g @anthropic-ai/claude-code"
        else
            "$NPM_CMD" install -g @anthropic-ai/claude-code
        fi
        ok "Claude Code installed."
    else
        die "Claude is required. Exiting."
    fi
fi

# ── Gemini CLI ────────────────────────────────────────────────────────────────
if ! run_as "command -v gemini" &>/dev/null; then
    warn "Gemini CLI not found."
    if prompt_yn "Install Gemini CLI now?"; then
        info "Installing Gemini CLI for $RUN_USER..."
        if $USE_AIDEVTEAM; then
            run_as "$NPM_CMD install -g @google/gemini-cli"
        else
            "$NPM_CMD" install -g @google/gemini-cli
        fi
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

# ── Step 6: Python + pip ──────────────────────────────────────────────────────
info "Step 6: Checking Python and pip..."

if ! command -v python3 &>/dev/null; then
    warn "Python 3 is not installed. It is required to run the orchestrator."
    if prompt_yn "Install Python 3 now?"; then
        if command -v apt &>/dev/null;      then apt install -y python3 python3-pip
            elif command -v dnf &>/dev/null;    then dnf install -y python3 python3-pip
            elif command -v pacman &>/dev/null; then pacman -S --noconfirm python python-pip
        else die "Cannot install Python automatically. Install from https://python.org then re-run."
        fi
        ok "Python 3 installed."
    else
        die "Python 3 is required to run the orchestrator. Exiting."
    fi
fi

if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null 2>&1; then
    warn "pip is not installed."
    if prompt_yn "Install pip now?"; then
        if command -v apt &>/dev/null;      then apt install -y python3-pip
            elif command -v dnf &>/dev/null;    then dnf install -y python3-pip
            elif command -v pacman &>/dev/null; then pacman -S --noconfirm python-pip
        else die "Cannot install pip automatically. Install manually then re-run."
        fi
        ok "pip installed."
    else
        die "pip is required to install Python dependencies. Exiting."
    fi
fi

PIP_CMD=$(command -v pip3 2>/dev/null || echo "python3 -m pip")

# In aidevteam mode: install to the user's home with --user (no system-wide side effects,
# no --break-system-packages needed).
# In root mode on apt-based systems: pip refuses system-wide installs without
# --break-system-packages; use apt where available, pip with the flag otherwise.
if $USE_AIDEVTEAM; then
    # Ubuntu 23.04+ marks Python as externally managed — pip refuses --user
    # installs without --break-system-packages even for user-local installs.
    # Packages still land in ~/.local (not system-wide) so this is safe.
    if command -v apt &>/dev/null; then
        PIP_EXTRA="--user --break-system-packages"
        info "User mode on apt system — pip packages will be installed under $RUN_HOME/.local"
    else
        PIP_EXTRA="--user"
        info "User mode — pip packages will be installed under $RUN_HOME/.local"
    fi
else
    PIP_EXTRA=""
    if command -v apt &>/dev/null; then
        PIP_EXTRA="--break-system-packages"
        info "Root mode on apt system — pip will use --break-system-packages."
    fi
fi

# pip_install LABEL PACKAGES...
# Runs pip as the correct user, streams output (visible + logged), and dies
# loudly if the install fails rather than silently continuing.
pip_install() {
    local _label="$1"; shift
    info "Installing $_label..."
    local _rc=0
    local _quiet_flag=""; $VERBOSE || _quiet_flag="-q"
    if $USE_AIDEVTEAM; then
        if $VERBOSE; then
            run_as "$PIP_CMD install $* $PIP_EXTRA" || _rc=$?
        else
            run_as "$PIP_CMD install $_quiet_flag $* $PIP_EXTRA" >>"$SETUP_LOG" 2>&1 || _rc=$?
        fi
    elif command -v apt &>/dev/null && [[ "$*" == *"python-dotenv"* || "$*" == *"certifi"* ]]; then
        apt install -y python3-dotenv python3-certifi >>"$SETUP_LOG" 2>&1 || _rc=$?
    else
        if $VERBOSE; then
            $PIP_CMD install $* $PIP_EXTRA || _rc=$?
        else
            $PIP_CMD install $_quiet_flag $* $PIP_EXTRA >>"$SETUP_LOG" 2>&1 || _rc=$?
        fi
    fi
    if [[ $_rc -ne 0 ]]; then
        warn "$_label install exited with code $_rc — see $SETUP_LOG for details."
        return $_rc
    fi
    ok "$_label installed."
}

pip_install "core Python dependencies (python-dotenv certifi)" python-dotenv certifi

# ── Discord bot (optional) ────────────────────────────────────────────────────
WANT_DISCORD=false
echo
if prompt_yn "Install Discord bot support? (optional, enables live monitoring)"; then
    WANT_DISCORD=true
    pip_install "discord.py" "discord.py>=2.0"
else
    info "Skipping Discord bot. The orchestrator will run without it."
fi

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
command -v ollama &>/dev/null && ollama list 2>/dev/null | grep -q "qwen2.5-coder:14b" \
&& pass "qwen2.5-coder:14b model is present" \
|| fail "qwen2.5-coder:14b not found — use --no-review or touch ai_team/messages/DISABLE_CLAUDE_REVIEW"

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

# Python
command -v python3 &>/dev/null \
&& pass "python3 is available ($(python3 --version 2>&1))" \
|| fail "python3 not found — orchestrator cannot run"

for pkg in dotenv certifi; do
    run_as "python3 -c 'import $pkg'" 2>/dev/null \
    && pass "Python package available: $pkg" \
    || fail "Python package missing: $pkg"
done

if $WANT_DISCORD; then
    run_as "python3 -c 'import discord'" 2>/dev/null \
    && pass "Python package available: discord" \
    || fail "Python package missing: discord (Discord bot will not work)"
fi

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
if prompt_yn "Run live Claude + Gemini Playwright tests now?"; then
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
    echo "    sudo -u $RUN_USER bash -c 'cd $PROJECT_ROOT && python3 ai_team/orchestrator.py'"
else
    echo "    cd $PROJECT_ROOT && python3 ai_team/orchestrator.py"
fi
echo
if $WANT_DISCORD; then
    echo "══════════════════════════════════════════════════════════"
    echo "  Discord bot requires ai_team/.env to be configured."
    echo "  A template has been provided:"
    echo
    echo "    cp $PROJECT_ROOT/ai_team/.env.example $PROJECT_ROOT/ai_team/.env"
    echo "    nano $PROJECT_ROOT/ai_team/.env"
    echo
    echo "  Required variables:"
    echo "    DISCORD_BOT_TOKEN"
    echo "    DISCORD_LOGS_CHANNEL_ID"
    echo "    DISCORD_TASKS_CHANNEL_ID"
    echo "    DISCORD_TODOS_CHANNEL_ID"
    echo "    DISCORD_STATUS_CHANNEL_ID"
    echo
    echo "  The orchestrator will offer to start the bot on launch"
    echo "  once the .env file is in place."
    echo "══════════════════════════════════════════════════════════"
    echo
fi
