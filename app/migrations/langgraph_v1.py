"""Add durable orchestration records without rewriting existing payloads."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "state_rev" not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN state_rev INTEGER NOT NULL DEFAULT 0")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orchestration_operations (
            operation_id TEXT PRIMARY KEY,
            parent_operation_id TEXT,
            conversation_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'prepared',
            runner_epoch INTEGER NOT NULL DEFAULT 0,
            input TEXT,
            selection TEXT,
            result TEXT,
            output TEXT,
            committed_snapshot TEXT,
            recovered_at TEXT,
            error TEXT,
            committed_rev INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS orchestration_operations_conversation
            ON orchestration_operations(conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS orchestration_operations_parent
            ON orchestration_operations(conversation_id, parent_operation_id, created_at, operation_id);
        CREATE INDEX IF NOT EXISTS orchestration_operations_kind_status
            ON orchestration_operations(conversation_id, kind, status, created_at, operation_id);
        CREATE TABLE IF NOT EXISTS orchestration_attempts (
            attempt_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            runner_epoch INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS orchestration_attempts_operation
            ON orchestration_attempts(operation_id, created_at);
        CREATE TABLE IF NOT EXISTS usage_events (
            event_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            operation_id TEXT,
            attempt_id TEXT,
            source TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS usage_events_conversation
            ON usage_events(conversation_id);
        CREATE TABLE IF NOT EXISTS orchestration_commands (
            command_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        DROP INDEX IF EXISTS orchestration_commands_pending_slot;
        CREATE UNIQUE INDEX orchestration_commands_pending_slot
            ON orchestration_commands(conversation_id)
            WHERE status = 'pending' AND kind = 'human';
    """)
    operation_columns = {row[1] for row in conn.execute("PRAGMA table_info(orchestration_operations)")}
    if "committed_snapshot" not in operation_columns:
        conn.execute("ALTER TABLE orchestration_operations ADD COLUMN committed_snapshot TEXT")
    if "recovered_at" not in operation_columns:
        conn.execute("ALTER TABLE orchestration_operations ADD COLUMN recovered_at TEXT")
