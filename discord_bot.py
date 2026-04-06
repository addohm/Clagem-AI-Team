"""
FlashQuest AI Team — Discord Monitor
=====================================
Multi-channel Discord monitor for the AI development team.

Channels expected (create these in your Discord server):
  #logs    — Streamed log entries from orchestrator, claude, gemini
  #tasks   — New tasks routed to inbox_claude / inbox_gemini
  #todos   — Test flight tickets and alerts from outbox_human
  #status  — Live TEAM_STATUS.md dashboard (channel renamed with status emoji)

Setup:
  1. Add to your .env:
       DISCORD_BOT_TOKEN=your_bot_token_here
       DISCORD_LOGS_CHANNEL_ID=111111111111111111
       DISCORD_TASKS_CHANNEL_ID=222222222222222222
       DISCORD_TODOS_CHANNEL_ID=333333333333333333
       DISCORD_STATUS_CHANNEL_ID=444444444444444444

  2. Install:
       pip install "discord.py>=2.0" python-dotenv

  3. Run:
       python ai_team/discord_bot.py
"""

import asyncio
import fcntl
import subprocess
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Fix SSL cert resolution for Python 3.14+ on systems where the default cert
# bundle doesn't satisfy aiohttp's TLS handshake with Discord.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from agents_config import AGENTS, AGENT_MAP, REVIEW_DISABLED_SENTINEL

# --- PATH RESOLUTION ---
AI_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = AI_DIR.parent.resolve()

# --- LOGGING SETUP (writes to logs/discord_bot.log regardless of launch method) ---
_log_path = AI_DIR / "logs" / "discord_bot.log"
_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("discord_bot")

load_dotenv(AI_DIR / ".env")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
LOGS_CHANNEL_ID = int(os.environ.get("DISCORD_LOGS_CHANNEL_ID", "0"))
TASKS_CHANNEL_ID = int(os.environ.get("DISCORD_TASKS_CHANNEL_ID", "0"))
TODOS_CHANNEL_ID = int(os.environ.get("DISCORD_TODOS_CHANNEL_ID", "0"))
STATUS_CHANNEL_ID = int(os.environ.get("DISCORD_STATUS_CHANNEL_ID", "0"))

if not TOKEN or not all(
    [LOGS_CHANNEL_ID, TASKS_CHANNEL_ID, TODOS_CHANNEL_ID, STATUS_CHANNEL_ID]):
    print("❌  Missing one or more Discord env vars. Check your .env.")
    print("    Required: DISCORD_BOT_TOKEN, DISCORD_LOGS_CHANNEL_ID,")
    print("              DISCORD_TASKS_CHANNEL_ID, DISCORD_TODOS_CHANNEL_ID,")
    print("              DISCORD_STATUS_CHANNEL_ID")
    sys.exit(1)

# --- FILE PATHS ---
LOG_FILES = {
    "ORCHESTRATOR": AI_DIR / "logs" / "orchestrator.log",
    **{a.name.upper(): a.log_file for a in AGENTS},
    "REVIEWER": AI_DIR / "logs" / "review.log",
}
STATUS_FILE = AI_DIR / "TEAM_STATUS.md"
OUTBOX_HUMAN = AI_DIR / "messages" / "outbox_human"
INBOX_TASKS = AI_DIR / "messages" / "inbox_tasks"
PROCESSED = AI_DIR / "messages" / "processed"
COUNTER_FILE = AI_DIR / "messages" / "counter.txt"
COUNTER_LOCK = AI_DIR / "messages" / "counter.lock"
# REVIEW_DISABLED_SENTINEL imported from agents_config

# --- LOG FILTER (lines to suppress from #logs) ---
SKIP_PATTERNS = [
    "⚙️ Spawning",
    "🧐 QWEN REVIEWING:",
    "⏩ REVIEW SKIPPED",
    "💾 ",
    "↩️ Reverted",
    "🗑️ Deleted",
]

