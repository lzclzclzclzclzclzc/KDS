"""Process-owned SQLite savers shared by all conversation workers.

The official saver serializes its connection with a lock. Connections permit
cross-thread access, use WAL and wait for short concurrent writes. Runners must
not close a shared saver: close_savers is for application shutdown and tests,
after their graph workers have stopped.
"""

import atexit
import sqlite3
import threading
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import LANGGRAPH_CHECKPOINT_PATH


class ManagedSqliteSaver(SqliteSaver):
    def __init__(self, conn):
        super().__init__(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        self._lifecycle_lock = threading.RLock()
        self._deleted_conversations = set()

    def _check_thread(self, config):
        thread_id = str(config["configurable"]["thread_id"])
        for prefix in self._deleted_conversations:
            if thread_id == prefix or thread_id.startswith(prefix + ":"):
                raise RuntimeError("Conversation checkpoints were deleted")

    def put(self, config, checkpoint, metadata, new_versions):
        with self._lifecycle_lock:
            self._check_thread(config)
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        with self._lifecycle_lock:
            self._check_thread(config)
            return super().put_writes(config, writes, task_id, task_path)

    def delete_conversation(self, conversation_id):
        prefix = f"conversation:{conversation_id}"
        with self._lifecycle_lock:
            self._deleted_conversations.add(prefix)
            # Use the saver's public API rather than relying on its table schema.
            thread_ids = {prefix}
            for checkpoint in self.list(None):
                thread_id = str(checkpoint.config["configurable"]["thread_id"])
                if thread_id == prefix or thread_id.startswith(prefix + ":"):
                    thread_ids.add(thread_id)
            for thread_id in thread_ids:
                self.delete_thread(thread_id)

    def _close(self):
        with self._lifecycle_lock, self.lock:
            self.conn.close()


_savers = {}
_registry_lock = threading.Lock()


def get_saver(path=None):
    path = Path(path if path is not None else LANGGRAPH_CHECKPOINT_PATH).resolve()
    with _registry_lock:
        key = str(path)
        if key not in _savers:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(key, check_same_thread=False, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA busy_timeout = 30000")
                conn.execute("PRAGMA synchronous = NORMAL")
                _savers[key] = ManagedSqliteSaver(conn)
            except BaseException:
                conn.close()
                raise
        return _savers[key]


def delete_conversation(conversation_id, path=None, saver=None):
    saver = saver if saver is not None else get_saver(path)
    if isinstance(saver, ManagedSqliteSaver):
        saver.delete_conversation(conversation_id)
        return
    prefix = f"conversation:{conversation_id}"
    thread_ids = {prefix}
    for checkpoint in saver.list(None):
        thread_id = str(checkpoint.config["configurable"]["thread_id"])
        if thread_id == prefix or thread_id.startswith(prefix + ":"):
            thread_ids.add(thread_id)
    for thread_id in thread_ids:
        saver.delete_thread(thread_id)


def close_savers(path=None):
    """Close only after workers stop; individual runners never own this resource."""
    with _registry_lock:
        keys = list(_savers) if path is None else [str(Path(path).resolve())]
        for key in keys:
            saver = _savers.pop(key, None)
            if saver is not None:
                saver._close()


atexit.register(close_savers)
