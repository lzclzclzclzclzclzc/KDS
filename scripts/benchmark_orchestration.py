"""Offline orchestration benchmark; all databases live in a temporary directory.

Run from the repository root:
    python scripts/benchmark_orchestration.py --turns 40 80 --repeat 2
Use --checkpointer sqlite to include synchronous file checkpoint overhead.
"""
import argparse
from contextlib import closing
import gc
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver

from app import db
from app.llm import LLMClient
from app.orchestration.checkpointer import close_savers, get_saver
from app.orchestration.runtime import GraphRunner
from app.repositories.orchestration import OrchestrationRepository


def measure(turns, speech_chars, checkpointer):
    with TemporaryDirectory(prefix="kds-perf-") as directory:
        business = Path(directory) / "business.db"
        checkpoint = Path(directory) / "checkpoints.db"
        with patch.object(db, "DB_PATH", business):
            repository = OrchestrationRepository(business)
            saver = get_saver(checkpoint) if checkpointer == "sqlite" else InMemorySaver()
            llm = LLMClient(mock=True)
            calls = 0

            def respond(*args):
                nonlocal calls
                calls += 1
                return {"speech": "x" * speech_chars, "propose_end": False,
                        "whiteboard_ops": []}, {"prompt_tokens": 1, "completion_tokens": 1}

            llm.agent_turn = respond
            runner = GraphRunner("benchmark", "config", "离线性能测试", {
                "agent_backend": "direct", "scheduling_mode": "round_robin",
                "agents": [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}],
                "first_speaker": "a", "single_max_tokens": 1000, "total_max_tokens": turns,
            }, llm, repository=repository, saver=saver)
            db.create_conversation(runner.id, runner.config_id, runner.name, runner.to_dict())
            try:
                start = perf_counter()
                runner._claim()
                runner._run()
                elapsed = perf_counter() - start
                assert runner.error is None, runner.error
                assert len(runner.messages) == calls == turns
                assert runner.status == "paused" and runner.paused_reason == "limit"
                assert runner.total_output_tokens == turns
            finally:
                close_savers(checkpoint)
            with closing(sqlite3.connect(business)) as conn:
                count, snapshots, inputs = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(LENGTH(committed_snapshot)), 0), "
                    "COALESCE(SUM(LENGTH(input)), 0) FROM orchestration_operations"
                ).fetchone()
                payload_bytes = conn.execute(
                    "SELECT LENGTH(CAST(payload AS BLOB)) FROM conversations WHERE id = ?",
                    (runner.id,),
                ).fetchone()[0]
            result = {"turns": turns, "speech_chars": speech_chars, "checkpointer": checkpointer,
                      "seconds": round(elapsed, 3), "operations": count,
                      "snapshot_chars": snapshots, "input_chars": inputs,
                      "payload_bytes": payload_bytes, "business_bytes": business.stat().st_size,
                      "checkpoint_bytes": checkpoint.stat().st_size if checkpoint.exists() else 0}
            del runner, saver
            gc.collect()
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", nargs="+", type=int, default=[40, 80])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--speech-chars", type=int, default=1000)
    parser.add_argument("--checkpointer", choices=["memory", "sqlite"], default="memory")
    args = parser.parse_args()
    if min(*args.turns, args.repeat, args.speech_chars) < 1:
        parser.error("回合数、重复次数和发言长度必须大于零")
    for turns in args.turns:
        for repeat in range(args.repeat):
            result = measure(turns, args.speech_chars, args.checkpointer)
            print(json.dumps(dict(repeat=repeat + 1, **result), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