# Lines that trigger a @here ping in #logs
ALERT_PATTERNS = [
    "❌",
    "🚨",
    "🔄 BATON LOOP",
    "🚫 MAX RETRIES",
    "PERMANENTLY FAILING",
]

# Status channel name suffix (we prepend the emoji)
STATUS_CHANNEL_BASE = "status"

# --- EMBED COLORS ---
# Agent colors derived from agents_config.py — edit there, not here.
COLOR_ORCHESTRATOR = discord.Color.from_str("#F59E0B")  # amber
COLOR_REVIEWER = discord.Color.from_str("#9333EA")  # purple
COLOR_TODO = discord.Color.green()
COLOR_ALERT = discord.Color.red()
COLOR_TASK = discord.Color.blurple()

# --- STATE ---
_log_offsets: dict[str, int] = {}  # label -> byte offset
_seen_outbox: set[str] = set()
_seen_tasks: set[str] = set()
_status_msg_id: int | None = None
_last_channel_status: str = ""  # "green" | "yellow" | "red"
_last_alert_time: float = 0.0  # epoch — throttle @here pings
ALERT_COOLDOWN = 300  # seconds between @here pings

STATE_FILE = AI_DIR / "messages" / "discord_state.json"


def _load_state():
    """Load persisted offsets and seen-sets from disk."""
    global _log_offsets, _seen_outbox, _seen_tasks, _status_msg_id
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        _log_offsets = data.get("log_offsets", {})
        _seen_outbox = set(data.get("seen_outbox", []))
        _seen_tasks = set(data.get("seen_tasks", []))
        _status_msg_id = data.get("status_msg_id")
    except Exception:
        pass  # corrupt state — start fresh


