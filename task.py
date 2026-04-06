import json
import os
import sys
import time
import fcntl
from pathlib import Path
from datetime import datetime

from agents_config import AGENTS, AGENT_MAP

# --- ROBUST PATH RESOLUTION ---
AI_DIR = Path(__file__).parent.resolve()
AI_TEAM_MSG_DIR = AI_DIR / "messages"
INBOX = AI_TEAM_MSG_DIR / "inbox_tasks"
COUNTER_FILE = AI_TEAM_MSG_DIR / "counter.txt"
COUNTER_LOCK_FILE = AI_TEAM_MSG_DIR / "counter.lock"
TEAM_STATUS_FILE = AI_DIR / "TEAM_STATUS.md"

# --- SEAMLESS CO-EDITING FIX ---
os.umask(0o002)  # Ensures task files are group-writable


def get_next_id():
    """Return a zero-padded task ID. Uses a file lock to prevent races with orchestrator.py."""
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


def check_orchestrator_running():
    """Return True if TEAM_STATUS.md was updated within the last 60 seconds."""
    if not TEAM_STATUS_FILE.exists():
        return False
    age_seconds = time.time() - TEAM_STATUS_FILE.stat().st_mtime
    return age_seconds < 60


def check_review_ai_running():
    """Return True if the ollama service (code review AI) is active."""
    import subprocess
    # Primary: systemctl (systemd systems)
    result = subprocess.run(["systemctl", "is-active", "ollama"],
                            capture_output=True,
                            text=True)
    if result.stdout.strip() == "active":
        return True
    # Fallback: look for any ollama process
    result = subprocess.run(["pgrep", "-f", "ollama"],
                            capture_output=True,
                            text=True)
    return result.returncode == 0


def check_discord_running():
    """Return True if the Discord monitor bot process is running."""
    import subprocess
    result = subprocess.run(["pgrep", "-f", "discord_bot.py"],
                            capture_output=True,
                            text=True)
    return result.returncode == 0


def create_task(task_text, target, skip_review=False, skip_ui_review=False):
    task_id = get_next_id()

    INBOX.mkdir(parents=True, exist_ok=True)
    path = INBOX / f"task_{task_id}.md"

    # Write a clean Markdown file instead of JSON
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"To: {target.capitalize()}\n")
        f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if skip_review:
            f.write(f"No-Review: true\n")
        if skip_ui_review:
            f.write(f"Skip-UI-Review: true\n")
        f.write(f"---\n\n")
        f.write(task_text)

    notes = []
    if skip_review:
        notes.append("code review skipped")
    if skip_ui_review:
        notes.append("UI review skipped")
    note_str = f" ({', '.join(notes)})" if notes else ""
    print(f"🚀 Task sent to {target.upper()}: task_{task_id}.md{note_str}")


REVIEW_CMD = ["ollama", "run", "qwen-reviewer"]
PROJECT_ROOT = AI_DIR.parent


def run_audit(filepath=None):
    """Run a full Qwen review on a specific file or all files changed vs main."""
    import re
    import subprocess as sp

    if filepath:
        targets = [(filepath, PROJECT_ROOT / filepath)]
    else:
        result = sp.run(["git", "diff", "main...HEAD", "--name-only"],
                        capture_output=True,
                        text=True,
                        cwd=PROJECT_ROOT)
        names = [f for f in result.stdout.strip().splitlines() if f.strip()]
        if not names:
            print("✅ No changed files found vs main.")
            return
        targets = [(name, PROJECT_ROOT / name) for name in names]

    if not check_review_ai_running():
        print("❌ ollama/qwen-reviewer does not appear to be running.")
        print("   Start it with: sudo systemctl start ollama")
        sys.exit(1)

    print(f"🔍 Auditing {len(targets)} file(s)...\n")
    for rel, fp in targets:
        if not fp.exists():
            print(f"⚠️  {rel}: file not found, skipping.")
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️  {rel}: could not read — {e}")
            continue

        print(f"  → {rel}")
        try:
            proc = sp.run(REVIEW_CMD +
                          [f"Review this code:\n{content[:8000]}"],
                          capture_output=True,
                          text=True,
                          timeout=180)
            raw = proc.stdout or proc.stderr or "(no output)"
        except sp.TimeoutExpired:
            raw = "Timed out."
        except Exception as e:
            raw = str(e)

        clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', raw).strip()
        print(f"     {clean[:600]}\n")


