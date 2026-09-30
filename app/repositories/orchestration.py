"""Atomic business persistence, independent of LangGraph's checkpoint file.

Every mutation uses a short SQLite transaction. A checkpointer can replay nodes,
but only this repository decides whether their business effect was committed.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from app import db


class RepositoryConflict(RuntimeError):
    """The conversation was deleted, revised, or claimed by another runner."""


_JSON_FIELDS = {"input", "selection", "result", "output", "committed_snapshot", "error", "payload"}
_OP_FIELDS = {"status", "runner_epoch", "input", "selection", "result", "output", "error", "committed_rev"}
_ATTEMPT_FIELDS = {"status", "result", "error"}


def _encode(value):
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _decode(row):
    if row is None:
        return None
    value = dict(row)
    for key in _JSON_FIELDS & value.keys():
        if value[key] is not None:
            value[key] = json.loads(value[key])
    return value


class OrchestrationRepository:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path if db_path is not None else db.DB_PATH)
        db.init_db(self.db_path)

    @contextmanager
    def _transaction(self):
        # Share the legacy lock as well as SQLite's inter-connection write lock.
        with db._lock:
            conn = db._connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _read(self, query, args=(), many=False):
        with db._lock:
            conn = db._connect(self.db_path)
            try:
                cursor = conn.execute(query, args)
                return cursor.fetchall() if many else cursor.fetchone()
            finally:
                conn.close()

    @staticmethod
    def _conversation(conn, conversation_id):
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise RepositoryConflict("Conversation no longer exists")
        return row, json.loads(row["payload"])

    @staticmethod
    def _check(row, current, expected_rev=None, runner_epoch=None):
        if expected_rev is not None and int(row["state_rev"]) != int(expected_rev):
            raise RepositoryConflict("Conversation revision changed")
        if runner_epoch is not None and int(current.get("runner_epoch", 0)) != int(runner_epoch):
            raise RepositoryConflict("Runner epoch changed")

    @staticmethod
    def _save(conn, row, current, payload):
        payload = dict(payload)
        # Usage is independently committed. A graph snapshot may lag a callback.
        for key in ("total_prompt_tokens", "total_output_tokens"):
            payload[key] = max(int(current.get(key, 0) or 0), int(payload.get(key, 0) or 0))
        payload["runner_epoch"] = int(current.get("runner_epoch", 0))
        revision = int(row["state_rev"]) + 1
        payload["state_rev"] = revision
        status = payload.get("status", row["status"])
        conn.execute(
            "UPDATE conversations SET payload = ?, status = ?, state_rev = ?, updated_at = ? WHERE id = ?",
            (_encode(payload), status, revision, db._now(), row["id"]),
        )
        return revision

    def get_conversation(self, conversation_id):
        row = self._read("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return db._row_to_conversation(row) if row else None

    def save_snapshot(self, conversation_id, payload, expected_rev=None, runner_epoch=None):
        with self._transaction() as conn:
            row, current = self._conversation(conn, conversation_id)
            self._check(row, current, expected_rev, runner_epoch)
            return self._save(conn, row, current, payload)

    def claim(self, conversation_id):
        with self._transaction() as conn:
            row, payload = self._conversation(conn, conversation_id)
            epoch = int(payload.get("runner_epoch", 0)) + 1
            payload["runner_epoch"] = epoch
            self._save(conn, row, payload, payload)
            return epoch

    def create_operation(self, operation_id, conversation_id, kind, input=None,
                         parent_operation_id=None, runner_epoch=None, **fields):
        unknown = set(fields) - _OP_FIELDS
        if unknown:
            raise ValueError(f"Unknown operation fields: {sorted(unknown)}")
        with self._transaction() as conn:
            row, payload = self._conversation(conn, conversation_id)
            self._check(row, payload, runner_epoch=runner_epoch)
            existing = conn.execute("SELECT * FROM orchestration_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if existing:
                if existing["conversation_id"] != conversation_id or existing["kind"] != kind:
                    raise RepositoryConflict("Operation ID already belongs to another input")
                return _decode(existing)
            now = db._now()
            epoch = int(payload.get("runner_epoch", 0) if runner_epoch is None else runner_epoch)
            conn.execute(
                "INSERT INTO orchestration_operations "
                "(operation_id, parent_operation_id, conversation_id, kind, status, runner_epoch, input, "
                "selection, result, output, error, committed_rev, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (operation_id, parent_operation_id, conversation_id, kind, fields.get("status", "prepared"), epoch,
                 _encode(input), _encode(fields.get("selection")), _encode(fields.get("result")),
                 _encode(fields.get("output")), _encode(fields.get("error")), fields.get("committed_rev"), now, now),
            )
            return _decode(conn.execute("SELECT * FROM orchestration_operations WHERE operation_id = ?", (operation_id,)).fetchone())

    def get_operation(self, operation_id):
        return _decode(self._read("SELECT * FROM orchestration_operations WHERE operation_id = ?", (operation_id,)))

    def list_operations(self, conversation_id, kind=None, statuses=None):
        query = "SELECT * FROM orchestration_operations WHERE conversation_id = ?"
        args = [conversation_id]
        if kind is not None:
            query += " AND kind = ?"
            args.append(kind)
        if statuses is not None:
            if not statuses:
                return []
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            args.extend(statuses)
        return [_decode(row) for row in self._read(query + " ORDER BY created_at, operation_id", args, many=True)]

    def update_operation(self, operation_id, **fields):
        if not fields or set(fields) - _OP_FIELDS:
            raise ValueError("No fields or unknown operation fields")
        with self._transaction() as conn:
            operation = conn.execute("SELECT * FROM orchestration_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if not operation:
                raise RepositoryConflict("Operation no longer exists")
            row, payload = self._conversation(conn, operation["conversation_id"])
            epoch = int(fields.get("runner_epoch", operation["runner_epoch"]))
            self._check(row, payload, runner_epoch=epoch)
            if operation["status"] in {"committed", "abandoned"}:
                if all(_decode(operation).get(key) == value for key, value in fields.items()):
                    return _decode(operation)
                raise RepositoryConflict("Operation is already finalized")
            assignments = [f"{key} = ?" for key in fields]
            if fields.get("status") == "running":
                assignments.append("recovered_at = NULL")
            values = [_encode(value) if key in _JSON_FIELDS else value for key, value in fields.items()]
            conn.execute("UPDATE orchestration_operations SET " + ", ".join(assignments) + ", updated_at = ? WHERE operation_id = ?",
                         values + [db._now(), operation_id])
            return _decode(conn.execute("SELECT * FROM orchestration_operations WHERE operation_id = ?", (operation_id,)).fetchone())

    def create_attempt(self, attempt_id, operation_id, runner_epoch, **fields):
        if set(fields) - _ATTEMPT_FIELDS:
            raise ValueError("Unknown attempt fields")
        with self._transaction() as conn:
            operation = conn.execute("SELECT * FROM orchestration_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if not operation:
                raise RepositoryConflict("Operation no longer exists")
            row, payload = self._conversation(conn, operation["conversation_id"])
            self._check(row, payload, runner_epoch=runner_epoch)
            if int(operation["runner_epoch"]) != int(runner_epoch) or operation["status"] in {"committed", "abandoned"}:
                raise RepositoryConflict("Operation cannot start another attempt")
            now = db._now()
            conn.execute(
                "INSERT INTO orchestration_attempts (attempt_id, operation_id, runner_epoch, status, result, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, operation_id, runner_epoch, fields.get("status", "running"), _encode(fields.get("result")), _encode(fields.get("error")), now, now),
            )
            conn.execute("UPDATE orchestration_operations SET recovered_at = NULL WHERE operation_id = ?", (operation_id,))
            return _decode(conn.execute("SELECT * FROM orchestration_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone())

    def list_attempts(self, operation_id):
        return [_decode(row) for row in self._read("SELECT * FROM orchestration_attempts WHERE operation_id = ? ORDER BY created_at, attempt_id", (operation_id,), many=True)]

    def update_attempt(self, attempt_id, **fields):
        if not fields or set(fields) - _ATTEMPT_FIELDS:
            raise ValueError("No fields or unknown attempt fields")
        with self._transaction() as conn:
            attempt = conn.execute(
                "SELECT a.*, o.conversation_id FROM orchestration_attempts a JOIN orchestration_operations o "
                "ON a.operation_id = o.operation_id WHERE a.attempt_id = ?", (attempt_id,),
            ).fetchone()
            if not attempt:
                raise RepositoryConflict("Attempt no longer exists")
            row, payload = self._conversation(conn, attempt["conversation_id"])
            self._check(row, payload, runner_epoch=attempt["runner_epoch"])
            assignments = [f"{key} = ?" for key in fields]
            values = [_encode(value) if key in _JSON_FIELDS else value for key, value in fields.items()]
            conn.execute("UPDATE orchestration_attempts SET " + ", ".join(assignments) + ", updated_at = ? WHERE attempt_id = ?",
                         values + [db._now(), attempt_id])
            return _decode(conn.execute("SELECT * FROM orchestration_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone())

    def record_usage(self, event_id, conversation_id, operation_id=None, attempt_id=None,
                     source="", prompt_tokens=0, completion_tokens=0):
        prompt_tokens, completion_tokens = int(prompt_tokens or 0), int(completion_tokens or 0)
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("Usage deltas cannot be negative")
        with self._transaction() as conn:
            row, payload = self._conversation(conn, conversation_id)
            event = conn.execute("SELECT * FROM usage_events WHERE event_id = ?", (event_id,)).fetchone()
            if event:
                identity = (conversation_id, operation_id, attempt_id, source, prompt_tokens, completion_tokens)
                if tuple(event[key] for key in ("conversation_id", "operation_id", "attempt_id", "source", "prompt_tokens", "completion_tokens")) != identity:
                    raise RepositoryConflict("Usage event ID was reused for different usage")
                return False
            # Known usage from an old epoch remains billable; result publication does not.
            conn.execute(
                "INSERT INTO usage_events (event_id, conversation_id, operation_id, attempt_id, source, prompt_tokens, completion_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, conversation_id, operation_id, attempt_id, source, prompt_tokens, completion_tokens, db._now()),
            )
            payload["total_prompt_tokens"] = int(payload.get("total_prompt_tokens", 0) or 0) + prompt_tokens
            payload["total_output_tokens"] = int(payload.get("total_output_tokens", 0) or 0) + completion_tokens
            self._save(conn, row, payload, payload)
            return True

    def commit(self, operation_id, payload, expected_rev, runner_epoch, consume_command_id=None):
        with self._transaction() as conn:
            operation = conn.execute("SELECT * FROM orchestration_operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if not operation:
                raise RepositoryConflict("Operation no longer exists")
            row, current = self._conversation(conn, operation["conversation_id"])
            self._check(row, current, runner_epoch=runner_epoch)
            if int(operation["runner_epoch"]) != int(runner_epoch):
                raise RepositoryConflict("Operation belongs to an older runner epoch")
            if operation["status"] == "committed":
                return int(operation["committed_rev"])
            if operation["status"] in {"abandoned", "uncertain"}:
                raise RepositoryConflict("Operation has no safe result to commit")
            self._check(row, current, expected_rev=expected_rev)
            if consume_command_id is not None:
                cur = conn.execute(
                    "UPDATE orchestration_commands SET status = 'consumed', updated_at = ? "
                    "WHERE command_id = ? AND conversation_id = ? AND status = 'pending'",
                    (db._now(), consume_command_id, row["id"]),
                )
                if cur.rowcount != 1:
                    raise RepositoryConflict("Pending command was changed")
                payload = dict(payload, pending_human_message=None, pending_human_target=None)
            revision = self._save(conn, row, current, payload)
            committed_snapshot = conn.execute("SELECT payload FROM conversations WHERE id = ?", (row["id"],)).fetchone()["payload"]
            conn.execute(
                "UPDATE orchestration_operations SET status = 'committed', committed_snapshot = ?, committed_rev = ?, updated_at = ? WHERE operation_id = ?",
                (committed_snapshot, revision, db._now(), operation_id),
            )
            return revision

    def reserve_command(self, conversation_id, payload, command_id=None, kind="human"):
        command_id = command_id or uuid.uuid4().hex
        with self._transaction() as conn:
            row, current = self._conversation(conn, conversation_id)
            now = db._now()
            if kind == "human":
                conn.execute("UPDATE orchestration_commands SET status = 'superseded', updated_at = ? WHERE conversation_id = ? AND kind = 'human' AND status = 'pending'", (now, conversation_id))
            conn.execute(
                "INSERT INTO orchestration_commands (command_id, conversation_id, kind, payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (command_id, conversation_id, kind, _encode(payload), now, now),
            )
            if kind == "human":
                current["pending_human_message"] = payload.get("message", payload.get("content"))
                current["pending_human_target"] = payload.get("target_agent_id", payload.get("target"))
                self._save(conn, row, current, current)
            return _decode(conn.execute("SELECT * FROM orchestration_commands WHERE command_id = ?", (command_id,)).fetchone())

    def get_pending_command(self, conversation_id):
        return _decode(self._read("SELECT * FROM orchestration_commands WHERE conversation_id = ? AND kind = 'human' AND status = 'pending'", (conversation_id,)))

    def sync_legacy_reservation(self, conversation_id, content, target):
        """Mirror an already saved legacy slot without rewriting its snapshot.

        Legacy still owns its payload and revision. This ledger lets a paused
        legacy runner hand an unconsumed reservation to the graph runner.
        """
        with self._transaction() as conn:
            self._conversation(conn, conversation_id)
            pending = conn.execute(
                "SELECT * FROM orchestration_commands WHERE conversation_id = ? AND kind = 'human' AND status = 'pending'",
                (conversation_id,),
            ).fetchone()
            now = db._now()
            if content is None:
                if pending:
                    conn.execute("UPDATE orchestration_commands SET status = 'consumed', updated_at = ? WHERE command_id = ?", (now, pending["command_id"]))
                return None
            if pending:
                existing = json.loads(pending["payload"])
                if (existing.get("content", existing.get("message")) == content
                        and existing.get("target", existing.get("target_agent_id")) == target):
                    return _decode(pending)
                conn.execute("UPDATE orchestration_commands SET status = 'superseded', updated_at = ? WHERE command_id = ?", (now, pending["command_id"]))
            command_id = uuid.uuid4().hex
            payload = {"content": content, "target": target}
            conn.execute(
                "INSERT INTO orchestration_commands (command_id, conversation_id, kind, payload, status, created_at, updated_at) VALUES (?, ?, 'human', ?, 'pending', ?, ?)",
                (command_id, conversation_id, _encode(payload), now, now),
            )
            return _decode(conn.execute("SELECT * FROM orchestration_commands WHERE command_id = ?", (command_id,)).fetchone())

    def consume_command(self, command_id):
        with self._transaction() as conn:
            command = conn.execute("SELECT * FROM orchestration_commands WHERE command_id = ?", (command_id,)).fetchone()
            if not command or command["status"] != "pending":
                return False
            row, current = self._conversation(conn, command["conversation_id"])
            conn.execute("UPDATE orchestration_commands SET status = 'consumed', updated_at = ? WHERE command_id = ?", (db._now(), command_id))
            if command["kind"] == "human":
                current.update(pending_human_message=None, pending_human_target=None)
                self._save(conn, row, current, current)
            return True

    def recover(self):
        """Reconcile process loss without making any external model requests."""
        changed_ids = []
        with self._transaction() as conn:
            now = db._now()
            conn.execute("UPDATE orchestration_attempts SET status = 'uncertain', error = ?, updated_at = ? WHERE status = 'running'",
                         (_encode("服务重启，调用结果不确定"), now))
            for row in conn.execute("SELECT * FROM conversations").fetchall():
                payload = json.loads(row["payload"])
                operations = conn.execute("SELECT * FROM orchestration_operations WHERE conversation_id = ? AND status NOT IN ('committed', 'abandoned') AND recovered_at IS NULL", (row["id"],)).fetchall()
                changed = bool(operations)
                reconciled_summaries = []
                if row["status"] == "running":
                    active = max(float(payload.get("active_seconds") or 0), float(payload.get("elapsed_seconds") or 0))
                    token_limit, time_limit = payload.get("total_max_tokens"), payload.get("total_duration_seconds")
                    reached = ((token_limit is not None and int(payload.get("total_output_tokens", 0)) >= token_limit)
                               or (time_limit is not None and active >= time_limit))
                    payload.update(status="paused", ended_at=None, active_seconds=active,
                                   elapsed_seconds=round(active, 1), paused_reason="limit" if reached else "manual",
                                   can_resume=not reached, remaining_seconds=round(max(0.0, time_limit - active), 1) if time_limit is not None else None)
                    changed = True
                for vote in payload.get("votes") or []:
                    if vote.get("status") in {"pending", "running"}:
                        vote.update(status="error", error="服务重启，投票已中断，请重新发起")
                        changed = True
                for operation in operations:
                    kind, status = operation["kind"], operation["status"]
                    conn.execute("UPDATE orchestration_operations SET recovered_at = ? WHERE operation_id = ?", (now, operation["operation_id"]))
                    if kind in {"vote", "ordinary_vote"} or (kind.startswith("vote") and kind != "vote_end"):
                        conn.execute("UPDATE orchestration_operations SET status = 'abandoned', error = ?, updated_at = ? WHERE operation_id = ?", (_encode("服务重启，投票已中断，请重新发起"), now, operation["operation_id"]))
                    elif kind == "summary":
                        payload["status"] = "completed"
                        payload["can_resume"] = False
                        result = _decode(operation).get("result")
                        if not result:
                            model_result = conn.execute(
                                "SELECT result FROM orchestration_operations WHERE parent_operation_id = ? "
                                "AND kind = 'summary_model' AND status IN ('result_ready', 'committed') ORDER BY created_at DESC LIMIT 1",
                                (operation["operation_id"],),
                            ).fetchone()
                            if model_result and model_result["result"]:
                                result = json.loads(model_result["result"])
                        if isinstance(result, dict) and "summary" in result:
                            payload["summary"] = result["summary"]
                            reconciled_summaries.append((operation["operation_id"], result))
                        else:
                            payload["summary"] = "总结中断：服务重启，未保存确定结果"
                            conn.execute("UPDATE orchestration_operations SET status = 'abandoned', error = ?, updated_at = ? WHERE operation_id = ?", (_encode(payload["summary"]), now, operation["operation_id"]))
                    elif status == "running":
                        conn.execute("UPDATE orchestration_operations SET status = 'uncertain', error = ?, updated_at = ? WHERE operation_id = ?", (_encode("服务重启，调用结果不确定"), now, operation["operation_id"]))
                if changed:
                    payload["runner_epoch"] = int(payload.get("runner_epoch", 0)) + 1
                    revision = self._save(conn, row, payload, payload)
                    for operation_id, result in reconciled_summaries:
                        conn.execute(
                            "UPDATE orchestration_operations SET status = 'committed', result = ?, "
                            "runner_epoch = ?, committed_snapshot = ?, committed_rev = ?, updated_at = ? WHERE operation_id = ?",
                            (_encode(result), payload["runner_epoch"], _encode(dict(payload, state_rev=revision)), revision, now, operation_id),
                        )
                    changed_ids.append(row["id"])
        return changed_ids

    def delete_conversation(self, conversation_id):
        with self._transaction() as conn:
            conn.execute("DELETE FROM orchestration_attempts WHERE operation_id IN (SELECT operation_id FROM orchestration_operations WHERE conversation_id = ?)", (conversation_id,))
            for table in ("usage_events", "orchestration_commands", "orchestration_operations"):
                conn.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (conversation_id,))
            return conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount > 0
