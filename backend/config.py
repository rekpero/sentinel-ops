"""
Configuration module - loads all settings from environment variables.
Designed to be general-purpose (not hardcoded to any specific repo).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === Tokens ===
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
CLAUDE_SETUP_TOKEN = os.getenv("CLAUDE_SETUP_TOKEN", "")
SWARMOPS_API_KEY = os.getenv("SWARMOPS_API_KEY", "")

# === GitHub ===
GITHUB_REPO = os.getenv("GITHUB_REPO", "spheron-core/landing-site")  # owner/repo
GITHUB_BLOG_LABEL = os.getenv("GITHUB_BLOG_LABEL", "blog")
GITHUB_AGENT_LABEL = os.getenv("GITHUB_AGENT_LABEL", "agent")
GITHUB_BOT_USERNAME = os.getenv("GITHUB_BOT_USERNAME", "claude-swarmops")
GITHUB_REVIEWER_USERNAME = os.getenv("GITHUB_REVIEWER_USERNAME", "rekpero")

# === SwarmOps ===
SWARMOPS_URL = os.getenv("SWARMOPS_URL", "http://67.220.95.148:8420")
SWARMOPS_TRIGGER_MENTION = os.getenv("SWARMOPS_TRIGGER_MENTION", "@claude-swarmops work on this")

# === Spheron ===
SPHERON_PRICING_API = os.getenv("SPHERON_PRICING_API", "https://app.spheron.ai/api/gpu-offers")
SPHERON_DOCS_URL = os.getenv("SPHERON_DOCS_URL", "https://docs.spheron.ai")
SPHERON_BLOG_URL = os.getenv("SPHERON_BLOG_URL", "https://www.spheron.network/blog/")

# === Agent Settings ===
DISCOVERY_CRON_HOUR = int(os.getenv("DISCOVERY_CRON_HOUR", "9"))  # Run at 9 AM
DISCOVERY_CRON_MINUTE = int(os.getenv("DISCOVERY_CRON_MINUTE", "0"))
DISCOVERY_TOPIC_COUNT = int(os.getenv("DISCOVERY_TOPIC_COUNT", "3"))
PR_POLL_INTERVAL_SECONDS = int(os.getenv("PR_POLL_INTERVAL_SECONDS", "120"))
MAX_REVIEW_ITERATIONS = int(os.getenv("MAX_REVIEW_ITERATIONS", "5"))

# === Workspace ===
BASE_DIR = Path(os.getenv("BASE_DIR", str(Path(__file__).parent.parent)))
WORKDIR = Path(os.getenv("WORKDIR", str(BASE_DIR / "workspace")))
REPO_CLONE_DIR = WORKDIR / "repo"

# === Database ===
# Use a writable location for the database
_default_db_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = Path(os.getenv("DB_PATH", str(_default_db_dir / "pipeline.db")))

# === Server ===
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8500"))

# === Claude Code ===
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
CLAUDE_MAX_TURNS = int(os.getenv("CLAUDE_MAX_TURNS", "30"))
