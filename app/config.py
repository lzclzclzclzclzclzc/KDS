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

DEFAULT_SINGLE_MAX_TOKENS = 300

HOST = os.getenv("HOST", "127.0.0.1")
PORT = _int("PORT", 5000)

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "kds.db"

# The backend is stored in each conversation; this only controls new chats.
ORCHESTRATION_BACKEND = os.getenv("ORCHESTRATION_BACKEND", "langgraph").strip().lower()
if ORCHESTRATION_BACKEND not in {"legacy", "langgraph"}:
    ORCHESTRATION_BACKEND = "langgraph"
LANGGRAPH_CHECKPOINT_PATH = Path(os.getenv(
    "LANGGRAPH_CHECKPOINT_PATH", str(DATA_DIR / "langgraph_checkpoints.db")
)).expanduser()
GRAPH_MAX_CONCURRENCY = max(1, _int("GRAPH_MAX_CONCURRENCY", 1))
AUXILIARY_MAX_CONCURRENCY = max(1, _int("AUXILIARY_MAX_CONCURRENCY", 4))

# Discussion turns use Harness; auxiliary scoring/voting keep the LLM_* route.
AGENT_BACKEND = os.getenv("AGENT_BACKEND", "dsh").strip().lower()
DSH_MODEL = os.getenv("DSH_MODEL", "deepseek-v4-flash").strip()
DSH_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
# Newer standalone dsh uses Messages (/anthropic); the pinned SDK runtime
# and auxiliary OpenAI client use Chat Completions. Keep their bases separate.
DSH_BASE_URL = (os.getenv("DSH_BASE_URL", "").strip()
                or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()).rstrip("/")
DSH_BIN = os.getenv("DSH_BIN", "").strip() or None
DSH_REASONING_EFFORT = os.getenv("DSH_REASONING_EFFORT", "").strip() or None
DSH_REQUEST_MAX_TOKENS = _int("DSH_REQUEST_MAX_TOKENS", 8192)
DSH_TURN_MAX_TOKENS = _int("DSH_TURN_MAX_TOKENS", 24000)
DSH_MAX_STEPS = _int("DSH_MAX_STEPS", 8)
DSH_MAX_TOOL_CALLS = _int("DSH_MAX_TOOL_CALLS", 12)
DSH_TURN_TIMEOUT = _float("DSH_TURN_TIMEOUT", 180)
DSH_JSON_BASE_URL = (os.getenv("DSH_JSON_BASE_URL", "").strip()
                     or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()).rstrip("/")
DSH_JSON_REPAIR_ATTEMPTS = _int("DSH_JSON_REPAIR_ATTEMPTS", 2)
DSH_JSON_REPAIR_MAX_TOKENS = _int("DSH_JSON_REPAIR_MAX_TOKENS", 8192)
DSH_TOOLS = tuple(x.strip() for x in os.getenv(
    "DSH_TOOLS", "web_search,web_fetch,read,glob,grep,write,edit,pwsh,bash"
).split(",") if x.strip())
