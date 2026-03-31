import json
import subprocess
import time
import os
import shutil
import sys
import pwd
import threading
import zipfile
import re
import fcntl
import argparse
from datetime import datetime
from pathlib import Path

# --- SEAMLESS CO-EDITING FIX ---
os.umask(0o002)  # Ensures all AI-created files have group write permissions

# --- ROBUST PATH RESOLUTION ---
AI_TEAM_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = AI_TEAM_DIR.parent.resolve()
TMP_DIR = PROJECT_ROOT / "tmp"

# Define all inboxes relative to the script's actual home
AI_TEAM_MSG_DIR = AI_TEAM_DIR / "messages"
INBOX_HUMAN = AI_TEAM_MSG_DIR / "inbox_tasks"
INBOX_GEMINI = AI_TEAM_MSG_DIR / "inbox_gemini"
INBOX_CLAUDE = AI_TEAM_MSG_DIR / "inbox_claude"
HUMAN_BOX = AI_TEAM_MSG_DIR / "outbox_human"
TEAM_STATUS_FILE = AI_TEAM_DIR / "TEAM_STATUS.md"
PROCESSED = AI_TEAM_MSG_DIR / "processed"
FAILED = AI_TEAM_MSG_DIR / "failed"
PAUSE_SENTINEL = AI_TEAM_MSG_DIR / "PAUSED"
CLAUDE_REVIEW_DISABLED_SENTINEL = AI_TEAM_MSG_DIR / "DISABLE_CLAUDE_REVIEW"
COUNTER_FILE = AI_TEAM_MSG_DIR / "counter.txt"
COUNTER_LOCK_FILE = AI_TEAM_MSG_DIR / "counter.lock"
DISCORD_BOT_PATH = AI_TEAM_DIR / "discord_bot.py"

# Logging Directory
LOGS_DIR = AI_TEAM_DIR / "logs"

# --- AI MODEL CONFIGURATION ---
# Update these when models change. GEMINI_FALLBACK is used when primary is capacity-exhausted.
CLAUDE_PRIMARY = "opus"
GEMINI_PRIMARY = "gemini-3.1-pro-preview"
GEMINI_FALLBACK = None  # Set to a model ID string for a specific fallback, or None for auto-select
GEMINI_FALLBACK_2 = "gemini-2.5-flash"  # Second fallback tried before triggering the 60-min rate-limit cooldown

# --- SAFETY LIMITS ---
MAX_TASK_RETRIES = 3  # Max QA fix attempts before a task is permanently failed
MAX_BATON_DEPTH = 6  # Max handoff chain length before circuit breaker triggers
AGENT_TIMEOUT = 7200  # Seconds before an AI process is killed (default: 2 hours)

# Ensure directories exist regardless of where you launched the script
for d in [
        AI_TEAM_MSG_DIR, INBOX_HUMAN, INBOX_GEMINI, INBOX_CLAUDE, HUMAN_BOX,
        PROCESSED, FAILED, LOGS_DIR
]:
    d.mkdir(parents=True, exist_ok=True)

# --- RESOLVE NATIVE CLAUDE PATH ---
current_user_home = pwd.getpwuid(os.getuid()).pw_dir
# Resolve claude: prefer PATH lookup, then running user's .local/bin, then addohm fallback
CLAUDE_EXEC = (shutil.which("claude")
               or f"{current_user_home}/.local/bin/claude"
               or "/home/addohm/.local/bin/claude")

# CLI Commands
CLAUDE_CMD = [
    CLAUDE_EXEC, "--model", CLAUDE_PRIMARY, "--dangerously-skip-permissions",
    "-p"
]
GEMINI_CMD = ["gemini", "-y", "-m", GEMINI_PRIMARY, "-p"]
REVIEW_CMD = ["ollama", "run", "qwen-reviewer"]

# Git Branches
BRANCH_MAIN = "main"  # Production branch — only receives merges from dev when fully verified
BRANCH_DEV = "dev"  # Integration branch — agent branches are cut from and merged back into here
BRANCH_CLAUDE = "backend-claude"
BRANCH_GEMINI = "frontend-gemini"

# Security Lock
PROTECTED_PATHS = [
    "ai_team/", ".git/", "_assets/", "_archive/", "_tests/", "GEMINI.md",
    "CLAUDE.md", "DAILY_LOG.md", "OWNER_TODOS.md"
]

DRY_RUN = False
GEMINI_REVIEW_ENABLED = True  # Claude reviews Gemini's UI/UX diff before commit
FULL_REVIEW_THRESHOLD = 10  # Run a full-file Qwen review every N diff reviews per file

# Review counter state file (tracks per-file diff review counts)
REVIEW_STATE_FILE = AI_TEAM_MSG_DIR / "review_state.json"

# --- STATE TRACKERS ---
AGENT_COOLDOWNS = {"claude": 0.0, "gemini": 0.0}
ACTIVE_PROCESS = None  # Tracks the live AI process for the kill switch
CURRENT_TASKS = {
}  # agent_name -> {id, preview, started_at} — drives the live task board
KICKBACK_LOG = []  # Recent kickback events: [{ts, id, agent, reason}]
FAILURE_LOG = []  # Recent hard failure events: [{ts, id, agent, reason}]
LAST_KNOWN_DEV_HEAD = ""  # Tracks HEAD of dev to detect new merge commits
_MAX_LOG_ENTRIES = 8

# --- IDLE QA STATE ---
# Fires once when all queues drain to empty after a busy period.
# Result is displayed in TEAM_STATUS until the next busy cycle clears it.
_queues_were_busy: bool = False
_dev_qa_status: dict = {
}  # {"sha": str, "passed": bool, "detail": str, "ts": str}

# --- SESSION TIME STATS ---
_session_start: float = 0.0  # set in main() at startup
_work_totals: dict = {}  # agent -> total seconds worked this session
_last_stat_agent: str = ""  # last agent seen by update_status (transition detection)
_agent_work_start: float = 0.0  # when current agent's working period began


# --- MULTI-CHANNEL LOGGING ---
def log(file, msg, also_print=True):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{ts}] {msg}"
    if also_print:
        print(formatted_msg)
        sys.stdout.flush()
    with open(LOGS_DIR / file, "a", encoding="utf-8") as f:
        f.write(formatted_msg + "\n\n")


# --- CORE UTILITIES ---
class DummyProcess:

    def __init__(self):
        self.returncode = 0


def run_git(args):
    if DRY_RUN: return DummyProcess()
    return subprocess.run(["git"] + args,
                          cwd=PROJECT_ROOT,
                          capture_output=True,
                          text=True)


def _ollama_running() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "ollama"],
                           capture_output=True,
                           text=True)
        if r.stdout.strip() == "active":
            return True
    except FileNotFoundError:
        pass
    r = subprocess.run(["pgrep", "-f", "ollama"], capture_output=True)
    return r.returncode == 0


def _qwen_available() -> bool:
    try:
        r = subprocess.run(["ollama", "list"],
                           capture_output=True,
                           text=True,
                           timeout=10)
        return "qwen-reviewer" in r.stdout
    except Exception:
        return False


def _discord_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "discord_bot.py"], capture_output=True)
    return r.returncode == 0