def _save_state():
    """Persist current offsets and seen-sets to disk."""
    try:
        STATE_FILE.write_text(
            json.dumps(
                {
                    "log_offsets": _log_offsets,
                    "seen_outbox": list(_seen_outbox),
                    "seen_tasks": list(_seen_tasks),
                    "status_msg_id": _status_msg_id,
                },
                indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = 1990) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…*(truncated)*"


def _is_alert(lines: list[str]) -> bool:
    return any(any(p in l for p in ALERT_PATTERNS) for l in lines)


def _read_new_lines(label: str) -> list[str]:
    path = LOG_FILES[label]
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(_log_offsets.get(label, 0))
        new = f.read()
        _log_offsets[label] = f.tell()
    lines = [l.rstrip() for l in new.splitlines() if l.strip()]
    return [l for l in lines if not any(p in l for p in SKIP_PATTERNS)]


def _parse_channel_status() -> str:
    """
    Derive 🔴 / 🟡 / 🟢 from TEAM_STATUS.md content and file freshness.
    🔴  = orchestrator appears stopped (file stale >3 min) or has failed tasks
    🟡  = at least one agent is actively working
    🟢  = all agents idle, nothing in progress
    """
    import time
    if not STATUS_FILE.exists():
        return "red"

    age = time.time() - STATUS_FILE.stat().st_mtime
    if age > 1800:  # 30 minutes — orchestrator likely crashed
        return "red"

    content = STATUS_FILE.read_text(encoding="utf-8", errors="replace")

    # Active work → yellow
    if "🟡 In Progress" in content or "WORKING" in content:
        return "yellow"

    # Failed tasks → red
    if "## ❌ Failed" in content:
        lines = [l for l in content.splitlines() if l.startswith("- [")]
        if lines:
            return "red"

    return "green"


def _status_emoji(state: str) -> str:
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(state, "🔴")


def _format_task(data: dict) -> str:
    """Render a task JSON dict as clean markdown."""
    from_agent = data.get("from", "?").upper()
    to_agent = data.get("to", "?").upper()
    ts = data.get("timestamp", "")[:19].replace("T", " ")
    task_body = data.get("task", "*(no content)*").strip()
    context = data.get("context", "")
    baton = data.get("baton_depth")
    skip_rev = data.get("skip_review", False)

    lines = [
        f"**From:** {from_agent}  →  **To:** {to_agent}",
        f"**Time:** {ts}",
    ]
    if baton:
        lines.append(f"**Handoff depth:** {baton}")
    if skip_rev:
        lines.append("**Code review:** skipped (`-nr`)")
    lines.append("")
    lines.append(task_body)
    if context:
        lines.append(f"\n---\n**Context:** {context}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TASK CREATION HELPERS
# ---------------------------------------------------------------------------


def _get_next_task_id() -> str:
    """Thread-safe counter using the same file-lock as task.py."""
    INBOX_TASKS.mkdir(parents=True, exist_ok=True)
    with open(COUNTER_LOCK, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            count = int(COUNTER_FILE.read_text().strip()
                        ) + 1 if COUNTER_FILE.exists() else 1
            COUNTER_FILE.write_text(str(count))
            return f"{count:04d}"
        except (ValueError, OSError):
            COUNTER_FILE.write_text("1")
            return "0001"
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _write_task(task_text: str, target: str, skip_review: bool, skip_ui_review: bool = False) -> str:
    """Write a task .md file to inbox_tasks/ and return the file path."""
    task_id = _get_next_task_id()
    path = INBOX_TASKS / f"task_{task_id}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"To: {target.capitalize()}\n")
        f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if skip_review:
            f.write("No-Review: true\n")
        if skip_ui_review:
            f.write("Skip-UI-Review: true\n")
        f.write("---\n\n")
        f.write(task_text)
    return path.name


# ---------------------------------------------------------------------------
# BACKGROUND TASKS
# ---------------------------------------------------------------------------
#🍺📜🏰🤝


@tasks.loop(seconds=10)
async def watch_logs():
    """Tail each log file and post new lines to #logs."""
    try:
        channel = bot.get_channel(LOGS_CHANNEL_ID)
        if not channel:
            return

        for label, color in (
            [("ORCHESTRATOR", COLOR_ORCHESTRATOR)]
            + [(a.name.upper(), discord.Color(a.discord_color)) for a in AGENTS]
            + [("REVIEWER", COLOR_REVIEWER)]
        ):
            lines = _read_new_lines(label)
            if not lines:
                continue

            alert = _is_alert(lines)
            # Batch into chunks that fit Discord's limit
            chunk: list[str] = []
            chunk_len = 0
            for line in lines:
                if chunk_len + len(line) + 1 > 1900:
                    await _flush_log_chunk(channel, label, color, chunk, alert)
                    chunk, chunk_len = [], 0
                    alert = False  # only ping once per batch
                chunk.append(line)
                chunk_len += len(line) + 1
            if chunk:
                await _flush_log_chunk(channel, label, color, chunk, alert)

        _save_state()
    except Exception as e:
        logger.warning(f"[watch_logs] error: {e}")


@watch_logs.error
async def watch_logs_error(error):
    logger.warning(f"[watch_logs] fatal error: {error} — restarting")
    watch_logs.restart()


async def _flush_log_chunk(channel, label, color, lines, alert):
    global _last_alert_time
    import time
    text = "\n".join(lines)
    embed = discord.Embed(
        description=f"```\n{_truncate(text)}\n```",
        color=color,
    )
    embed.set_author(name=f"[{label}]")
    now = time.time()
    ping = ""
    if alert and (now - _last_alert_time) > ALERT_COOLDOWN:
        ping = "@here\n"
        _last_alert_time = now
    await channel.send(ping, embed=embed)


@tasks.loop(seconds=6)
async def watch_tasks():
    """Post new tasks from inbox_claude / inbox_gemini to #tasks."""
    global _seen_tasks
    try:
        channel = bot.get_channel(TASKS_CHANNEL_ID)
        if not channel:
            return

        for inbox, label, color in [
            (a.inbox, a.name.upper(), discord.Color(a.discord_color))
            for a in AGENTS
        ]:
            if not inbox.exists():
                continue
            for fp in sorted(inbox.iterdir(), key=lambda x: x.stat().st_mtime):
                if fp.suffix != ".json" or fp.name in _seen_tasks:
                    continue
                _seen_tasks.add(fp.name)
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:
                    continue

                body = _format_task(data)
                embed = discord.Embed(
                    title=f"📬 New Task → {label}",
                    description=_truncate(body, 3900),
                    color=color,
                    timestamp=datetime.now(),
                )
                embed.set_footer(text=fp.name)
                await channel.send(embed=embed)

        _save_state()
    except Exception as e:
        logger.warning(f"[watch_tasks] error: {e}")


@watch_tasks.error
async def watch_tasks_error(error):
    logger.warning(f"[watch_tasks] fatal error: {error} — restarting")
    watch_tasks.restart()


@tasks.loop(seconds=8)
async def watch_todos():
    """Post new outbox_human items to #todos."""
    global _seen_outbox
    try:
        channel = bot.get_channel(TODOS_CHANNEL_ID)
        if not channel:
            return

        if not OUTBOX_HUMAN.exists():
            return

        for fp in sorted(OUTBOX_HUMAN.iterdir(),
                         key=lambda x: x.stat().st_mtime):
            if fp.name in _seen_outbox:
                continue
            _seen_outbox.add(fp.name)
            try:
                content = fp.read_text(encoding="utf-8")
            except Exception:
                continue

            is_alert = "loop_alert" in fp.name or "LOOP" in content.upper()
            embed = discord.Embed(
                title="🚨 Loop Alert — Human Intervention Required"
                if is_alert else "📋 Test Flight Ready",
                description=_truncate(content, 3900),
                color=COLOR_ALERT if is_alert else COLOR_TODO,
                timestamp=datetime.now(),
            )
            embed.set_footer(text=fp.name)
            ping = "@here\n" if is_alert else ""
            await channel.send(ping, embed=embed)

        _save_state()
    except Exception as e:
        logger.warning(f"[watch_todos] error: {e}")


@watch_todos.error
async def watch_todos_error(error):
    logger.warning(f"[watch_todos] fatal error: {error} — restarting")
    watch_todos.restart()


async def _reconcile_todos_channel():
    """
    Sync #todos with outbox_human:
      - Delete embeds whose source file is gone.
      - If remaining embeds are out of file-creation order, delete and re-queue
        them so watch_todos re-posts in the correct sequence.
    """
    global _seen_outbox
    channel = bot.get_channel(TODOS_CHANNEL_ID)
    if not channel:
        return
    try:
        # Collect bot todo embeds (history returns newest-first)
        valid = []  # [(message, Path)] newest → oldest
        async for message in channel.history(limit=100):
            if message.author.id != bot.user.id or not message.embeds:
                continue
            footer = message.embeds[0].footer
            if not footer or not footer.text:
                continue
            fp = OUTBOX_HUMAN / footer.text
            if not fp.exists():
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound):
                    pass
            else:
                valid.append((message, fp))

        # Find files in outbox_human that have no embed in Discord and re-queue them
        posted_names = {fp.name for _, fp in valid}
        requeued = 0
        if OUTBOX_HUMAN.exists():
            for fp in OUTBOX_HUMAN.iterdir():
                if fp.name in _seen_outbox and fp.name not in posted_names:
                    _seen_outbox.discard(fp.name)
                    requeued += 1
        if requeued:
            _save_state()
            logger.info(
                f"[reconcile_todos] re-queued {requeued} unposted todo(s)")

    except Exception as e:
        logger.info(f"[reconcile_todos] error: {e}")


@tasks.loop(seconds=15)
async def watch_status():
    """Edit the pinned status message and rename the channel."""
    global _status_msg_id, _last_channel_status
    import time

    try:
        if not STATUS_FILE.exists():
            return

        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if not channel:
            return

        content = STATUS_FILE.read_text(encoding="utf-8", errors="replace")
        state = _parse_channel_status()
        emoji = _status_emoji(state)
        new_name = f"{emoji}・{STATUS_CHANNEL_BASE}"

        # --- Update pinned message ---
        embed = discord.Embed(
            description=_truncate(content, 3900),
            color={
                "green": discord.Color(AGENTS[-1].discord_color),
                "yellow": COLOR_ORCHESTRATOR,
                "red": COLOR_ALERT
            }[state],
            timestamp=datetime.now(),
        )
        embed.set_footer(text="Last updated")

        if _status_msg_id:
            try:
                msg = await channel.fetch_message(_status_msg_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                _status_msg_id = None
            except Exception as e:
                logger.warning(
                    f"[watch_status] failed to edit pinned message: {e}")
                _status_msg_id = None

        if not _status_msg_id:
            # Purge all previous bot messages from the channel before posting
            try:
                await channel.purge(
                    limit=50,
                    check=lambda m: m.author.id == bot.user.id,
                )
            except Exception:
                pass
            msg = await channel.send(embed=embed)
            _status_msg_id = msg.id
            _save_state()

        todos_channel = bot.get_channel(TODOS_CHANNEL_ID)

        # --- Remove idle notification when team goes back to work ---
        if state != "green" and _last_channel_status == "green" and todos_channel:
            try:
                await todos_channel.purge(
                    limit=100,
                    check=lambda m: m.author.id == bot.user.id and
                    "AI team is idle" in m.content,
                )
            except Exception:
                pass

        # --- Notify on transition to idle (exactly one message, no history) ---
        if state == "green" and _last_channel_status and _last_channel_status != "green":
            if todos_channel:
                # Remove any previous idle messages before posting the fresh one
                try:
                    await todos_channel.purge(
                        limit=100,
                        check=lambda m: m.author.id == bot.user.id and
                        "AI team is idle" in m.content,
                    )
                except Exception:
                    pass
                await todos_channel.send(
                    "@everyone 🟢 AI team is idle — ready for the next task.")

        # --- Rename channel on every state change ---
        if state != _last_channel_status:
            _last_channel_status = state  # update now so idle notify fires exactly once
            await _reconcile_todos_channel()
            try:
                await channel.edit(name=new_name)
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass  # rate limited — channel name may lag, state tracking is correct

    except Exception as e:
        logger.warning(f"[watch_status] error: {e}")


@watch_status.error
async def watch_status_error(error):
    logger.warning(f"[watch_status] fatal error: {error} — restarting")
    watch_status.restart()


@tasks.loop(minutes=30)
async def clean_tasks_channel():
    """Delete any non-bot messages from the #tasks channel."""
    channel = bot.get_channel(TASKS_CHANNEL_ID)
    if not channel:
        return
    try:
        deleted = await channel.purge(
            limit=100,
            check=lambda m: m.author.id != bot.user.id,
        )
        if deleted:
            logger.info(
                f"[clean_tasks_channel] removed {len(deleted)} stray message(s)"
            )
    except Exception as e:
        logger.warning(f"[clean_tasks_channel] error: {e}")


@clean_tasks_channel.error
async def clean_tasks_channel_error(error):
    logger.warning(f"[clean_tasks_channel] fatal error: {error} — restarting")
    clean_tasks_channel.restart()


# ---------------------------------------------------------------------------
# BOT SETUP
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    global _log_offsets, _seen_outbox, _seen_tasks, _status_msg_id

    # Load persisted state so restarts don't re-post already-seen content
    _load_state()

    # For any log file with no saved offset (first run), seed to current EOF
    for label, path in LOG_FILES.items():
        if label not in _log_offsets:
            _log_offsets[label] = path.stat().st_size if path.exists() else 0

    # Sanity-check offsets: if a log was rotated/truncated, reset to 0
    for label, path in LOG_FILES.items():
        if path.exists() and _log_offsets.get(label, 0) > path.stat().st_size:
            _log_offsets[label] = 0

    _save_state()

    # Clean up any stray messages in #tasks left while bot was offline
    tasks_channel = bot.get_channel(TASKS_CHANNEL_ID)
    if tasks_channel:
        try:
            deleted = await tasks_channel.purge(
                limit=100,
                check=lambda m: m.author.id != bot.user.id,
            )
            if deleted:
                logger.info(
                    f"[on_ready] cleared {len(deleted)} stray message(s) from #tasks"
                )
        except Exception as e:
            logger.warning(f"[on_ready] tasks channel cleanup error: {e}")

    # Reconcile #todos against outbox_human on startup
    await _reconcile_todos_channel()

    # Seed channel status so we don't re-fire the idle notification on restart
    _last_channel_status = _parse_channel_status()

    # Purge any stale idle notifications from #todos (keep channel clean on restart)
    todos_channel = bot.get_channel(TODOS_CHANNEL_ID)
    if todos_channel:
        try:
            await todos_channel.purge(
                limit=100,
                check=lambda m: m.author.id == bot.user.id and
                "AI team is idle" in m.content,
            )
        except Exception as e:
            logger.warning(f"[on_ready] idle message cleanup error: {e}")

    # Start all watchers
    watch_logs.start()
    watch_tasks.start()
    watch_todos.start()
    watch_status.start()
    clean_tasks_channel.start()

    # Announce in logs channel
    logs_channel = bot.get_channel(LOGS_CHANNEL_ID)
    if logs_channel:
        _watching = " · ".join(["orchestrator"] + [a.name for a in AGENTS])
        await logs_channel.send(
            f"🟢 **FlashQuest AI Monitor online** — "
            f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"Watching: {_watching}")

    logger.info(f"✅  Discord monitor running as {bot.user}")


@bot.event
async def on_raw_reaction_add(payload):
    """Mark a #todos item complete when reacted with ✅."""
    if payload.channel_id != TODOS_CHANNEL_ID:
        return
    if payload.user_id == bot.user.id:
        return
    if str(payload.emoji) != "✅":
        return

    channel = bot.get_channel(TODOS_CHANNEL_ID)
    if not channel:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden):
        return

    # Only act on bot embeds that have a filename in the footer
    if message.author.id != bot.user.id or not message.embeds:
        return
    footer = message.embeds[0].footer
    if not footer or not footer.text:
        return

    filename = footer.text
    src = OUTBOX_HUMAN / filename
    if not src.exists():
        return

    PROCESSED.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(PROCESSED / filename))
    logger.info(f"[todos] ✅ marked complete: {filename}")

    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass


# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------


@bot.command(name="dcr")
async def cmd_dcr(ctx):
    """Toggle the reviewer agent's UI/UX review of the reviewed agent's diffs on or off.
    Usage: !dcr"""
    if REVIEW_DISABLED_SENTINEL.exists():
        REVIEW_DISABLED_SENTINEL.unlink()
        await ctx.send(
            "✅ **UI review ENABLED** — frontend diffs will be reviewed before commit."
        )
    else:
        REVIEW_DISABLED_SENTINEL.touch()
        await ctx.send(
            "⚠️ **UI review DISABLED** — frontend diffs will skip review until re-enabled."
        )


@bot.command(name="help")
async def cmd_help(ctx):
    embed = discord.Embed(title="FlashQuest AI Monitor",
                          color=COLOR_ORCHESTRATOR)
    _flag_hint = " | ".join(f"-{a.name[0]}" for a in AGENTS)
    embed.add_field(name=f"!task <{_flag_hint}> [-nr] [-dcr] <text>",
                    value=f"Queue a task. {' '.join(f'`-{a.name[0]}` → {a.name.capitalize()}' for a in AGENTS)}, `-nr` skips code review, `-dcr` skips UI review",
                    inline=False)
    embed.add_field(name="!logs [n]",
                    value="Last n orchestrator log lines (default 20)",
                    inline=False)
    embed.add_field(name="!tasks", value="Queue depth per inbox", inline=False)
    embed.add_field(
        name="!dcr",
        value="Toggle Claude's UI/UX review of Gemini diffs on/off (global)",
        inline=False)
    await ctx.send(embed=embed)


@bot.command(name="task")
async def cmd_task(ctx):
    """
    Task submission. Usage: !task <-c|--claude|-g|--gemini> [-nr] <text>
    A routing flag is required; bare !task with no flag is rejected.
    Parses ctx.message.content directly to handle backticks, code blocks, and
    multi-line messages without tripping the framework's shlex tokenizer.
    """
    original_content = ctx.message.content

    async def _reject():
        """DM the author that their message was rejected (no reason given)."""
        _flags_hint = " / ".join(f"-{a.name[0]}" for a in AGENTS)
        try:
            await ctx.author.send(
                f"the message has been rejected (check routing flag: {_flags_hint})")
            logger.info(
                f"[task] rejection DM sent to {ctx.author} for: {original_content!r}"
            )
        except discord.Forbidden:
            logger.warning(
                f"[task] rejection DM blocked (Forbidden) for {ctx.author} — DMs disabled?"
            )
        except Exception as e:
            logger.info(f"[task] rejection DM failed for {ctx.author}: {e}")

    # Always delete the invoking message to leave no trace
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    # Build routing flags from configured agents: -<first_letter> and --<name>
    _agent_flag_map = {}  # flag_str -> agent_name
    for _a in AGENTS:
        _agent_flag_map[f"-{_a.name[0]}"] = _a.name
        _agent_flag_map[f"--{_a.name[0]}"] = _a.name
        _agent_flag_map[f"--{_a.name}"] = _a.name

    # Parse entirely from raw content — never let the framework tokenize it
    raw = re.sub(r'^!task\s*', '', original_content, flags=re.IGNORECASE)

    skip_review = bool(re.search(r'(?:^|\s)-nr(?=\s|$)', raw))
    raw = re.sub(r'(?:^|\s)-nr(?=\s|$)', '', raw)

    skip_ui_review = bool(re.search(r'(?:^|\s)-dcr(?=\s|$)', raw))
    raw = re.sub(r'(?:^|\s)-dcr(?=\s|$)', '', raw)

    target = None
    for flag, agent_name in _agent_flag_map.items():
        if re.search(r'(?:^|\s)' + re.escape(flag) + r'(?=\s|$)', raw):
            target = agent_name
            raw = re.sub(r'(?:^|\s)' + re.escape(flag) + r'(?=\s|$)',
                         '',
                         raw,
                         count=1)
            break

    if target is None:
        await _reject()
        return

    task_text = raw.strip()
    if not task_text:
        await _reject()
        return

    # Build confirmation embed
    notes = []
    if skip_review:
        notes.append("code review skipped")
    if skip_ui_review:
        notes.append("UI review skipped")
    review_note = f"  *({', '.join(notes)})*" if notes else ""
    confirm_embed = discord.Embed(
        title="⚠️  Confirm Task Submission",
        description=_truncate(task_text, 1800),
        color=discord.Color(AGENT_MAP[target].discord_color),
    )
    confirm_embed.add_field(name="Route to", value=target.upper(), inline=True)
    confirm_embed.set_footer(
        text=f"Reply y to confirm · n to cancel · times out in 60s{review_note}"
    )
    confirm_msg = await ctx.send(embed=confirm_embed)

    # Wait for y/n from the same user in the same channel
    def _check(m):
        return (m.author == ctx.author and m.channel == ctx.channel
                and m.content.lower() in ("y", "yes", "n", "no"))

    reply_msg = None
    confirmed = False
    try:
        reply_msg = await bot.wait_for("message", check=_check, timeout=60.0)
        confirmed = reply_msg.content.lower() in ("y", "yes")
    except asyncio.TimeoutError:
        confirmed = False

    # Delete the confirmation embed and the user's reply — no trace either way
    for msg in (confirm_msg, reply_msg):
        if msg:
            try:
                await msg.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

    if not confirmed:
        return

    # Write the task file
    try:
        filename = _write_task(task_text, target, skip_review, skip_ui_review)
    except Exception as e:
        err = await ctx.send(f"❌ Failed to queue task: {e}")
        await err.delete(delay=8)
        return

    # Brief success notice — auto-deletes in 5 seconds
    notice = await ctx.send(f"✅ Queued `{filename}` → **{target.upper()}**")
    await notice.delete(delay=5)


@cmd_task.error
async def cmd_task_error(ctx, error):
    """Catch framework-level parse errors (e.g. backticks confusing shlex) and clean up."""
    logger.warning(f"[task] framework error for {ctx.author}: {error}")
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    try:
        _flags_hint2 = " / ".join(f"-{a.name[0]}" for a in AGENTS)
        await ctx.author.send(
            f"the message has been rejected (check routing flag: {_flags_hint2})")
    except Exception:
        pass


@bot.command(name="logs")
async def cmd_logs(ctx, n: int = 20):
    n = min(max(n, 1), 100)
    path = LOG_FILES["ORCHESTRATOR"]
    if not path.exists():
        await ctx.send("No orchestrator.log found yet.")
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-n:])
        await ctx.send(
            f"**Last {n} orchestrator log lines:**\n```\n{_truncate(tail)}\n```"
        )
    except Exception as e:
        await ctx.send(f"Error reading log: {e}")


