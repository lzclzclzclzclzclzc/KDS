import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_MOCK = _bool("LLM_MOCK", False)
LLM_TEMPERATURE = _float("LLM_TEMPERATURE", 0.8)
LLM_SCORE_TEMPERATURE = _float("LLM_SCORE_TEMPERATURE", 0.0)
LLM_SCORE_MAX_TOKENS = _int("LLM_SCORE_MAX_TOKENS", 512)
LLM_TIMEOUT = _float("LLM_TIMEOUT", 120)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = _int("PORT", 5000)

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "kds.db"
