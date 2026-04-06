# ai_team/agents_config.py
#
# SINGLE SOURCE OF TRUTH for AI agent identity.
#
# To swap an agent (e.g. Claude → DeepSeek, Gemini → OpenAI):
#   1. Edit the relevant AgentConfig block below — name, cli_cmd, branch, model_primary, etc.
#   2. Create the matching inbox directory:  mkdir ai_team/messages/inbox_<name>
#   3. Create the matching log file:         touch ai_team/logs/<name>.log
#   4. Update the agent's .md guidance file (CLAUDE.md / GEMINI.md) with the new identity header.
#   5. Nothing else needs to change — orchestrator.py, task.py, and discord_bot.py all derive
#      their agent-specific behaviour from this file.
#
# orchestrator.py imports: AGENTS, AGENT_MAP, REVIEWER_AGENT, REVIEWED_AGENT
# task.py imports:         AGENTS, AGENT_MAP
# discord_bot.py imports:  AGENTS, REVIEW_DISABLED_SENTINEL

from dataclasses import dataclass, field
from pathlib import Path

AI_TEAM_DIR      = Path(__file__).parent.resolve()
AI_TEAM_MSG_DIR  = AI_TEAM_DIR / "messages"

# Sentinel written when the human disables peer UI review for one session.
# Keeping this here means all three files reference the same path object.
REVIEW_DISABLED_SENTINEL = AI_TEAM_MSG_DIR / "DISABLE_CLAUDE_REVIEW"


@dataclass
class AgentConfig:
    # --- Identity ---
    name: str           # Project-level name ("claude", "gemini", "deepseek" …).
                        # Used for inbox dir names, log filenames, task routing, and status labels.
    role: str           # Human-readable role label shown in logs and the status dashboard.

    # --- Code ownership ---
    domain: str         # Git subdirectory this agent exclusively owns ("backend/", "frontend/").
                        # Used for ghost-write detection, git add scoping, and QA domain checks.
    branch: str         # Git working branch ("backend-claude", "frontend-gemini").

    # --- Invocation ---
    cli_cmd: list       # Full CLI command list passed to subprocess when running the agent.
                        # Example (Claude):  ["claude", "--dangerously-skip-permissions", "-p"]
                        # Example (Gemini):  ["gemini", "-y", "-p"]
                        # Example (DeepSeek): ["deepseek", "--model", "deepseek-chat", "-p"]
    qa_cmd: list        # QA command run after each task to verify the build is clean.
                        # Backend:  ["docker", "compose", ..., "python", "manage.py", "check"]
                        # Frontend: ["docker", "compose", ..., "npm", "run", "lint"]
    model_primary: str  # Primary model identifier — used in log messages and fallback reporting.

    # --- Fallback models (for capacity / rate-limit recovery) ---
    # Each entry is a model ID string. When the primary is capacity-exhausted, the orchestrator
    # tries these in order, building the fallback CLI command by inserting the model after "-m".
    # For agents whose CLI does not support a -m flag, leave this empty.
    model_fallbacks: list = field(default_factory=list)

    # --- Peer review ---
    is_reviewer: bool = False
    # True  → this agent reviews the OTHER agent's output before it is merged.
    # False → this agent's output is reviewed by the reviewer agent.
    # Exactly one agent in AGENTS should have is_reviewer=True.

    # --- Display ---
    discord_color: int = 0x92cc41   # Hex colour for Discord embed borders.
    emoji: str = "🤖"               # Icon shown in the TEAM_STATUS dashboard.

    # --- Derived properties (do not set manually) ---

    @property
    def inbox(self) -> Path:
        """Inbox directory where the orchestrator drops tasks for this agent."""
        return AI_TEAM_MSG_DIR / f"inbox_{self.name}"

    @property
    def log_file(self) -> Path:
        """Log file streamed to Discord #logs."""
        return AI_TEAM_DIR / "logs" / f"{self.name}.log"


# ── DEFINE YOUR AGENTS HERE ──────────────────────────────────────────────────
#
# Edit these two blocks to swap models.  Everything downstream derives from them.

AGENT_BACKEND = AgentConfig(
    name           = "claude",
    role           = "Backend Lead",
    domain         = "backend/",
    branch         = "backend-claude",
    cli_cmd        = ["claude", "--dangerously-skip-permissions", "-p"],
    qa_cmd         = [
        "docker", "compose", "-f", "docker-compose.dev.yml",
        "exec", "-T", "backend", "python", "manage.py", "check",
    ],
    model_primary  = "claude-sonnet-4-6",
    model_fallbacks = [],           # Claude has no capacity-based fallback configured
    is_reviewer    = True,          # Backend agent reviews the frontend agent's UI/UX diff
    discord_color  = 0x5865F2,      # blurple
    emoji          = "🦉",
)

AGENT_FRONTEND = AgentConfig(
    name           = "gemini",
    role           = "Frontend Lead",
    domain         = "frontend/",
    branch         = "frontend-gemini",
    cli_cmd        = ["gemini", "-y", "-p"],
    qa_cmd         = [
        "docker", "compose", "-f", "docker-compose.dev.yml",
        "exec", "-T", "frontend", "npm", "run", "lint",
    ],
    model_primary  = "gemini-3.1-pro-preview",
    model_fallbacks = ["gemini-2.5-flash"],   # Tried in order on capacity errors
    is_reviewer    = False,
    discord_color  = 0x10B981,      # teal
    emoji          = "🦊",
)

# ── DERIVED CONSTANTS (do not edit) ─────────────────────────────────────────

# Ordered list — orchestrator processes queues in this order.
AGENTS: list = [AGENT_BACKEND, AGENT_FRONTEND]

# O(1) lookup by agent name string, e.g. AGENT_MAP["claude"]
AGENT_MAP: dict = {a.name: a for a in AGENTS}

# Convenience aliases for the peer-review relationship.
REVIEWER_AGENT: AgentConfig = next(a for a in AGENTS if a.is_reviewer)
REVIEWED_AGENT: AgentConfig = next(a for a in AGENTS if not a.is_reviewer)