if __name__ == "__main__":
    args = sys.argv[1:]

    # --- AUDIT MODE ---
    audit_flag = "-a" in args or "--audit" in args
    if audit_flag:
        flag_index = args.index("-a") if "-a" in args else args.index(
            "--audit")
        audit_path = args[flag_index + 1] if flag_index + 1 < len(
            args) and not args[flag_index + 1].startswith("-") else None
        run_audit(audit_path)
        sys.exit(0)

    # Build routing flags from configured agents: first letter as short flag, full name as long flag
    # e.g. AGENTS = [claude(backend), gemini(frontend)] → -c/--claude → "claude", -g/--gemini → "gemini"
    _agent_flags = {}
    for _a in AGENTS:
        _agent_flags[f"-{_a.name[0]}"] = _a.name
        _agent_flags[f"--{_a.name}"] = _a.name

    _flag_display = " | ".join(
        f"-{a.name[0]}|--{a.name}" for a in AGENTS
    )
    _default_agent = AGENTS[0].name

    # Require at least one argument (the task text, with optional routing flag)
    if len(args) < 1:
        print(f"Usage: python ai_team/task.py ['Your task here'] [{_flag_display}] [-nr|--no-review] [-dcr|--disable-ui-review]")
        print(f"       python ai_team/task.py -a [optional/path/to/file]  (audit mode)")
        print(f"       Default agent: {_default_agent.capitalize()} (omit flag to send to {_default_agent.capitalize()})")
        print(f"       -dcr skips UI review of the reviewed agent's output for this task")
        sys.exit(1)

    # Extract -nr / --no-review flag (position-independent)
    no_review = "-nr" in args or "--no-review" in args
    args = [a for a in args if a not in ("-nr", "--no-review")]

    # Extract -dcr / --disable-ui-review flag (position-independent)
    no_ui_review = "-dcr" in args or "--disable-claude-review" in args or "--disable-ui-review" in args
    args = [a for a in args if a not in ("-dcr", "--disable-claude-review", "--disable-ui-review")]

    if len(args) < 1:
        print(f"Usage: python ai_team/task.py ['Your task here'] [{_flag_display}] [-nr|--no-review] [-dcr|--disable-ui-review]")
        sys.exit(1)

    # Detect optional routing flag as the first argument
    if args[0].lower() in _agent_flags:
        target_agent = _agent_flags[args[0].lower()]
        if len(args) < 2:
            print("❌ Error: No task text provided after routing flag.")
            sys.exit(1)
        task_text = args[1]
    else:
        # No flag — default to first agent
        target_agent = _default_agent
        task_text = args[0]

    # --- ORCHESTRATOR RUNNING CHECK ---
    if not check_orchestrator_running():
        print("\n⚠️  WARNING: The orchestrator does not appear to be running.")
        print(
            "   (TEAM_STATUS.md is missing or hasn't been updated in 60+ seconds)"
        )
        print("   Start it with: python ai_team/orchestrator.py")
        ans = input("   Continue anyway? [y/N]: ")
        if ans.lower() not in ['y', 'yes']:
            print("Task cancelled.")
            sys.exit(0)

    # --- CODE REVIEW AI CHECK ---
    if not no_review and not check_review_ai_running():
        print(
            "\n⚠️  WARNING: The code review AI (ollama/qwen-reviewer) does not appear to be running."
        )
        print("   Reviews will be skipped or fail during task execution.")
        print("   Start it with: sudo systemctl start ollama")
        print("   Or bypass review with: --no-review")
        ans = input("   Continue anyway? [y/N]: ")
        if ans.lower() not in ['y', 'yes']:
            print("Task cancelled.")
            sys.exit(0)

    # --- DISCORD BOT STATUS (informational) ---
    if check_discord_running():
        print("🔔 Discord monitor: running")
    else:
        print(
            "🔕 Discord monitor: not running  (start with: python ai_team/discord_bot.py)"
        )

    # --- GIT SAFEGUARD PROMPT ---
    print("\n🛡️  Git State Check")
    ans = input(
        "Have you committed or stashed your recent changes on the main branch? [y/N]: "
    )

    if ans.lower() not in ['y', 'yes']:
        print(
            "🛑 Task cancelled. Please run `git status` and manage your working tree before assigning new AI tasks!"
        )
        sys.exit(0)

    create_task(task_text,
                target_agent,
                skip_review=no_review,
                skip_ui_review=no_ui_review)
