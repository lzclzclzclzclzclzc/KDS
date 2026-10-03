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
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(db_path)
        try:
            # The first team migration keeps a consistent SQLite backup of an
            # existing business database. New/temporary databases need none.
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'conversations' in tables and 'team_sessions' not in tables:
                backup_path = db_path.with_name(db_path.stem + '.before-team-v1.db')
                if not backup_path.exists():
                    backup_conn = sqlite3.connect(str(backup_path))
                    try:
                        conn.backup(backup_conn)
                    finally:
                        backup_conn.close()
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
            from app.migrations.langgraph_v1 import migrate
            migrate(conn)
            from app.migrations.team_v1 import migrate as migrate_teams
            migrate_teams(conn)
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
        "state_rev": row["state_rev"] if "state_rev" in row.keys() else 0,
    }


def _revision_update(conn: sqlite3.Connection) -> str:
    # A few legacy importers use the original schema until init_db is called.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    return ", state_rev = state_rev + 1" if "state_rev" in columns else ""


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
            revision = _revision_update(conn)
            if status is not None:
                conn.execute(
                    "UPDATE conversations SET payload = ?, status = ?, updated_at = ?" + revision + " WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), status, _now(), record_id),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET payload = ?, updated_at = ?" + revision + " WHERE id = ?",
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
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "orchestration_attempts" in tables:
                conn.execute(
                    "DELETE FROM orchestration_attempts WHERE operation_id IN "
                    "(SELECT operation_id FROM orchestration_operations WHERE conversation_id = ?)",
                    (record_id,),
                )
            for table in ("usage_events", "orchestration_commands", "orchestration_operations"):
                if table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (record_id,))
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (record_id,))
            conn.commit()
        finally:
            conn.close()
    return cur.rowcount > 0


def mark_stale_running_conversations() -> None:
    """Recover conversations and votes whose worker threads ended on restart."""
    with _lock:
        conn = _connect(DB_PATH)
        try:
            rows = conn.execute("SELECT id, payload, status FROM conversations").fetchall()
            now = _now()
            revision = _revision_update(conn)
            for row in rows:
                payload = json.loads(row["payload"])
                if payload.get("kind") == "team":
                    continue
                changed = False
                if row["status"] == "running":
                    active_seconds = max(
                        float(payload.get("active_seconds") or 0),
                        float(payload.get("elapsed_seconds") or 0),
                    )
                    total_max_tokens = payload.get("total_max_tokens")
                    total_duration_seconds = payload.get("total_duration_seconds")
                    limit_reached = (
                        (total_max_tokens is not None
                         and payload.get("total_output_tokens", 0) >= total_max_tokens)
                        or (total_duration_seconds is not None
                            and active_seconds >= total_duration_seconds)
                    )
                    payload["status"] = "paused"
                    payload["ended_at"] = None
                    payload["active_seconds"] = active_seconds
                    payload["elapsed_seconds"] = round(active_seconds, 1)
                    payload["remaining_seconds"] = (
                        round(max(0.0, total_duration_seconds - active_seconds), 1)
                        if total_duration_seconds is not None else None
                    )
                    payload["paused_reason"] = "limit" if limit_reached else "manual"
                    payload["can_resume"] = not limit_reached
                    changed = True
                for vote in payload.get("votes") or []:
                    if vote.get("status") in ("pending", "running"):
                        vote["status"] = "error"
                        vote["error"] = "服务重启，投票已中断，请重新发起"
                        changed = True
                if not changed:
                    continue
                conn.execute(
                    "UPDATE conversations SET status = ?, payload = ?, updated_at = ?" + revision + " WHERE id = ?",
                    (payload["status"], json.dumps(payload, ensure_ascii=False), now, row["id"]),
                )
            conn.commit()
        finally:
            conn.close()
