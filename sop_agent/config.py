"""Runtime configuration. Everything is overridable through environment variables or .env."""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

APP_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(APP_ROOT / ".env")

FIXTURES_DIR = Path(os.getenv("FIXTURES_DIR", APP_ROOT / "fixtures"))
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.getenv("MODEL", "openai/gpt-5.6-luna")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "1200"))

# SOP thresholds
MIN_VERIFIED_FIELDS = 3
MAX_VERIFY_FAILURES = 3
MAX_OFF_TOPIC_STRIKES = 3
MAX_PERSUASION_ATTEMPTS = 3
HISTORY_MESSAGES = 14


def api_key() -> str | None:
    return os.getenv("OPENROUTER_API_KEY")


def today() -> date:
    """'Today' for deadline math. Set TODAY=YYYY-MM-DD to replay the fixtures at a fixed date."""
    value = os.getenv("TODAY")
    return date.fromisoformat(value) if value else date.today()