def check_startup_services():
    """Check supporting services on startup and offer to start any that are down."""
    print("\n🔍 Checking supporting services...\n")

    # ── 1/3 Ollama ─────────────────────────────────────────────────────────
    ollama_ok = _ollama_running()
    status = "✅ running" if ollama_ok else "❌ not running"
    print(f"  [1/3] Ollama daemon ........... {status}")
    if not ollama_ok:
        ans = input("        Start ollama now? [Y/n]: ").strip().lower()
        if ans not in ("n", "no"):
            print("        Starting...", end=" ", flush=True)
            # Try systemctl first (works if the service unit is configured)
            r = subprocess.run(["systemctl", "start", "ollama"],
                               capture_output=True)
            if r.returncode != 0:
                # Fall back to launching ollama serve directly (orchestrator
                # already runs as aidevteam, so no sudo needed)
                subprocess.Popen(["ollama", "serve"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            time.sleep(4)
            ollama_ok = _ollama_running()
            print("✅ ollama is now running"
                  if ollama_ok else "⚠️  could not verify — continuing")
        print()

    # ── 2/3 Qwen reviewer model ─────────────────────────────────────────────
    if ollama_ok:
        qwen_ok = _qwen_available()
        status = "✅ available" if qwen_ok else "❌ not found"
        print(f"  [2/3] Qwen reviewer model ..... {status}")
        if not qwen_ok:
            ans = input(
                "        Pull qwen-reviewer now? [Y/n]: ").strip().lower()
            if ans not in ("n", "no"):
                print("        Pulling model (this may take a while)...")
                subprocess.run(["ollama", "pull", "qwen-reviewer"])
            print()
    else:
        print(
            "  [2/3] Qwen reviewer model ..... ⏭️  skipped (ollama not running)"
        )

    # ── 3/3 Discord bot ─────────────────────────────────────────────────────
    discord_ok = _discord_running()
    status = "✅ running" if discord_ok else "❌ not running"
    print(f"  [3/3] Discord monitor ......... {status}")
    if not discord_ok:
        ans = input("        Start discord bot now? [Y/n]: ").strip().lower()
        if ans not in ("n", "no"):
            discord_log = LOGS_DIR / "discord_bot.log"
            with open(discord_log, "a") as log_f:
                proc = subprocess.Popen(
                    [sys.executable, str(DISCORD_BOT_PATH)],
                    start_new_session=True,
                    stdout=log_f,
                    stderr=log_f,
                )
            time.sleep(2)
            if proc.poll() is None:
                print(f"        🚀 Discord bot started (PID {proc.pid})"
                      f" — kill with: kill {proc.pid}")
                print(f"        📄 Logs: {discord_log}")
            else:
                print(
                    f"        ❌ Discord bot exited immediately (code {proc.returncode})"
                )
                print(f"        Check logs: {discord_log}")
        print()

    print("✅ Service checks complete.\n")


def check_permissions():
    """Pre-flight check to scan for permission roadblocks before starting."""
    log("orchestrator.log", "🔍 Running pre-flight permission check...")

    locked_files = 0

    # Check frontend/ and backend/ recursively (skip venv — Python binaries are never AI-edited)
    SKIP_DIRS = {"venv", "node_modules", "__pycache__", ".git"}
    for directory in [PROJECT_ROOT / "frontend", PROJECT_ROOT / "backend"]:
        if not directory.exists():
            continue
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for file in files:
                file_path = os.path.join(root, file)
                if not os.access(file_path, os.W_OK):
                    locked_files += 1

    # Also check root-level files (e.g. .gitignore, deployment.md)
    for item in PROJECT_ROOT.iterdir():
        if item.is_file() and not os.access(item, os.W_OK):
            locked_files += 1

    if locked_files > 0:
        log("orchestrator.log",
            f"🛑 WARNING: Found {locked_files} files the AI team cannot edit!")
        log("orchestrator.log",
            f"Please run: sudo setfacl -R -m g:aidevteam:rwX {PROJECT_ROOT}")
        return False
    else:
        log("orchestrator.log",
            "✅ All project files are writable by the AI team.")
        return True


def run_agent(cmd_base, prompt, agent_name):
    global ACTIVE_PROCESS

    if DRY_RUN:
        log("orchestrator.log", f"🌵 [DRY RUN] Would call {agent_name}")
        return json.dumps({
            "summary": "Dry run",
            "content": "Simulated",
            "files": []
        })

    log(f"{agent_name}.log", f">>> PROMPT SENT:\n{prompt}", also_print=False)

    if agent_name in ["gemini", "claude"]:
        TMP_DIR.mkdir(exist_ok=True)
        prompt_file = TMP_DIR / f"{agent_name}_current_task.txt"
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
        safe_cli_prompt = f"Please read your task instructions from this file and execute them: {prompt_file.absolute()}"
    else:
        safe_cli_prompt = prompt

    try:
        # Spawn the process asynchronously
        process = subprocess.Popen(cmd_base + [safe_cli_prompt],
                                   cwd=PROJECT_ROOT,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   text=True)

        ACTIVE_PROCESS = process
        pid = process.pid
        log("orchestrator.log",
            f"⚙️ Spawning {agent_name.upper()} native client (PID: {pid})")

        # Create the timer thread
        stop_event = threading.Event()

        def live_status_updater():
            start_time = time.time()
            while not stop_event.is_set():
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                timer = f"{mins}m {secs}s"
                update_status(agent_name,
                              f"Generating code (PID: {pid}) ⏳ {timer}")
                stop_event.wait(2)

        # daemon=True ensures this thread dies instantly if the main script gets a Ctrl+C
        status_thread = threading.Thread(target=live_status_updater,
                                         daemon=True)
        status_thread.start()

        # Block safely until the AI finishes
        try:
            output, _ = process.communicate(timeout=AGENT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            raise Exception(
                f"Process timed out after {AGENT_TIMEOUT} seconds.")
        finally:
            stop_event.set()
            status_thread.join(timeout=1.0)
            ACTIVE_PROCESS = None

        output = output.strip()
        log(f"{agent_name}.log",
            f"<<< RAW RESPONSE:\n{output}",
            also_print=False)

        if agent_name in ["gemini", "claude"] and prompt_file.exists():
            prompt_file.unlink()

        # Gemini sometimes writes its completion JSON to output.json in the
        # project root instead of (or in addition to) stdout. Delete it so it
        # doesn't accumulate or get committed.
        stray_output = PROJECT_ROOT / "output.json"
        if stray_output.exists():
            stray_output.unlink()
            log(
                "orchestrator.log",
                f"🧹 Cleaned up stray output.json from project root after {agent_name} run"
            )

        return output

    except Exception as e:
        err_msg = f"❌ EXECUTION ERROR: {str(e)}"
        log(f"{agent_name}.log", err_msg)

        if agent_name in ["gemini", "claude"] and prompt_file.exists():
            prompt_file.unlink()

        return json.dumps({"error": err_msg})


def extract_balanced_json(text, start_idx):
    """Extract a balanced JSON object by counting braces, correctly skipping string contents."""
    depth = 0
    in_string = False
    escape_next = False

    for i in range(start_idx, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start_idx:i + 1]
    return None


def parse_json(text):
    # Strategy 1: ```json marker — most explicit signal
    marker_idx = text.lower().find('```json')
    if marker_idx != -1:
        start_idx = text.find('{', marker_idx + 7)
        if start_idx != -1:
            json_str = extract_balanced_json(text, start_idx)
            if json_str:
                try:
                    return json.loads(json_str, strict=False)
                except Exception:
                    pass

    # Strategy 2: scan backwards from the last '"summary":' — our JSON block always
    # contains this key. Using rfind avoids false starts from Gemini's startup noise
    # (e.g. "Capabilities: { tools: {} }") which would break the forward-scan fallback.
    summary_idx = text.rfind('"summary":')
    if summary_idx != -1:
        brace_idx = text.rfind('{', 0, summary_idx)
        if brace_idx != -1:
            json_str = extract_balanced_json(text, brace_idx)
            if json_str:
                try:
                    return json.loads(json_str, strict=False)
                except Exception:
                    pass

    # Strategy 3: iterate every '{' left-to-right as a last resort
    search_from = 0
    while True:
        start_idx = text.find('{', search_from)
        if start_idx == -1:
            break
        json_str = extract_balanced_json(text, start_idx)
        if json_str:
            try:
                return json.loads(json_str, strict=False)
            except Exception:
                pass
        search_from = start_idx + 1

    err_msg = "No valid JSON object found in the response."
    log("orchestrator.log", f"⚠️ JSON PARSE FAILED: {err_msg}")
    log("orchestrator.log",
        f"--- RAW TEXT THAT FAILED ---\n{text[:1000]}\n---------------------------",
        also_print=False)
    return {"parse_error": True, "message": err_msg}


def _load_review_state() -> dict:
    try:
        if REVIEW_STATE_FILE.exists():
            return json.loads(REVIEW_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"review_counts": {}}


def _save_review_state(state: dict):
    try:
        REVIEW_STATE_FILE.write_text(json.dumps(state, indent=2),
                                     encoding="utf-8")
    except Exception as e:
        log("orchestrator.log", f"⚠️ Could not save review state: {e}")


def run_audit(path=None) -> list:
    """Run a full Qwen review on a specific file or all files changed vs main.
    Returns a list of result strings (one per file)."""
    if path:
        targets = [PROJECT_ROOT / path]
    else:
        result = subprocess.run(
            ["git", "diff", f"{BRANCH_DEV}...HEAD", "--name-only"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT)
        targets = [
            PROJECT_ROOT / f for f in result.stdout.strip().splitlines()
            if f.strip()
        ]

    results = []
    for fp in targets:
        if not fp.exists():
            results.append(f"⚠️ {fp.name}: file not found")
            continue
        rel = str(fp.relative_to(PROJECT_ROOT))
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception as e:
            results.append(f"⚠️ {rel}: could not read — {e}")
            continue
        log("orchestrator.log", f"🧐 AUDIT (FULL): {rel}...")
        raw = run_agent(REVIEW_CMD, f"Review this code:\n{content}",
                        "reviewer")
        clean = _clean_review_output(raw)
        summary = _summarize_review(clean, max_chars=600)
        log("review.log", f"[AUDIT] [{rel}] {summary}", also_print=False)
        results.append(f"**{rel}**: {summary}")

    # Reset review counters for audited files so the clock restarts
    state = _load_review_state()
    for fp in targets:
        rel = str(fp.relative_to(PROJECT_ROOT))
        state["review_counts"].pop(rel, None)
    _save_review_state(state)

    return results


def _clean_review_output(raw: str) -> str:
    """Strip terminal escape sequences and ollama spinner noise from review output."""
    # Remove ANSI/VT control sequences
    clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', raw)
    # Remove braille spinner characters (U+2800–U+28FF)
    clean = re.sub(r'[\u2800-\u28FF]+', '', clean)
    # Collapse runs of whitespace/blank lines
    clean = re.sub(r'[ \t]+', ' ', clean)
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    return clean.strip()


def _summarize_review(clean_text: str, max_chars: int = 400) -> str:
    """Return a short summary from cleaned review text."""
    if not clean_text:
        return "(no output)"
    # JSON error object from orchestrator — extract just the message
    if clean_text.lstrip().startswith('{"error"'):
        try:
            err = json.loads(clean_text)
            msg = err.get("error", clean_text)
            return msg[:max_chars]
        except Exception:
            pass
    # Collapse to single line and trim
    lines = [l.strip() for l in clean_text.splitlines() if l.strip()]
    summary = ' '.join(lines)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(' ', 1)[0] + '…'
    return summary


def claude_ui_review():
    """Ask Claude to review only Gemini's frontend diff for NES aesthetic compliance.
    Returns (approved: bool, feedback: str).
    """
    if DRY_RUN:
        return True, ""

    diff_result = subprocess.run(["git", "diff", "HEAD", "--", "frontend/"],
                                 cwd=PROJECT_ROOT,
                                 capture_output=True,
                                 text=True)
    diff_text = diff_result.stdout.strip()

    if not diff_text:
        log("orchestrator.log",
            "⏩ UI review skipped — no frontend diff detected.")
        return True, ""

    # Cap diff size to keep token usage reasonable but catch more issues
    if len(diff_text) > 12000:
        diff_text = diff_text[:12000] + "\n... (truncated)"

    review_prompt = (
        "You are the UI/UX Supervisor for FlashQuest, a strict NES/8-bit arcade app. "
        "Review ONLY the following git diff from the frontend developer (Gemini) "
        "and check for violations of these rules:\n"
        "1. NES.css only — no Tailwind, Bootstrap, MUI, or inline modern styles\n"
        "2. 2-Button SRS only — Hit and Miss. No rating scales.\n"
        "3. ZERO browser dialogs — window.confirm(), window.alert(), and alert() are BANNED. "
        "All confirmations must use ThemedModal. Reject immediately if any of these appear.\n"
        "4. userStats null safety — accessing userStats.field (without optional chaining ?.) "
        "at component top level or in useEffect dependency arrays is a crash bug. Reject if found.\n"
        "5. Field names — userStats.is_staff is WRONG (must be userStats.isStaff). "
        "userStats.current_streak is WRONG (must be userStats.streak). Reject if found.\n"
        f"DIFF:\n{diff_text}\n\n"
        "Reply with ONLY valid JSON — no explanation, no preamble:\n"
        "{\"action\": \"approve\"}\n"
        "OR\n"
        "{\"action\": \"reject\", \"feedback\": \"specific rule violations found\"}"
    )

    log("orchestrator.log",
        "🔍 Routing Gemini diff to Claude for UI/UX review...")
    update_status("claude", "Reviewing Gemini UI/UX diff...")

    raw = run_agent(CLAUDE_CMD, review_prompt, "claude")
    update_status()

    try:
        result = parse_json(raw)
        action = result.get("action", "approve").lower()
        if action == "reject":
            feedback = result.get("feedback", "No specific feedback provided.")
            log("orchestrator.log", f"❌ UI REVIEW REJECTED: {feedback}")
            return False, feedback
        else:
            log("orchestrator.log", "✅ UI REVIEW APPROVED by Claude.")
            return True, ""
    except Exception as e:
        log("orchestrator.log",
            f"⚠️ UI review parse error: {e} — defaulting to approve.")
        return True, ""


def _get_ollama_journal_cursor():
    """Return the current journalctl cursor for the ollama service."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "ollama", "--show-cursor", "-n", "0"],
            capture_output=True,
            text=True,
            timeout=5)
        for line in result.stdout.splitlines():
            if line.startswith("-- cursor:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _capture_ollama_journal(cursor):
    """Read ollama journal entries since cursor; return filtered log lines."""
    if not cursor:
        return ""
    try:
        result = subprocess.run([
            "journalctl", "-u", "ollama", f"--after-cursor={cursor}", "-o",
            "cat", "--no-pager"
        ],
                                capture_output=True,
                                text=True,
                                timeout=5)
        lines = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            # Keep GIN timing lines and WARN/ERROR level entries; skip health-check noise
            if "[GIN]" in line or "level=WARN" in line or "level=ERROR" in line:
                # Skip noisy health-check endpoints
                if any(skip in line for skip in
                       [" HEAD /", "GET /api/tags", "GET /api/version"]):
                    continue
                lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def apply_changes(response_json, agent_name, skip_review=False):
    files = response_json.get("files", [])
    if not files or DRY_RUN: return

    for f in files:
        if not f or not isinstance(f, dict) or "path" not in f:
            continue

        rel_path = f["path"].lstrip("/")

        # --- PATH TRAVERSAL PROTECTION ---
        # Resolve to an absolute path and confirm it stays inside PROJECT_ROOT
        try:
            full_path = (PROJECT_ROOT / rel_path).resolve()
            if not str(full_path).startswith(str(PROJECT_ROOT)):
                log(
                    "orchestrator.log",
                    f"🚨 PATH TRAVERSAL BLOCKED: {agent_name} tried to write outside project root: {rel_path}"
                )
                continue
        except Exception as e:
            log("orchestrator.log",
                f"⚠️ Path resolution failed for {rel_path}: {e}")
            continue

        if any(rel_path.startswith(p) for p in PROTECTED_PATHS):
            log("orchestrator.log",
                f"⚠️ SECURITY ALERT: {agent_name} blocked from {rel_path}!")
            continue

        code_content = f.get("code", "")
        # --- THE NATIVE TOOL COLLISION FIX ---
        is_placeholder = any(phrase in code_content.upper() for phrase in [
            "ALREADY WRITTEN", "VIA TOOL", "SEE_FILE", "SEE FILE", "ON DISK",
            "ALREADY SAVED"
        ])

        if is_placeholder:
            log(
                "orchestrator.log",
                f"👍 {agent_name.upper()} natively wrote {rel_path}. Skipping JSON overwrite."
            )
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as file:
                file.write(code_content)
            log("orchestrator.log",
                f"💾 {agent_name.upper()} WROTE: {rel_path}")

        if code_content and not skip_review:
            # --- DIFF-FIRST REVIEW WITH PERIODIC FULL-FILE SCAN ---
            diff_result = subprocess.run(
                ["git", "diff", "HEAD", "--", rel_path],
                capture_output=True,
                text=True,
                cwd=PROJECT_ROOT)
            diff_text = diff_result.stdout.strip()

            # Increment per-file review counter
            rev_state = _load_review_state()
            counts = rev_state.setdefault("review_counts", {})
            counts[rel_path] = counts.get(rel_path, 0) + 1
            do_full = not diff_text or counts[rel_path] >= FULL_REVIEW_THRESHOLD
            if do_full:
                counts[rel_path] = 0  # reset after full review
            _save_review_state(rev_state)

            if do_full:
                # New file (no diff) or threshold reached — full file review
                if is_placeholder:
                    try:
                        with open(full_path, "r", encoding="utf-8") as file:
                            review_code = file.read()
                    except Exception as e:
                        review_code = f"Error reading file for review: {e}"
                else:
                    review_code = code_content
                review_label = "FULL" if diff_text else "NEW"
            else:
                review_code = diff_text
                review_label = f"DIFF ({counts[rel_path]}/{FULL_REVIEW_THRESHOLD})"

            log("orchestrator.log",
                f"🧐 QWEN REVIEWING ({review_label}): {rel_path}...")
            _journal_cursor = _get_ollama_journal_cursor()
            review_out = run_agent(REVIEW_CMD,
                                   f"Review this code:\n{review_code}",
                                   "reviewer")
            _ollama_logs = _capture_ollama_journal(_journal_cursor)
            clean_out = _clean_review_output(review_out)
            summary = _summarize_review(clean_out)
            review_entry = f"[{review_label}] [{rel_path}] {summary}"
            if _ollama_logs:
                review_entry += f"\n  Ollama: {_ollama_logs}"
            log("review.log", review_entry, also_print=False)
        elif code_content and skip_review:
            log("orchestrator.log",
                f"⏩ REVIEW SKIPPED (--no-review): {rel_path}")


def _fmt_dur(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, rem = divmod(m, 60)
    return f"{h}h {rem}m" if rem else f"{h}h"


def _record_kickback(task_id: str, agent: str, reason: str):
    """Append a kickback event to the in-memory log (capped at _MAX_LOG_ENTRIES)."""
    KICKBACK_LOG.append({
        "ts": datetime.now().strftime("%H:%M"),
        "id": task_id,
        "agent": agent.upper(),
        "reason": reason,
    })
    if len(KICKBACK_LOG) > _MAX_LOG_ENTRIES:
        KICKBACK_LOG.pop(0)
    log("orchestrator.log",
        f"↩️ KICKBACK [{task_id}] → {agent.upper()}: {reason}")


def _task_to_markdown(msg: dict, status: str = "processed") -> str:
    """Convert a task JSON message to a human-readable markdown string."""
    lines = []
    task_id = msg.get("id") or Path(msg.get("path", "unknown")).stem
    lines.append(f"# Task: {task_id}")
    lines.append(f"**Status:** {status.upper()}")
    lines.append(f"**Timestamp:** {msg.get('timestamp', 'unknown')}")
    lines.append(
        f"**From:** {msg.get('from', '?')}  →  **To:** {msg.get('to', '?')}")
    if msg.get("failure_reason"):
        lines.append(f"\n> ⚠️ **Failure Reason:** {msg['failure_reason']}")
    if msg.get("retry_count"):
        lines.append(f"**Retry count:** {msg['retry_count']}")
    if msg.get("baton_depth"):
        lines.append(f"**Baton depth:** {msg['baton_depth']}")
    if msg.get("skip_review"):
        lines.append(f"**Skip review:** {msg['skip_review']}")
    task_text = msg.get("task", "").strip()
    if task_text:
        lines.append(f"\n## Task\n\n{task_text}")
    context_text = msg.get("context", "").strip()
    if context_text:
        lines.append(f"\n## Context\n\n{context_text}")
    return "\n".join(lines) + "\n"


def _write_markdown_alongside(dest_json_path: Path, msg: dict, status: str):
    """Write a .md sidecar file next to the JSON for human readability."""
    try:
        md_path = dest_json_path.with_suffix(".md")
        md_path.write_text(_task_to_markdown(msg, status), encoding="utf-8")
    except Exception as e:
        log("orchestrator.log", f"⚠️ Could not write markdown sidecar: {e}")


def _move_to_processed(f_path):
    """Move a task file to PROCESSED/, write a markdown sidecar, then delete the JSON."""
    src = Path(f_path)
    dest = PROCESSED / src.name
    try:
        msg = json.loads(src.read_text(encoding="utf-8"))
        shutil.move(str(src), dest)
        _write_markdown_alongside(dest, msg, "processed")
        dest.unlink(missing_ok=True)
    except Exception:
        shutil.move(str(src), dest)


def _move_to_failed(f_path, task_id: str, agent: str, reason: str):
    """Move a task file to FAILED/, stamping it with the failure_reason and logging it."""
    FAILURE_LOG.append({
        "ts": datetime.now().strftime("%H:%M"),
        "id": task_id,
        "agent": agent.upper(),
        "reason": reason,
    })
    if len(FAILURE_LOG) > _MAX_LOG_ENTRIES:
        FAILURE_LOG.pop(0)
    log("orchestrator.log",
        f"💀 HARD FAILURE [{task_id}] ({agent.upper()}): {reason}")
    try:
        msg = json.loads(Path(f_path).read_text(encoding="utf-8"))
        msg["failure_reason"] = reason
        dest = FAILED / Path(f_path).name
        _write_markdown_alongside(dest, msg, "failed")
        Path(f_path).unlink()
    except Exception:
        shutil.move(f_path, FAILED / Path(f_path).name)


def update_status(active_agent=None, activity="", permission_warning=False):
    """Write TEAM_STATUS.md — unified status dashboard and live task board."""
    global _last_stat_agent, _agent_work_start, _work_totals
    now = time.time()
    ts = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")

    # --- TIME TRACKING: detect agent transitions and accumulate ---
    agent_key = active_agent or ""
    if agent_key != _last_stat_agent:
        if _last_stat_agent and _agent_work_start:
            elapsed = now - _agent_work_start
            _work_totals[_last_stat_agent] = _work_totals.get(
                _last_stat_agent, 0.0) + elapsed
        _agent_work_start = now if agent_key else 0.0
        _last_stat_agent = agent_key

    # Queue counts
    human_q = len(list(INBOX_HUMAN.glob("*.json"))) + len(
        list(INBOX_HUMAN.glob("*.md")))
    gemini_q = len(list(INBOX_GEMINI.glob("*.json")))
    claude_q = len(list(INBOX_CLAUDE.glob("*.json")))
    total_q = human_q + gemini_q + claude_q
    failed_q = len(list(FAILED.glob("*.md")))

    lines = []

    # --- PAUSED BANNER ---
    if PAUSE_SENTINEL.exists():
        pause_info = PAUSE_SENTINEL.read_text().strip()
        lines.append(f"# ⏸ ORCHESTRATOR PAUSED\n")
        lines.append(f"```\n{pause_info}\n```\n")
        lines.append(
            f"**Delete `ai_team/messages/PAUSED` to resume.**\n\n---\n\n")

    # --- IDLE QA BANNER ---
    if _dev_qa_status:
        if _dev_qa_status["passed"]:
            lines.append(
                f"# ✅ DEV READY TO PROMOTE\n"
                f"`{BRANCH_DEV}` @ `{_dev_qa_status['sha']}` passed all QA checks at {_dev_qa_status['ts']}. "
                f"Safe to merge into `{BRANCH_MAIN}`.\n\n---\n\n")
        else:
            lines.append(
                f"# ❌ DEV QA FAILED\n"
                f"`{BRANCH_DEV}` @ `{_dev_qa_status['sha']}` failed QA at {_dev_qa_status['ts']}.\n"
                f"```\n{_dev_qa_status['detail'][:800]}\n```\n\n---\n\n")

    # --- HEADER ---
    lines.append(f"**🤖 AI TEAM STATUS** | ⏱️ {ts}\n")

    warning = " | 🛑 **PERMISSIONS ERROR: Run `sudo setfacl -R -m g:aidevteam:rwX .`**" if permission_warning else ""
    failed_warn = f" | ❌ **{failed_q} FAILED** (check messages/failed/)" if failed_q > 0 else ""
    lines.append(
        f"📦 **Queue [{total_q}]:** 👤 {human_q} | 🦊 {gemini_q} | 🦉 {claude_q}{failed_warn}{warning}\n"
    )

    for agent, icon, title in [("gemini", "🦊", "Gemini"),
                               ("claude", "🦉", "Claude"),
                               ("reviewer", "🧐", "Qwen")]:
        cooldown_remaining = AGENT_COOLDOWNS.get(agent, 0) - time.time()
        if cooldown_remaining > 0:
            mins_left = int(cooldown_remaining // 60) + 1
            status_text = f"🔴 TIMEOUT (~{mins_left}m remaining)"
        elif active_agent == agent:
            status_text = f"🟡 WORKING - {activity}"
        else:
            status_text = "🟢 IDLE"
        lines.append(f"{icon} **{title}:** {status_text}\n")

    discord_alive = subprocess.run(["pgrep", "-f", "discord_bot.py"],
                                   capture_output=True).returncode == 0
    discord_icon = "🟢" if discord_alive else "🔴"
    discord_label = "LIVE" if discord_alive else "OFFLINE"
    lines.append(f"🔔 **Discord:** {discord_icon} {discord_label}\n")

    # --- SESSION STATS ---
    if _session_start:
        session_dur = now - _session_start
        total_worked = sum(_work_totals.values())
        if _last_stat_agent and _agent_work_start:
            total_worked += now - _agent_work_start
        idle_dur = max(0.0, session_dur - total_worked)
        worked_pct = int(total_worked / session_dur *
                         100) if session_dur > 0 else 0
        idle_pct = 100 - worked_pct
        lines.append(f"\n## ⏱️ Session Stats\n")
        lines.append(f"**Uptime:** {_fmt_dur(session_dur)} | "
                     f"🟡 Working: {_fmt_dur(total_worked)} ({worked_pct}%) | "
                     f"⚪ Idle: {_fmt_dur(idle_dur)} ({idle_pct}%)\n")
        agent_parts = []
        for key, icon, label in [("gemini", "🦊", "Gemini"),
                                 ("claude", "🦉", "Claude"),
                                 ("reviewer", "🧐", "Qwen")]:
            secs = _work_totals.get(key, 0.0)
            if _last_stat_agent == key and _agent_work_start:
                secs += now - _agent_work_start
            if secs > 0:
                pct = int(secs / total_worked * 100) if total_worked > 0 else 0
                agent_parts.append(
                    f"{icon} {label}: {_fmt_dur(secs)} ({pct}%)")
        if agent_parts:
            lines.append("  ·  ".join(agent_parts) + "\n")

    # --- IN PROGRESS ---
    if CURRENT_TASKS:
        lines.append("\n## 🟡 In Progress\n")
        for agent, info in CURRENT_TASKS.items():
            elapsed = int(time.time() - info["started_at"])
            m, s = divmod(elapsed, 60)
            lines.append(
                f"- [ ] **[{agent.upper()}]** `{info['id']}` *(⏳ {m}m {s}s)* — {info['preview']}\n"
            )
    else:
        lines.append("\n## ✅ Status\n")
        lines.append("*Idle — no tasks in progress.*\n")

    # # --- QUEUED ---
    # lines.append("\n## 📋 Queued\n")
    # queued_items = []
    # for inbox, label in [(INBOX_CLAUDE, "CLAUDE"), (INBOX_GEMINI, "GEMINI")]:
    #     for fp in sorted(inbox.glob("*.json"),
    #                      key=lambda x: x.stat().st_mtime):
    #         try:
    #             msg = json.loads(fp.read_text(encoding="utf-8"))
    #             preview = str(msg.get("task", "")).replace("\n",
    #                                                        " ").strip()[:100]
    #         except Exception:
    #             preview = "*(unreadable)*"
    #         queued_items.append(
    #             f"- [ ] **[{label}]** `{fp.stem}` — {preview}\n")
    # lines.extend(queued_items if queued_items else ["*Queue is empty.*\n"])

    # # --- RECENTLY COMPLETED ---
    # lines.append("\n## ✅ Recently Completed\n")
    # processed = sorted([f for f in PROCESSED.glob("*.json") if f.is_file()],
    #                    key=lambda x: x.stat().st_mtime,
    #                    reverse=True)[:8]

    # if processed:
    #     for fp in processed:
    #         try:
    #             msg = json.loads(fp.read_text(encoding="utf-8"))
    #             agent = msg.get("to", msg.get("from", "?")).upper()
    #             preview = str(msg.get("task", "")).replace("\n",
    #                                                        " ").strip()[:100]
    #         except Exception:
    #             agent, preview = "?", "*(unreadable)*"
    #         lines.append(f"- [x] **[{agent}]** `{fp.stem}` — {preview}\n")
    # else:
    #     lines.append("*No completed tasks yet.*\n")

    # --- KICKBACKS ---
    if KICKBACK_LOG:
        lines.append("\n## ↩️ Recent Kickbacks\n")
        for kb in reversed(KICKBACK_LOG):
            lines.append(
                f"- `{kb['ts']}` **[{kb['agent']}]** kickback `{kb['id']}` — {kb['reason']}\n"
            )

    # --- FAILED ---
    failed_files = sorted([f for f in FAILED.glob("*.md") if f.is_file()],
                          key=lambda x: x.stat().st_mtime,
                          reverse=True)[:5]
    if failed_files:
        lines.append("\n## ❌ Hard Failures\n")
        for fp in failed_files:
            try:
                content = fp.read_text(encoding="utf-8")
                agent = "?"
                reason = fp.stem
                for line in content.splitlines():
                    if "**To:**" in line:
                        agent = line.split("**To:**", 1)[1].strip().upper()
                    if "**Failure Reason:**" in line:
                        reason = line.split("**Failure Reason:**",
                                            1)[1].strip()[:100]
                        break
            except Exception:
                agent, reason = "?", "*(unreadable)*"
            lines.append(f"- ❌ **[{agent}]** `{fp.stem}` — {reason}\n")

    try:
        TEAM_STATUS_FILE.write_text("".join(lines), encoding="utf-8")
    except Exception as e:
        log("orchestrator.log",
            f"⚠️ Could not write TEAM_STATUS.md: {e}",
            also_print=False)


def archive_processed_files():
    # Get all files in PROCESSED, excluding the zip archive itself
    files = [
        f for f in PROCESSED.glob("*")
        if f.is_file() and f.name != "archive.zip"
    ]

    # If there are 10 or fewer files, do nothing
    if len(files) <= 10:
        return

    # Sort files by modification time (newest first)
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    # Grab everything after the first 10
    files_to_archive = files[10:]
    archive_path = PROCESSED / "archive.zip"

    try:
        # Open the zip file in append mode ('a') so it continually adds to the same archive
        with zipfile.ZipFile(archive_path, 'a', zipfile.ZIP_DEFLATED) as zipf:
            for f in files_to_archive:
                zipf.write(f, arcname=f.name)
                f.unlink(
                )  # Delete the physical file after it is safely zipped

        log("orchestrator.log",
            f"🗄️ AUTO-ARCHIVED {len(files_to_archive)} old files to archive.zip",
            also_print=False)
    except Exception as e:
        log("orchestrator.log",
            f"⚠️ Failed to archive processed files: {e}",
            also_print=False)


def get_next_id():
    """Return a zero-padded task ID. Uses a file lock to prevent races with task.py."""
    COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(COUNTER_LOCK_FILE, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            if not COUNTER_FILE.exists():
                count = 1
            else:
                try:
                    count = int(COUNTER_FILE.read_text().strip()) + 1
                except (ValueError, OSError):
                    count = 1
            COUNTER_FILE.write_text(str(count))
            return f"{count:04d}"
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def revert_agent_files(reply_json):
    """Revert only the files an agent changed, leaving the rest of the working tree intact."""
    changed_paths = []
    for f in reply_json.get("files", []):
        if f and isinstance(f, dict) and "path" in f:
            p = f["path"].lstrip("/")
            if not any(p.startswith(pp) for pp in PROTECTED_PATHS):
                changed_paths.append(p)

    if not changed_paths:
        # No explicit file list — fall back to a full hard reset
        log(
            "orchestrator.log",
            "⚠️ No file list to target for revert — falling back to git reset --hard"
        )
        run_git(["reset", "--hard"])
        return

    for p in changed_paths:
        full = PROJECT_ROOT / p
        # Try to restore the file from HEAD (works for tracked files)
        result = run_git(["checkout", "HEAD", "--", p])
        if result.returncode != 0:
            # File is new/untracked — just delete it
            if full.exists():
                try:
                    full.unlink()
                    log("orchestrator.log",
                        f"🗑️ Deleted new file after QA failure: {p}",
                        also_print=False)
                except Exception as e:
                    log("orchestrator.log",
                        f"⚠️ Could not delete {p}: {e}",
                        also_print=False)
        else:
            log("orchestrator.log",
                f"↩️ Reverted {p} to HEAD",
                also_print=False)


def trigger_failure_pause(task_name: str, reason: str):
    """Halt the orchestrator loop until the human deletes the PAUSE_SENTINEL file."""
    PAUSE_SENTINEL.write_text(
        f"PAUSED\nTask: {task_name}\nReason: {reason}\n"
        f"To resume: delete this file or run:\n  rm ai_team/messages/PAUSED\n")
    log("orchestrator.log", f"ORCHESTRATOR PAUSED -- {task_name}: {reason}")
    print(f"\n{'='*60}")
    print(f"  ORCHESTRATOR PAUSED")
    print(f"  Task '{task_name}' permanently failed.")
    print(f"  Reason: {reason}")
    print(f"  To resume: delete ai_team/messages/PAUSED")
    print(f"{'='*60}\n")


def check_post_merge_qa():
    """Detect new merge commits on dev and run full QA if one is found.
    The dev branch is the integration point where both agent branches land —
    this is where structural divergence (duplicate declarations, broken arrow
    function syntax, missing helper functions) first manifests as parse errors.
    Alerts the human inbox on failure; does nothing on pass or non-merge commits.
    main is never watched here — it only receives verified merges from dev.
    """
    global LAST_KNOWN_DEV_HEAD

    # Skip while an agent process is running to avoid confusing git state
    if ACTIVE_PROCESS and ACTIVE_PROCESS.poll() is None:
        return

    result = run_git(["rev-parse", f"refs/heads/{BRANCH_DEV}"])
    if result.returncode != 0:
        return
    current = result.stdout.strip()
    if not current or current == LAST_KNOWN_DEV_HEAD:
        LAST_KNOWN_DEV_HEAD = current
        return
    LAST_KNOWN_DEV_HEAD = current

    # Only act on merge commits (2+ parents)
    parents_result = run_git(["log", "-1", "--pretty=%P", current])
    if parents_result.returncode != 0:
        return
    parents = parents_result.stdout.strip().split()
    if len(parents) < 2:
        return  # Regular commit — nothing to verify

    log(
        "orchestrator.log",
        f"🔀 New merge commit on {BRANCH_DEV} ({current[:8]}) detected — running post-merge QA..."
    )

    backend_result = subprocess.run([
        "docker", "compose", "-f", "docker-compose.dev.yml", "exec", "-T",
        "backend", "python", "manage.py", "check"
    ],
                                    cwd=PROJECT_ROOT,
                                    capture_output=True,
                                    text=True)

    lint_result = subprocess.run([
        "docker", "compose", "-f", "docker-compose.dev.yml", "exec", "-T",
        "frontend", "npm", "run", "lint"
    ],
                                 cwd=PROJECT_ROOT,
                                 capture_output=True,
                                 text=True)

    failures = []
    if backend_result.returncode != 0:
        failures.append(
            f"BACKEND (manage.py check):\n{(backend_result.stdout + backend_result.stderr).strip()[:600]}"
        )
    if lint_result.returncode != 0:
        failures.append(
            f"FRONTEND (npm run lint):\n{(lint_result.stdout + lint_result.stderr).strip()[:600]}"
        )

    if failures:
        error_output = "\n\n".join(failures)
        log(
            "orchestrator.log",
            f"💥 POST-MERGE QA FAILED on {BRANCH_DEV} ({current[:8]}):\n{error_output[:800]}"
        )
        alert_id = get_next_id()
        alert = {
            "id":
            alert_id,
            "from":
            "orchestrator",
            "to":
            "human",
            "task":
            (f"POST-MERGE QA FAILURE on {BRANCH_DEV} ({current[:8]}).\n\n"
             f"A merge commit was detected on {BRANCH_DEV} and QA failed. "
             f"This is typically caused by structural divergence between the agent branches "
             f"(duplicate declarations, broken arrow function syntax, missing helper functions, "
             f"or a Django model/migration mismatch).\n\n"
             f"do NOT merge {BRANCH_DEV} into {BRANCH_MAIN} until this is resolved.\n\n"
             f"Errors:\n{error_output[:2000]}\n\n"
             f"Action: fix the issues in the listed files, commit to {BRANCH_DEV}, "
             f"and confirm both `manage.py check` and `npm run lint` pass cleanly."
             ),
            "timestamp":
            datetime.now().isoformat()
        }
        with open(HUMAN_BOX / f"merge_qa_failure_{alert_id}.json", "w") as hf:
            json.dump(alert, hf, indent=2)
        log(
            "orchestrator.log",
            f"📬 Post-merge QA failure alert written to human inbox: merge_qa_failure_{alert_id}.json"
        )
    else:
        log(
            "orchestrator.log",
            f"✅ Post-merge QA passed on {BRANCH_DEV} ({current[:8]}). Safe to merge into {BRANCH_MAIN} when ready."
        )


def run_idle_dev_qa():
    """Run full QA on dev when all queues just went idle.
    Updates _dev_qa_status so TEAM_STATUS shows a 'ready to promote' or
    'QA failed' banner until the next busy cycle clears it.
    """
    global _dev_qa_status

    sha_result = run_git(["rev-parse", "--short", f"refs/heads/{BRANCH_DEV}"])
    sha = sha_result.stdout.strip(
    ) if sha_result.returncode == 0 else "unknown"
    ts = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")

    log("orchestrator.log",
        f"🏁 All queues idle — running final dev QA ({sha})...")

    backend_result = subprocess.run([
        "docker", "compose", "-f", "docker-compose.dev.yml", "exec", "-T",
        "backend", "python", "manage.py", "check"
    ],
                                    cwd=PROJECT_ROOT,
                                    capture_output=True,
                                    text=True)

    lint_result = subprocess.run([
        "docker", "compose", "-f", "docker-compose.dev.yml", "exec", "-T",
        "frontend", "npm", "run", "lint"
    ],
                                 cwd=PROJECT_ROOT,
                                 capture_output=True,
                                 text=True)

    failures = []
    if backend_result.returncode != 0:
        failures.append(
            f"BACKEND:\n{(backend_result.stdout + backend_result.stderr).strip()[:600]}"
        )
    if lint_result.returncode != 0:
        failures.append(
            f"FRONTEND:\n{(lint_result.stdout + lint_result.stderr).strip()[:600]}"
        )

    if failures:
        detail = "\n\n".join(failures)
        _dev_qa_status = {
            "sha": sha,
            "passed": False,
            "detail": detail,
            "ts": ts
        }
        log("orchestrator.log",
            f"❌ Idle dev QA FAILED ({sha}):\n{detail[:800]}")
    else:
        _dev_qa_status = {"sha": sha, "passed": True, "detail": "", "ts": ts}
        log(
            "orchestrator.log",
            f"✅ Idle dev QA passed ({sha}) — dev is ready to promote to {BRANCH_MAIN}."
        )


def process_queue(inbox_path, agent_name):
    global AGENT_COOLDOWNS

    # Check rate limit timeout
    if time.time() < AGENT_COOLDOWNS.get(agent_name, 0):
        return

    files = list(inbox_path.glob("*.json"))
    for f_path in files:
        try:
            with open(f_path) as f:
                msg = json.load(f)
        except Exception as e:
            log("orchestrator.log", f"❌ FAILED TO LOAD {f_path.name}: {e}")
            continue

        # --- MAX RETRY GUARD ---
        retry_count = msg.get("retry_count", 0)
        if retry_count >= MAX_TASK_RETRIES:
            log(
                "orchestrator.log",
                f"🚫 MAX RETRIES ({MAX_TASK_RETRIES}) exceeded for {f_path.name}. Permanently failing task."
            )
            if not DRY_RUN:
                _move_to_failed(f_path, msg.get("id", f_path.stem), agent_name,
                                f"Max retries ({MAX_TASK_RETRIES}) exceeded")
            trigger_failure_pause(
                f_path.name, f"Max retries ({MAX_TASK_RETRIES}) exceeded")
            continue

        raw_task = msg.get('task')
        task_text = str(raw_task) if raw_task else "No task specified."

        # --- REGISTER TASK ON LIVE BOARD ---
        CURRENT_TASKS[agent_name] = {
            "id": f_path.stem,
            "preview": task_text.replace("\n", " ").strip()[:100],
            "started_at": time.time()
        }

        log("orchestrator.log",
            f"🏃 PROCESSING ({agent_name.upper()}): {task_text[:600]}...")
        update_status(agent_name,
                      f"Thinking and writing code for {f_path.name}...")

        branch = BRANCH_GEMINI if agent_name == "gemini" else BRANCH_CLAUDE
        checkout_attempt = run_git(["checkout", branch])
        if checkout_attempt and checkout_attempt.returncode != 0:
            log("orchestrator.log",
                f"🌱 Branch '{branch}' missing. Creating from {BRANCH_DEV}...")
            run_git(["checkout", "-b", branch, BRANCH_DEV])
        else:
            # Bring the agent branch up to date with any dev changes from other
            # tasks before this agent runs, so it always starts from latest state.
            sync_result = run_git([
                "merge", BRANCH_DEV, "--no-edit", "-m",
                f"Sync {branch} with {BRANCH_DEV}"
            ])
            if sync_result.returncode != 0:
                run_git(["merge", "--abort"])
                log(
                    "orchestrator.log",
                    f"⚠️  Could not auto-sync {branch} with {BRANCH_DEV} — proceeding without sync. "
                    f"This may indicate conflicting changes.")

        context_text = msg.get('context', '')
        # --- STRICT ROLE ENFORCEMENT ---
        if agent_name == "claude":
            role_instruction = "You are CLAUDE, the BACKEND LEAD (Django/Python). You MUST ONLY edit backend/ files. If frontend work is needed, write a handoff for Gemini."
            qa_instruction = (
                "- Claude (Backend): You MUST run `docker compose -f docker-compose.dev.yml exec -T backend python manage.py check` to verify Django starts cleanly. "
                "THEN use your Playwright MCP browser tool to do a live verification: "
                "navigate to http://localhost:8000/api/catchphrase and confirm it returns JSON with a 'text' field; "
                "navigate to http://localhost:8000/api/users/me and confirm it returns 401, not 500. "
                "Report the browser results before generating your final JSON."
            )
        else:
            role_instruction = "You are GEMINI, the FRONTEND LEAD (React/JS). You MUST ONLY edit frontend/ files. If backend work is needed, write a handoff for Claude."
            qa_instruction = (
                "- Gemini (Frontend): You MUST run `npm run lint` in the frontend directory to verify the UI code. "
                "THEN use your Playwright MCP browser tool to do a live verification: "
                "navigate to http://localhost:5173, confirm the app loads without uncaught JS errors, "
                "and visually verify that your specific changes render correctly. "
                "Report the browser results before generating your final JSON."
            )

        # --- ROLE-SPECIFIC GUARDRAILS ---
        if agent_name == "gemini":
            guardrails = (
                f'HARD GUARDRAILS — These are non-negotiable. Violating any will cause a UI review rejection:\n'
                f'G1. ZERO browser dialogs: window.confirm(), window.alert(), and alert() are BANNED. '
                f'All confirmations and errors MUST use ThemedModal (import ThemedModal from "../components/ThemedModal"). '
                f'Modal state pattern: const [modal, setModal] = useState({{open:false,title:"",message:"",confirmText:"OK",cancelText:null,onConfirm:null}}). '
                f'Render: <ThemedModal isOpen={{modal.open}} title={{modal.title}} message={{modal.message}} confirmText={{modal.confirmText}} cancelText={{modal.cancelText}} onConfirm={{modal.onConfirm||(() => setModal(m=>{{...m,open:false}}))}} onCancel={{() => setModal(m=>{{...m,open:false}}))}} />\n'
                f'G2. userStats null safety: userStats is loaded async and is null on first render. '
                f'NEVER access userStats.anything without optional chaining (userStats?.field). '
                f'Every component that receives userStats MUST have an early return: if (!userStats || loading) return <div className="nes-container is-dark">LOADING...</div>;\n'
                f'G3. Correct userStats field names — App.jsx remaps these: '
                f'use userStats.isStaff (NOT is_staff), userStats.streak (NOT current_streak), '
                f'userStats.dailyNewLimit (NOT daily_new_limit), userStats.dailyReviewLimit (NOT daily_review_limit), '
                f'userStats.newCardsToday (NOT new_cards_today), userStats.reviewsToday (NOT reviews_today).\n'
                f'G4. No redundant API calls: Do NOT fetch /api/users/me inside a page component if userStats is passed as a prop. Read the prop directly.\n'
                f'G5. No leftover scripts: If you create a one-time patch script, delete it immediately after use. Never leave patch_*.js or fix_*.py in the repo root.\n'
            )
        else:
            guardrails = (
                f'HARD GUARDRAILS — Applies to every Gemini handoff you write:\n'
                f'G1. NEVER instruct Gemini to use window.confirm(), window.alert(), or alert(). '
                f'Always specify ThemedModal. Copy this exact pattern into handoffs: '
                f'State: const [modal, setModal] = useState({{open:false,title:"",message:"",confirmText:"OK",cancelText:null,onConfirm:null}}). '
                f'Usage: setModal({{open:true,title:"...",message:"...",confirmText:"YES",cancelText:"NO",onConfirm:()=>{{setModal(m=>{{...m,open:false}}));/*action*/}}}}). '
                f'JSX: <ThemedModal isOpen={{modal.open}} title={{modal.title}} message={{modal.message}} confirmText={{modal.confirmText}} cancelText={{modal.cancelText}} onConfirm={{modal.onConfirm}} onCancel={{()=>setModal(m=>{{...m,open:false}}))}} />\n'
                f'G2. When referencing userStats fields in handoffs, always use the camelCase mapped names: '
                f'isStaff, streak, dailyNewLimit, dailyReviewLimit, newCardsToday, reviewsToday. '
                f'Never write is_staff, current_streak, etc. in instructions to Gemini.\n'
                f'G3. Always specify userStats?.field (with optional chaining) in any JSX or hook code you write for Gemini.\n'
                f'G4. Never tell Gemini to fetch /api/users/me — userStats is already available as a prop.\n'
            )

        json_instruction = (
            f'\n\n--- CRITICAL WORKFLOW INSTRUCTION ---\n'
            f'{role_instruction}\n'
            f'You are operating in an autonomous CLI environment. You have full native tool access (Read, Edit, Write, Bash) and SHOULD use them to implement changes and run tests. After completing all work, you MUST output your final response as a valid JSON block (see structure below) — this is mandatory, the orchestrator cannot route your work without it. If you wrote files natively with Edit/Write tools, use "ALREADY WRITTEN" as the code value in the files array.\n'
            f'\n{guardrails}\n'
            f'BEFORE you generate your final response, you MUST use your tools to verify your code works:\n'
            f'{qa_instruction}\n'
            f'If your tests show an error, FIX the code and test again. Do not hand off broken code.\n\n'
            f'ONLY AFTER your code is tested and verified, output your final response in valid JSON using this exact structure:\n'
            f'{{\n'
            f'  "summary": "one-line description of what you did",\n'
            f'  "content": "Instructions for the next agent only. NEVER embed file code here — file code belongs in the files array.",\n'
            f'  "files": [\n'
            f'    {{"path": "relative/path", "code": "The COMPLETE, updated file content. You MUST output the entire file so the system can save and test it. DO NOT USE SNIPPETS."}}\n'
            f'  ],\n'
            f'  "test_flight": "Step-by-step checklist for the human to verify your changes",\n'
            f'  "status": "in_progress OR complete",\n'
            f'  "recipient": "claude OR gemini"\n'
            f'}}\n'
            f'RULES FOR STATUS AND RECIPIENT:\n'
            f'1. If the overall feature requires both backend AND frontend work, the status is "in_progress".\n'
            f'2. If "in_progress", you MUST set "recipient" to the other agent and provide them instructions in "content".\n'
            f'3. Set status to "complete" ONLY if the entire full-stack feature is fully implemented and tested.\n'
            f'GEMINI HANDOFF STANDARD (Claude only — applies whenever recipient is "gemini"):\n'
            f'Your "content" field must be hyper-explicit. Gemini has zero project context beyond what you write — it cannot infer intent, look up files, or make assumptions. Every handoff MUST include ALL of the following:\n'
            f'(1) Exact file path(s) to modify and which files must NOT be touched.\n'
            f'(2) Exact component and function names.\n'
            f'(3) Exact prop names, types, and the values to pass.\n'
            f'(4) Exact state variable names with their useState initial values.\n'
            f'(5) Exact CSS class names and every inline style key-value pair.\n'
            f'(6) Exact API endpoint URLs with the full expected response shape (every field name and type).\n'
            f'(7) Exact JSX element types, nesting order, and where in the existing tree to insert them.\n'
            f'(8) Exact text strings for every label, placeholder, button, heading, and empty-state message.\n'
            f'(9) Exact conditional rendering logic written out in plain English or pseudocode.\n'
            f'Do NOT write "style appropriately", "add a button", "update the component", or any other vague instruction. Write exactly what to render, what to name it, and what value it must have.\n'
            f'CRITICAL JSON FORMATTING RULES — violating any of these will cause a parse failure and kill the handoff:\n'
            f'1. NEVER use triple-backtick code fences (```) inside any JSON field value. The parser uses triple-backticks to locate the JSON block — any inner ``` terminates the match early. Use single backticks or plain text for code examples inside field values.\n'
            f'2. NEVER include unescaped double-quotes inside a JSON string value. Use \\" or rewrite with single quotes.\n'
            f'3. NEVER include literal newlines inside a JSON string value. Use the \\n escape sequence instead.\n'
            f'4. NEVER output more than one ```json block in your response. The parser takes the first one — a second block will be ignored or cause confusion.\n'
            f'Never write files inside ai_team/, .git/, _archive/, _assets/, _tests/.'
        )
        clean_prompt = f"TASK: {task_text}\nCONTEXT: {context_text}{json_instruction}"

        # Initial AI Execution
        raw_reply = run_agent(
            GEMINI_CMD if agent_name == "gemini" else CLAUDE_CMD, clean_prompt,
            agent_name)

        # --- GEMINI FALLBACK ---
        # A "substantive" reply means the agent did real work before hitting any error.
        # If the reply is long (>3000 chars), the agent wrote files and we should NOT
        # re-run from scratch — instead fall through to parse_json which will trigger
        # the JSON-only retry ("Your files are already saved, just output the JSON").
        SUBSTANTIVE_THRESHOLD = 3000

        if raw_reply and agent_name == "gemini":
            reply_lower = raw_reply.lower()

            is_capacity_error = any(phrase in reply_lower for phrase in [
                "no capacity available", "model_capacity_exhausted",
                "resource_exhausted", "quota_exhausted",
                "exhausted your capacity"
            ])

            if is_capacity_error:
                if len(raw_reply) > SUBSTANTIVE_THRESHOLD:
                    log(
                        "orchestrator.log",
                        f"⚠️ {GEMINI_PRIMARY} hit capacity mid-run but reply is substantive "
                        f"({len(raw_reply)} chars) — skipping re-run, falling through to JSON-only retry."
                    )
                    # Fall through — parse_json will fail, triggering the JSON-only retry
                else:
                    log(
                        "orchestrator.log",
                        f"⚠️ {GEMINI_PRIMARY} capacity/quota exhausted! Falling back..."
                    )
                    fallback_cmd = [
                        "gemini", "-y", "-m", GEMINI_FALLBACK, "-p"
                    ] if GEMINI_FALLBACK else ["gemini", "-y", "-p"]
                    raw_reply = run_agent(fallback_cmd, clean_prompt,
                                          agent_name)

        # --- API ERROR & RATE LIMIT DETECTOR ---
        if raw_reply:
            reply_lower = raw_reply.lower()

            is_rate_limit = any(phrase in reply_lower for phrase in [
                "hit your limit", "rate limit", "no capacity available",
                "resource_exhausted", "quota_exhausted",
                "exhausted your capacity"
            ])
            is_overloaded = any(
                phrase in reply_lower for phrase in
                ["overloaded_error", "overloaded", "api error: 529", "529"])
            is_auth_error = "failed to authenticate" in reply_lower or "oauth token has expired" in reply_lower

            if is_overloaded and not is_rate_limit:
                log(
                    "orchestrator.log",
                    f"⏳ {agent_name.upper()} HIT OVERLOADED (529)! Pausing this agent for 10 minutes. Task stays in queue."
                )
                AGENT_COOLDOWNS[agent_name] = time.time() + (10 * 60)
                return

            if is_rate_limit or is_auth_error:
                if len(raw_reply) > SUBSTANTIVE_THRESHOLD:
                    # Agent did real work — the rate limit hit at the end, not the start.
                    # Don't re-run; the JSON-only retry below will recover the output.
                    log(
                        "orchestrator.log",
                        f"⚠️ Rate limit hit mid-run but reply is substantive "
                        f"({len(raw_reply)} chars) — skipping re-run, falling through to JSON-only retry."
                    )
                    # Fall through to parse_json
                elif is_rate_limit and agent_name == "gemini" and GEMINI_FALLBACK_2:
                    log(
                        "orchestrator.log",
                        f"⚠️ Rate limit hit! Trying {GEMINI_FALLBACK_2} as second fallback before cooldown..."
                    )
                    fallback2_cmd = [
                        "gemini", "-y", "-m", GEMINI_FALLBACK_2, "-p"
                    ]
                    raw_reply = run_agent(fallback2_cmd, clean_prompt,
                                          agent_name)
                    if raw_reply:
                        reply_lower = raw_reply.lower()
                        still_limited = any(
                            phrase in reply_lower for phrase in [
                                "hit your limit", "rate limit",
                                "no capacity available", "resource_exhausted",
                                "quota_exhausted", "exhausted your capacity"
                            ])
                        if still_limited:
                            log(
                                "orchestrator.log",
                                f"⏳ {GEMINI_FALLBACK_2} also rate-limited! Pausing gemini for 60 minutes. Task stays in queue."
                            )
                            AGENT_COOLDOWNS[agent_name] = time.time() + (60 *
                                                                         60)
                            return
                        log(
                            "orchestrator.log",
                            f"✅ {GEMINI_FALLBACK_2} succeeded! Continuing with fallback response."
                        )
                    else:
                        log(
                            "orchestrator.log",
                            f"⏳ {GEMINI_FALLBACK_2} returned no reply! Pausing gemini for 60 minutes. Task stays in queue."
                        )
                        AGENT_COOLDOWNS[agent_name] = time.time() + (60 * 60)
                        return
                else:
                    issue_type = "RATE LIMIT" if is_rate_limit else "AUTH ERROR"
                    log(
                        "orchestrator.log",
                        f"⏳ {agent_name.upper()} HIT {issue_type}! Pausing this agent for 60 minutes. Task stays in queue."
                    )
                    AGENT_COOLDOWNS[agent_name] = time.time() + (60 * 60)
                    return

        if not raw_reply or raw_reply.startswith('{"error":'):
            log(
                "orchestrator.log",
                f"⚠️ EMPTY/ERROR RESPONSE from {agent_name} — moving to failed queue"
            )
            if not DRY_RUN:
                _move_to_failed(f_path, msg.get("id", f_path.stem), agent_name,
                                "Empty or error response from agent")
            CURRENT_TASKS.pop(agent_name, None)
            update_status()
            trigger_failure_pause(
                f_path.name, f"Empty or error response from {agent_name}")
            continue

        reply_json = parse_json(raw_reply)

        # --- CATCH JSON PARSE FAILURES — retry once with a JSON-only prompt ---
        if reply_json.get("parse_error"):
            log(
                "orchestrator.log",
                f"⚠️ JSON parse failed for {agent_name} — retrying with JSON-only prompt..."
            )
            retry_prompt = (
                f"Your previous response was prose text without the required JSON output block. "
                f"The orchestrator cannot continue without it.\n\n"
                f"DO NOT redo any work. Your files are already saved on disk.\n\n"
                f"Output ONLY the JSON structure below — no preamble, no markdown prose, no explanation. "
                f"Start your response directly with the opening curly brace.\n"
                f"For any files you already wrote with Edit/Write tools, use 'ALREADY WRITTEN' as the code value.\n\n"
                f'{{"summary": "one-line summary of what was done", '
                f'"content": "instructions for the next agent, or empty string if complete", '
                f'"files": [{{"path": "relative/path/to/file", "code": "ALREADY WRITTEN"}}], '
                f'"test_flight": "step-by-step verification checklist", '
                f'"status": "in_progress or complete", '
                f'"recipient": "claude or gemini"}}')
            retry_cmd = GEMINI_CMD if agent_name == "gemini" else CLAUDE_CMD
            raw_retry = run_agent(retry_cmd, retry_prompt, agent_name)

            # If the retry itself hit a rate limit, try GEMINI_FALLBACK_2 before cooldown
            if raw_retry:
                retry_lower = raw_retry.lower()
                retry_rate_limited = any(phrase in retry_lower for phrase in [
                    "hit your limit", "rate limit", "no capacity available",
                    "resource_exhausted", "quota_exhausted",
                    "exhausted your capacity"
                ])
                if retry_rate_limited and agent_name == "gemini":
                    # Step 2: try auto (no -m flag)
                    log(
                        "orchestrator.log",
                        f"⏳ JSON-only retry hit a rate limit — trying auto-select for JSON output..."
                    )
                    raw_retry = run_agent(["gemini", "-y", "-p"], retry_prompt, agent_name)
                    if raw_retry:
                        retry_lower = raw_retry.lower()
                        retry_rate_limited = any(phrase in retry_lower for phrase in [
                            "hit your limit", "rate limit", "no capacity available",
                            "resource_exhausted", "quota_exhausted",
                            "exhausted your capacity"
                        ])
                    else:
                        retry_rate_limited = True
                    # Step 3: try GEMINI_FALLBACK_2
                    if retry_rate_limited and GEMINI_FALLBACK_2:
                        log(
                            "orchestrator.log",
                            f"⏳ Auto also rate-limited — trying {GEMINI_FALLBACK_2} for JSON output..."
                        )
                        raw_retry = run_agent(["gemini", "-y", "-m", GEMINI_FALLBACK_2, "-p"], retry_prompt, agent_name)
                        if raw_retry:
                            retry_lower = raw_retry.lower()
                            retry_rate_limited = any(phrase in retry_lower for phrase in [
                                "hit your limit", "rate limit", "no capacity available",
                                "resource_exhausted", "quota_exhausted",
                                "exhausted your capacity"
                            ])
                        else:
                            retry_rate_limited = True
                if retry_rate_limited:
                    log(
                        "orchestrator.log",
                        f"⏳ JSON-only retry exhausted all fallbacks — pausing {agent_name} for 60 minutes. Task stays in queue."
                    )
                    AGENT_COOLDOWNS[agent_name] = time.time() + (60 * 60)
                    continue
                reply_json = parse_json(raw_retry)

            if not raw_retry or reply_json.get("parse_error"):
                log(
                    "orchestrator.log",
                    f"⚠️ AI output was not valid JSON after retry. Moving {f_path.name} to FAILED queue."
                )
                if not DRY_RUN:
                    _move_to_failed(f_path, msg.get("id", f_path.stem),
                                    agent_name, "AI output was not valid JSON")
                CURRENT_TASKS.pop(agent_name, None)
                update_status()
                trigger_failure_pause(
                    f_path.name,
                    f"AI output was not valid JSON ({agent_name})")
                continue

        apply_changes(reply_json,
                      agent_name,
                      skip_review=msg.get("skip_review", False))

        log("orchestrator.log",
            f"🧪 Running Automated QA for {agent_name.upper()}...")
        qa_passed = True
        qa_error = ""

        if agent_name == "claude":
            qa_result = subprocess.run([
                "docker", "compose", "-f", "docker-compose.dev.yml", "exec",
                "-T", "backend", "python", "manage.py", "check"
            ],
                                       cwd=PROJECT_ROOT,
                                       capture_output=True,
                                       text=True)
            if qa_result.returncode != 0:
                qa_passed = False
                qa_error = f"{qa_result.stdout}\n{qa_result.stderr}".strip()

        elif agent_name == "gemini":
            # Run full frontend lint rather than per-file ESLint.
            # Per-file lint misses cross-file issues (undefined functions, hooks-after-return)
            # that appear when both agents touched the same file on diverged branches.
            qa_result = subprocess.run([
                "docker", "compose", "-f", "docker-compose.dev.yml", "exec",
                "-T", "frontend", "npm", "run", "lint"
            ],
                                       cwd=PROJECT_ROOT,
                                       capture_output=True,
                                       text=True)
            if qa_result.returncode != 0:
                qa_passed = False
                qa_error = f"{qa_result.stdout}\n{qa_result.stderr}".strip()

        if not qa_passed:
            log("orchestrator.log",
                f"💥 QA FAILED! Sending error back to {agent_name.upper()}.")

            fix_id = get_next_id()
            fix_task = {
                "id": fix_id,
                "from": "orchestrator",
                "to": agent_name,
                "kickback_reason": f"QA failure: build/lint broke",
                "retry_count": retry_count + 1,
                "task":
                f"CRITICAL ERROR: Your last code update broke the build. DO NOT hand off to anyone else until this is fixed. Analyze this error and fix the code:\n\n{qa_error[-1500:]}",
                "context": reply_json.get("summary", ""),
                "timestamp": datetime.now().isoformat()
            }

            inbox = INBOX_GEMINI if agent_name == "gemini" else INBOX_CLAUDE
            with open(inbox / f"kickback_{fix_id}.json", "w") as f:
                json.dump(fix_task, f, indent=2)
            _record_kickback(fix_id, agent_name,
                             "QA failure — build/lint broke")

            if not DRY_RUN:
                _move_to_processed(f_path)

            # Revert only the AI's files, not the entire working tree
            revert_agent_files(reply_json)
            CURRENT_TASKS.pop(agent_name, None)
            update_status()
            continue

        log("orchestrator.log", f"✅ QA PASSED.")

        # --- UI/UX SUPERVISOR REVIEW (Gemini tasks only) ---
        review_disabled = CLAUDE_REVIEW_DISABLED_SENTINEL.exists()
        if agent_name == "gemini" and not review_disabled and not msg.get(
                "skip_ui_review", False):
            ui_approved, ui_feedback = claude_ui_review()
            if not ui_approved:
                log("orchestrator.log",
                    f"🎨 UI/UX REVIEW FAILED — sending feedback to Gemini.")
                fix_id = get_next_id()
                fix_task = {
                    "id":
                    fix_id,
                    "from":
                    "claude_reviewer",
                    "to":
                    "gemini",
                    "kickback_reason":
                    "UI/UX review failure — Claude rejected Gemini's frontend changes",
                    "retry_count":
                    retry_count + 1,
                    "task":
                    ("UI/UX REVIEW FAILURE: Your frontend changes are already on disk. "
                     "Do NOT redo them from scratch. Read the current state of the files you changed, "
                     "then make ONLY the targeted fixes listed below. Do not touch anything else.\n\n"
                     f"Issues to fix:\n{ui_feedback}"),
                    "context":
                    reply_json.get("summary", ""),
                    "timestamp":
                    datetime.now().isoformat()
                }
                with open(INBOX_GEMINI / f"kickback_{fix_id}.json",
                          "w") as fix_f:
                    json.dump(fix_task, fix_f, indent=2)
                _record_kickback(
                    fix_id, "gemini",
                    "UI/UX review — Claude rejected frontend changes")
                if not DRY_RUN:
                    _move_to_processed(f_path)
                CURRENT_TASKS.pop(agent_name, None)
                update_status()
                continue

        test_flight = reply_json.get("test_flight")
        if test_flight:
            todo_filename = f"todo_{f_path.stem}_{agent_name}.md"
            todo_path = HUMAN_BOX / todo_filename

            with open(todo_path, "w", encoding="utf-8") as f:
                f.write(f"# Test Flight: {agent_name.upper()}\n")
                f.write(
                    f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                f.write(f"**Original Task:** {task_text[:200]}...\n\n")
                f.write("## Instructions\n")
                f.write(test_flight)

            log(
                "orchestrator.log",
                f"📋 Dropped test flight ticket into human_box: {todo_filename}"
            )

        # --- SCOPED GIT ADD: agent's domain + any root-level files explicitly submitted ---
        domain = "backend/" if agent_name == "claude" else "frontend/"
        run_git(["add", domain])
        for f in reply_json.get("files", []):
            if f and isinstance(f, dict) and "path" in f:
                p = f["path"].lstrip("/")
                if not p.startswith("frontend/") and not p.startswith(
                        "backend/"):
                    if not any(p.startswith(pp) for pp in PROTECTED_PATHS):
                        run_git(["add", p])

        run_git([
            "commit", "-m",
            f"Auto-Update ({agent_name}): {reply_json.get('summary', 'work in progress')}"
        ])

        # --- MERGE AGENT BRANCH INTO DEV ---
        task_summary = reply_json.get("summary", "task complete")
        log("orchestrator.log", f"🔀 Merging {branch} into {BRANCH_DEV}...")
        update_status(agent_name, f"Merging into {BRANCH_DEV}...")

        # Snapshot dev HEAD so we can hard-reset if post-merge QA fails
        dev_snap = run_git(["rev-parse", f"refs/heads/{BRANCH_DEV}"])
        dev_pre_merge_sha = dev_snap.stdout.strip(
        ) if dev_snap.returncode == 0 else None

        run_git(["checkout", BRANCH_DEV])
        merge_result = run_git([
            "merge", "--no-ff", branch, "-m", f"Merge {branch}: {task_summary}"
        ])

        if merge_result.returncode != 0:
            # Merge conflict — abort, return to agent branch, kickback
            merge_output = (merge_result.stdout + merge_result.stderr).strip()
            run_git(["merge", "--abort"])
            run_git(["checkout", branch])
            log(
                "orchestrator.log",
                f"💥 MERGE CONFLICT merging {branch} into {BRANCH_DEV}:\n{merge_output}"
            )
            conflict_id = get_next_id()
            conflict_task = {
                "id":
                conflict_id,
                "from":
                "orchestrator",
                "to":
                agent_name,
                "kickback_reason":
                f"Merge conflict: {branch} → {BRANCH_DEV}",
                "retry_count":
                retry_count + 1,
                "task":
                (f"MERGE CONFLICT: Your changes could not be automatically merged into {BRANCH_DEV}. "
                 f"This means your branch has diverged in a file that also changed on {BRANCH_DEV} "
                 f"(likely from the other agent's last task). "
                 f"Read the current state of {BRANCH_DEV} for the conflicting files, "
                 f"incorporate those changes into your work, and resubmit.\n\n"
                 f"Conflict details:\n{merge_output[-1200:]}"),
                "context":
                reply_json.get("summary", ""),
                "timestamp":
                datetime.now().isoformat()
            }
            inbox = INBOX_GEMINI if agent_name == "gemini" else INBOX_CLAUDE
            with open(inbox / f"kickback_{conflict_id}.json", "w") as kf:
                json.dump(conflict_task, kf, indent=2)
            _record_kickback(conflict_id, agent_name,
                             f"Merge conflict into {BRANCH_DEV}")
            if not DRY_RUN:
                _move_to_processed(f_path)
            CURRENT_TASKS.pop(agent_name, None)
            update_status()
            continue

        # --- POST-MERGE QA ON DEV ---
        log("orchestrator.log",
            f"🧪 Post-merge QA on {BRANCH_DEV} ({agent_name.upper()} task)...")
        update_status(agent_name, f"Post-merge QA on {BRANCH_DEV}...")
        dev_qa_errors = []

        backend_check = subprocess.run([
            "docker", "compose", "-f", "docker-compose.dev.yml", "exec", "-T",
            "backend", "python", "manage.py", "check"
        ],
                                       cwd=PROJECT_ROOT,
                                       capture_output=True,
                                       text=True)
        if backend_check.returncode != 0:
            dev_qa_errors.append(
                f"Backend (manage.py check):\n"
                f"{(backend_check.stdout + backend_check.stderr).strip()[:600]}"
            )

        frontend_lint = subprocess.run([
            "docker", "compose", "-f", "docker-compose.dev.yml", "exec", "-T",
            "frontend", "npm", "run", "lint"
        ],
                                       cwd=PROJECT_ROOT,
                                       capture_output=True,
                                       text=True)
        if frontend_lint.returncode != 0:
            dev_qa_errors.append(
                f"Frontend (npm run lint):\n"
                f"{(frontend_lint.stdout + frontend_lint.stderr).strip()[:600]}"
            )

        if dev_qa_errors:
            combined = "\n\n".join(dev_qa_errors)
            log(
                "orchestrator.log",
                f"💥 POST-MERGE QA FAILED on {BRANCH_DEV} — reverting merge...")
            if dev_pre_merge_sha:
                run_git(["reset", "--hard", dev_pre_merge_sha])
                log(
                    "orchestrator.log",
                    f"↩️  {BRANCH_DEV} reset to pre-merge state ({dev_pre_merge_sha[:8]})."
                )
            run_git(["checkout", branch])

            devqa_id = get_next_id()
            devqa_task = {
                "id":
                devqa_id,
                "from":
                "orchestrator",
                "to":
                agent_name,
                "kickback_reason":
                f"Post-merge QA failed on {BRANCH_DEV}",
                "retry_count":
                retry_count + 1,
                "task":
                (f"POST-MERGE QA FAILURE: Your changes passed per-branch QA but broke "
                 f"{BRANCH_DEV} after merging. The merge has been reverted. "
                 f"Fix the errors below on your branch and resubmit — do NOT hand off until clean.\n\n"
                 f"{combined[-1500:]}"),
                "context":
                reply_json.get("summary", ""),
                "timestamp":
                datetime.now().isoformat()
            }
            inbox = INBOX_GEMINI if agent_name == "gemini" else INBOX_CLAUDE
            with open(inbox / f"kickback_{devqa_id}.json", "w") as kf:
                json.dump(devqa_task, kf, indent=2)
            _record_kickback(devqa_id, agent_name,
                             f"Post-merge QA failed on {BRANCH_DEV}")
            if not DRY_RUN:
                _move_to_processed(f_path)
            CURRENT_TASKS.pop(agent_name, None)
            update_status()
            continue

        # Merge succeeded and dev QA is clean — update the poll tracker so
        # check_post_merge_qa() doesn't re-run QA for this same commit.
        global LAST_KNOWN_DEV_HEAD
        new_dev_head = run_git(["rev-parse", f"refs/heads/{BRANCH_DEV}"])
        if new_dev_head.returncode == 0:
            LAST_KNOWN_DEV_HEAD = new_dev_head.stdout.strip()

        log("orchestrator.log",
            f"✅ {branch} merged into {BRANCH_DEV} and post-merge QA passed.")

        # Return to agent branch for clean state before routing
        run_git(["checkout", branch])

        status = reply_json.get("status", "complete").lower()
        recipient = reply_json.get("recipient")

        if status == "in_progress" and recipient not in ["claude", "gemini"]:
            log(
                "orchestrator.log",
                f"⚠️ BATON DROPPED BY {agent_name.upper()}! Rejecting and forcing handoff."
            )

            bounce_id = get_next_id()
            bounce_task = {
                "id": bounce_id,
                "from": "orchestrator",
                "to": agent_name,
                "kickback_reason":
                "Baton dropped — marked in_progress with no valid recipient",
                "retry_count": retry_count + 1,
                "task":
                "SYSTEM RULE VIOLATION: You marked this task as 'in_progress' but failed to provide a valid 'recipient' (claude or gemini). Review the feature requirements. If the frontend needs to be updated to match your backend changes (or vice versa), you MUST set the recipient and instruct them on what to do. Fix your JSON output.",
                "context": reply_json.get("summary", ""),
                "timestamp": datetime.now().isoformat()
            }

            inbox = INBOX_GEMINI if agent_name == "gemini" else INBOX_CLAUDE
            with open(inbox / f"kickback_{bounce_id}.json", "w") as f:
                json.dump(bounce_task, f, indent=2)
            _record_kickback(
                bounce_id, agent_name,
                "Baton dropped — in_progress with no valid recipient")

            if not DRY_RUN:
                _move_to_processed(f_path)
            CURRENT_TASKS.pop(agent_name, None)
            update_status()
            continue

        if recipient and recipient in ["claude", "gemini"
                                       ] and status == "in_progress":
            # --- BATON DEPTH CIRCUIT BREAKER ---
            baton_depth = msg.get("baton_depth", 0) + 1
            if baton_depth > MAX_BATON_DEPTH:
                log(
                    "orchestrator.log",
                    f"🔄 BATON LOOP DETECTED! Depth {baton_depth} exceeds MAX_BATON_DEPTH ({MAX_BATON_DEPTH}). Routing to human inbox."
                )
                loop_alert_id = get_next_id()
                loop_alert = {
                    "id": loop_alert_id,
                    "from": "orchestrator",
                    "to": "human",
                    "task":
                    f"⚠️ BATON LOOP: The task chain between {agent_name} and {recipient} has exceeded {MAX_BATON_DEPTH} handoffs. Manual intervention required.\n\nLast summary: {reply_json.get('summary', 'N/A')}\n\nLast content: {str(reply_json.get('content', ''))[:500]}",
                    "timestamp": datetime.now().isoformat()
                }
                with open(HUMAN_BOX / f"loop_alert_{loop_alert_id}.json",
                          "w") as hf:
                    json.dump(loop_alert, hf, indent=2)
            else:
                target_inbox = INBOX_GEMINI if recipient == "gemini" else INBOX_CLAUDE
                new_id = get_next_id()
                handoff = {
                    "id": new_id,
                    "from": agent_name,
                    "to": recipient,
                    "baton_depth": baton_depth,
                    "task": reply_json.get("content")
                    or reply_json.get("task"),
                    "context": reply_json.get("summary"),
                    "timestamp": datetime.now().isoformat()
                }
                with open(target_inbox / f"handoff_{new_id}.json", "w") as hf:
                    json.dump(handoff, hf, indent=2)
                log(
                    "orchestrator.log",
                    f"📩 HANDOFF: {agent_name} -> {recipient} (depth {baton_depth})"
                )

        CURRENT_TASKS.pop(agent_name, None)
        if not DRY_RUN:
            _move_to_processed(f_path)
        log("orchestrator.log", f"✅ COMPLETED: {f_path.name}")
        update_status()


def main():
    global _session_start
    parser = argparse.ArgumentParser(
        description="FlashQuest AI Team Orchestrator")
    parser.add_argument(
        "-dcr",
        "--disable-claude-review",
        action="store_true",
        help="Disable Claude's UI/UX review of Gemini's frontend diffs")
    args = parser.parse_args()

    if args.disable_claude_review:
        CLAUDE_REVIEW_DISABLED_SENTINEL.touch()
        print("⚠️  Claude UI review DISABLED (sentinel created).")
    elif CLAUDE_REVIEW_DISABLED_SENTINEL.exists():
        # Explicitly not passing -dcr means review is ON — clear any stale sentinel
        CLAUDE_REVIEW_DISABLED_SENTINEL.unlink()

    os.system('clear')
    _session_start = time.time()
    log("orchestrator.log", "========================================")
    log("orchestrator.log", f" PROJECT AI TEAM ENGINE: ONLINE")
    log("orchestrator.log", f" PROJECT ROOT: {PROJECT_ROOT}")
    review_status = "DISABLED" if CLAUDE_REVIEW_DISABLED_SENTINEL.exists(
    ) else "ENABLED"
    log("orchestrator.log", f" Claude UI Review: {review_status}")
    log("orchestrator.log", "========================================")

    # 1. Check supporting services (ollama → qwen → discord)
    check_startup_services()

    # 2. Run the Pre-Flight Check
    perms_ok = check_permissions()

    global _queues_were_busy, _dev_qa_status

    while True:
        # --- PAUSE GUARD: halt until human removes the sentinel file ---
        if PAUSE_SENTINEL.exists():
            update_status()
            time.sleep(5)
            continue

        # Check for BOTH JSON and Markdown files
        h_files = list(INBOX_HUMAN.glob("*.json")) + list(
            INBOX_HUMAN.glob("*.md"))

        for hf in h_files:
            try:
                # --- MARKDOWN INGESTION ENGINE ---
                if hf.suffix.lower() == ".md":
                    with open(hf, "r", encoding="utf-8") as f:
                        content = f.read()

                    # Simple routing: If you type "To: Claude" anywhere in the markdown, it routes to Claude.
                    # Otherwise, it defaults to Gemini.
                    content_lower = content.lower()
                    target = "gemini" if "to: gemini" in content_lower else "claude"
                    skip_review = "no-review: true" in content_lower

                    msg = {
                        "from": "human",
                        "to": target,
                        "task": content,
                        "skip_review": skip_review,
                        "timestamp": datetime.now().isoformat()
                    }

                    target_inbox = INBOX_CLAUDE if target == "claude" else INBOX_GEMINI
                    new_filename = f"{hf.stem}.json"

                    # Wrap it in JSON and drop it in the AI queue
                    with open(target_inbox / new_filename,
                              "w",
                              encoding="utf-8") as out_f:
                        json.dump(msg, out_f, indent=2)

                    # Clean up the original markdown file
                    hf.unlink()
                    log("orchestrator.log",
                        f"👤 CONVERTED MD -> {target.upper()}: {new_filename}")
                    update_status()

                # --- STANDARD JSON ROUTING ---
                else:
                    with open(hf) as f:
                        msg = json.load(f)
                    target = msg.get("to", "claude").lower()

                    if target == "claude":
                        log("orchestrator.log",
                            f"👤 HUMAN REQUEST -> CLAUDE: {hf.name}")
                        shutil.move(hf, INBOX_CLAUDE / hf.name)
                    else:
                        log("orchestrator.log",
                            f"👤 HUMAN REQUEST -> GEMINI: {hf.name}")
                        shutil.move(hf, INBOX_GEMINI / hf.name)
                    update_status()

            except Exception as e:
                log("orchestrator.log",
                    f"❌ ERROR routing human task {hf.name}: {e}")

        process_queue(INBOX_GEMINI, "gemini")
        process_queue(INBOX_CLAUDE, "claude")

        # --- RUN THE CLEANUP SWEEP ---
        archive_processed_files()

        # --- POST-MERGE QA: detect new merge commits on main and lint the full frontend ---
        check_post_merge_qa()

        # --- IDLE QA: fire once when all queues drain after a busy period ---
        gemini_pending = list(INBOX_GEMINI.glob("*.json"))
        claude_pending = list(INBOX_CLAUDE.glob("*.json"))
        human_pending = list(INBOX_HUMAN.glob("*.json")) + list(
            INBOX_HUMAN.glob("*.md"))
        all_queues_idle = (
            len(gemini_pending) == 0 and len(claude_pending) == 0
            and len(human_pending) == 0
            and not (ACTIVE_PROCESS and ACTIVE_PROCESS.poll() is None))
        if _queues_were_busy and all_queues_idle:
            run_idle_dev_qa()
        _queues_were_busy = not all_queues_idle

        # Keep the backlog counter updated while idle, and pass the permission warning state
        if not ACTIVE_PROCESS:
            update_status(None, "", permission_warning=not perms_ok)

        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\n\n[!] Interrupt received (Ctrl+C). Shutting down orchestrator..."
        )

        # Check if an AI is currently running
        if ACTIVE_PROCESS and ACTIVE_PROCESS.poll() is None:
            try:
                # Ask the user what to do with the child process
                ans = input(
                    f"⚠️  An AI process (PID: {ACTIVE_PROCESS.pid}) is still running in the background. Kill it? [Y/n]: "
                )

                if ans.lower() != 'n':
                    ACTIVE_PROCESS.kill()
                    print(
                        f"🔪 Terminated AI process (PID: {ACTIVE_PROCESS.pid})."
                    )
                else:
                    print(
                        f"👻 Left PID {ACTIVE_PROCESS.pid} running in the background."
                    )

            except Exception:
                # If you spam Ctrl+C again during the prompt, it executes a hard kill
                ACTIVE_PROCESS.kill()
                print("\n🔪 Force terminated running AI process.")

        sys.exit(0)
