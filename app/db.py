import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import DB_PATH

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS configs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    config_id TEXT,
                    name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def _row_to_config(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload"])
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        **payload,
    }


def _row_to_conversation(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload"])
    return {
        "id": row["id"],
        "config_id": row["config_id"],
        "name": row["name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        **payload,
    }


def create_config(record_id: str, name: str, payload: dict) -> dict:
    now = _now()
    with _lock:
        conn = _connect(DB_PATH)
        try:
            conn.execute(
                "INSERT INTO configs (id, name, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (record_id, name, json.dumps(payload, ensure_ascii=False), now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return get_config(record_id)


def update_config(record_id: str, name: str, payload: dict) -> Optional[dict]:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            cur = conn.execute(
                "UPDATE configs SET name = ?, payload = ?, updated_at = ? WHERE id = ?",
                (name, json.dumps(payload, ensure_ascii=False), _now(), record_id),
            )
            conn.commit()
        finally:
            conn.close()
    return get_config(record_id)


def get_config(record_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            row = conn.execute("SELECT * FROM configs WHERE id = ?", (record_id,)).fetchone()
        finally:
            conn.close()
    return _row_to_config(row) if row else None


def list_configs() -> list[dict]:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            rows = conn.execute("SELECT * FROM configs ORDER BY updated_at DESC").fetchall()
        finally:
            conn.close()
    return [_row_to_config(r) for r in rows]


def delete_config(record_id: str) -> bool:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            cur = conn.execute("DELETE FROM configs WHERE id = ?", (record_id,))
            conn.commit()
        finally:
            conn.close()
    return cur.rowcount > 0


def create_conversation(
    record_id: str, config_id: str, name: str, payload: dict, status: str = "running"
) -> dict:
    now = _now()
    with _lock:
        conn = _connect(DB_PATH)
        try:
            conn.execute(
                "INSERT INTO conversations (id, config_id, name, payload, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record_id, config_id, name, json.dumps(payload, ensure_ascii=False), status, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return get_conversation(record_id)


def update_conversation(record_id: str, payload: dict, status: Optional[str] = None) -> Optional[dict]:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            if status is not None:
                conn.execute(
                    "UPDATE conversations SET payload = ?, status = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), status, _now(), record_id),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET payload = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), _now(), record_id),
                )
            conn.commit()
        finally:
            conn.close()
    return get_conversation(record_id)


def get_conversation(record_id: str) -> Optional[dict]:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (record_id,)).fetchone()
        finally:
            conn.close()
    return _row_to_conversation(row) if row else None


def list_conversations() -> list[dict]:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            rows = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC").fetchall()
        finally:
            conn.close()
    return [_row_to_conversation(r) for r in rows]


def delete_conversation(record_id: str) -> bool:
    with _lock:
        conn = _connect(DB_PATH)
        try:
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (record_id,))
            conn.commit()
        finally:
            conn.close()
    return cur.rowcount > 0


def mark_stale_running_conversations() -> None:
    """Mark conversations left in 'running' state as paused (e.g. after a restart)."""
    with _lock:
        conn = _connect(DB_PATH)
        try:
            rows = conn.execute("SELECT id, payload FROM conversations WHERE status = 'running'").fetchall()
            now = _now()
            for row in rows:
                payload = json.loads(row["payload"])
                payload["status"] = "paused"
                payload["ended_at"] = None
                conn.execute(
                    "UPDATE conversations SET status = 'paused', payload = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), now, row["id"]),
                )
            conn.commit()
        finally:
            conn.close()