@bot.command(name="audit")
async def cmd_audit(ctx, filepath: str = None):
    """Run a full Qwen review on a file or all AI-changed files.
    Usage: !audit [optional/path/to/file]"""
    PROJECT_ROOT = AI_DIR.parent

    if filepath:
        target_path = PROJECT_ROOT / filepath
        if not target_path.exists():
            await ctx.send(f"❌ File not found: `{filepath}`")
            return
        await ctx.send(f"🔍 Auditing `{filepath}`...")
        targets = [(filepath, target_path)]
    else:
        await ctx.send(
            "🔍 Auditing all files changed vs `main`... this may take a while.")
        result = subprocess.run(["git", "diff", "main...HEAD", "--name-only"],
                                capture_output=True,
                                text=True,
                                cwd=PROJECT_ROOT)
        names = [f for f in result.stdout.strip().splitlines() if f.strip()]
        if not names:
            await ctx.send("✅ No changed files found vs main.")
            return
        targets = [(name, PROJECT_ROOT / name) for name in names
                   if (PROJECT_ROOT / name).exists()]

    REVIEW_CMD_LOCAL = ["ollama", "run", "qwen-reviewer"]
    results = []
    for rel, fp in targets:
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception as e:
            results.append(f"⚠️ `{rel}`: could not read — {e}")
            continue
        try:
            proc = subprocess.run(REVIEW_CMD_LOCAL +
                                  [f"Review this code:\n{content[:8000]}"],
                                  capture_output=True,
                                  text=True,
                                  timeout=180)
            raw = proc.stdout or proc.stderr or "(no output)"
        except subprocess.TimeoutExpired:
            raw = "Timed out."
        except Exception as e:
            raw = str(e)
        # Strip ANSI and collapse whitespace
        import re as _re
        clean = _re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', raw)
        clean = ' '.join(clean.split())[:500]
        results.append(f"**{rel}**: {clean}")

    if not results:
        await ctx.send("✅ Nothing to audit.")
        return

    # Send paginated (Discord 2000 char limit)
    chunk = ""
    for line in results:
        if len(chunk) + len(line) + 1 > 1900:
            await ctx.send(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await ctx.send(chunk)


@bot.command(name="tasks")
async def cmd_tasks(ctx):
    FAILED = AI_DIR / "messages" / "failed"
    PROCESSED = AI_DIR / "messages" / "processed"
    try:
        rows = []
        for label, path in (
            [(f"{a.name.capitalize()} inbox", a.inbox) for a in AGENTS]
            + [("Human inbox", AI_DIR / "messages" / "inbox_tasks"),
               ("Failed", FAILED), ("Processed", PROCESSED)]
        ):
            count = len(list(path.glob("*.*"))) if path.exists() else 0
            icon = "🔴" if label == "Failed" and count > 0 else "📬" if count > 0 else "✅"
            rows.append(f"{icon} **{label}:** {count}")
        await ctx.send("\n".join(rows))
    except Exception as e:
        await ctx.send(f"Error reading task queues: {e}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

bot.run(TOKEN)
