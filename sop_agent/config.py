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


# Direct-provider settings (used when the visitor's key is an OpenAI or Anthropic key)
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")


def api_key() -> str | None:
    """Optional server-side fallback keys (the live demo sets none: visitors bring their own)."""
    return os.getenv("OPENROUTER_API_KEY")


def openai_key() -> str | None:
    return os.getenv("OPENAI_API_KEY")


def anthropic_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY")


def today() -> date:
    """'Today' for deadline math. Set TODAY=YYYY-MM-DD to replay the fixtures at a fixed date."""
    value = os.getenv("TODAY")
    return date.fromisoformat(value) if value else date.today()
