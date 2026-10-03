"""Add versioned teams and row-scoped runtime records, preserving old chats."""
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS role_templates (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS role_versions (
            role_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(role_id, version));
        CREATE TABLE IF NOT EXISTS team_definitions (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS team_definition_versions (
            team_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(team_id, version));
        CREATE TABLE IF NOT EXISTS team_sessions (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 1,
            payload TEXT NOT NULL, event_seq INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_instances (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_id TEXT, status TEXT NOT NULL,
            epoch INTEGER NOT NULL DEFAULT 1, current_task_id TEXT, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS agent_instances_run ON agent_instances(run_id);
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            parent_task_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS agent_tasks_ready ON agent_tasks(run_id,status,created_at);
        CREATE INDEX IF NOT EXISTS agent_tasks_parent ON agent_tasks(run_id,parent_task_id);
        CREATE TABLE IF NOT EXISTS team_rooms (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS team_rooms_run ON team_rooms(run_id);
        CREATE TABLE IF NOT EXISTS discussions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, room_id TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS discussions_run ON discussions(run_id,status);
        CREATE TABLE IF NOT EXISTS team_messages (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL, room_id TEXT,
            instance_id TEXT, task_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS team_messages_scope ON team_messages(run_id,room_id,instance_id,seq);
        CREATE TABLE IF NOT EXISTS mailbox_deliveries (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, instance_id TEXT NOT NULL, task_id TEXT,
            message_id TEXT NOT NULL, consumed_by TEXT,
            UNIQUE(instance_id,message_id));
        CREATE INDEX IF NOT EXISTS mailbox_pending ON mailbox_deliveries(run_id,instance_id,consumed_by);
        CREATE TABLE IF NOT EXISTS team_activations (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, instance_id TEXT NOT NULL, task_id TEXT NOT NULL,
            epoch INTEGER NOT NULL, instance_epoch INTEGER NOT NULL, status TEXT NOT NULL,
            payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS team_one_activation ON team_activations(run_id,instance_id)
            WHERE status IN ('prepared','running','result_ready');
        CREATE TABLE IF NOT EXISTS budget_reservations (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT, tokens INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS budget_reservations_run ON budget_reservations(run_id,status);
        CREATE TABLE IF NOT EXISTS team_receipts (
            scope TEXT NOT NULL, request_id TEXT NOT NULL, arguments TEXT NOT NULL,
            result TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scope,request_id));
        CREATE TABLE IF NOT EXISTS team_events (
            run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(run_id,seq));
        CREATE TABLE IF NOT EXISTS team_tool_logs (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, instance_id TEXT NOT NULL,
            task_id TEXT NOT NULL, activation_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS team_tool_logs_scope ON team_tool_logs(run_id,instance_id,task_id);
        CREATE TABLE IF NOT EXISTS team_artifacts (
            run_id TEXT NOT NULL, rev INTEGER NOT NULL, content TEXT NOT NULL,
            editor_id TEXT, created_at TEXT NOT NULL, PRIMARY KEY(run_id,rev));
    """)
    for table, columns in {
        'orchestration_operations': {'instance_id': 'TEXT', 'task_id': 'TEXT',
                                    'activation_id': 'TEXT', 'instance_epoch': 'INTEGER'},
        'orchestration_attempts': {'instance_id': 'TEXT', 'task_id': 'TEXT'},
        'usage_events': {'instance_id': 'TEXT', 'task_id': 'TEXT'},
    }.items():
        present = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        for name, kind in columns.items():
            if name not in present:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
