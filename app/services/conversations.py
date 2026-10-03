"""Select a persisted conversation's engine and share one live owner."""
from app import config as settings
from app import db
from app.engine import RUNNERS, RUNNERS_LOCK, ConversationRunner


class ConversationService:
    def create_runner(self, conv_id, config_id, name, config, llm, backend=None):
        backend = backend or settings.ORCHESTRATION_BACKEND
        if backend not in {"legacy", "langgraph"}:
            raise ValueError("编排后端必须是 legacy 或 langgraph")
        if backend == "langgraph":
            from app.orchestration.runtime import GraphRunner
            return GraphRunner(conv_id, config_id, name, config, llm)
        return ConversationRunner(conv_id, config_id, name, config, llm)

    def load(self, conv_id, statuses, llm, get_record=None):
        with RUNNERS_LOCK:
            runner = RUNNERS.get(conv_id)
            if runner is not None:
                return runner, None
            record = (get_record or db.get_conversation)(conv_id)
            if record and record.get('kind') == 'team':
                return None, record
            if record is None or record.get("status") not in statuses:
                return None, record
            if record.get("orchestration_backend", "legacy") == "langgraph":
                from app.orchestration.runtime import GraphRunner
                runner = GraphRunner.from_payload(record, llm)
            else:
                runner = ConversationRunner.from_payload(record, llm)
            RUNNERS[conv_id] = runner
            return runner, record

    def delete(self, conv_id):
        record = db.get_conversation(conv_id)
        if record and record.get('kind') == 'team':
            from app.services.team_sessions import team_service
            return team_service.delete(conv_id)
        with RUNNERS_LOCK:
            runner = RUNNERS.get(conv_id)
            if runner is not None and hasattr(runner, "delete"):
                ok = runner.delete()
            else:
                if runner is not None:
                    runner.interrupt()
                record = db.get_conversation(conv_id)
                if record and record.get("orchestration_backend") == "langgraph":
                    from app.repositories.orchestration import OrchestrationRepository
                    from app.orchestration.checkpointer import delete_conversation
                    ok = OrchestrationRepository().delete_conversation(conv_id)
                    delete_conversation(conv_id)
                else:
                    ok = db.delete_conversation(conv_id)
            RUNNERS.pop(conv_id, None)
            return ok

    def migrate(self, conv_id, backend, llm):
        """Explicit migration at a fully reconciled paused boundary."""
        if backend not in {"legacy", "langgraph"}:
            raise ValueError("编排后端必须是 legacy 或 langgraph")
        with RUNNERS_LOCK:
            record = db.get_conversation(conv_id)
            if record is None:
                return None
            if record.get('kind') == 'team':
                raise ValueError('团队运行请使用团队观察台，不能迁移到旧群聊引擎')
            old = RUNNERS.get(conv_id)
            if old is not None:
                with old._lock:
                    if (old.status != "paused" or old.is_alive() or old._vote_in_progress
                            or old._summary_in_progress):
                        raise ValueError("请先暂停并等待当前操作结束")
                    record = old.to_dict()
            elif record["status"] != "paused":
                raise ValueError("只能在暂停状态切换编排后端")
            if record.get("orchestration_backend", "legacy") == "langgraph":
                from app.repositories.orchestration import OrchestrationRepository
                repo = old.repository if old is not None else OrchestrationRepository()
                pending = repo.list_operations(conv_id, statuses=("prepared", "running", "result_ready", "uncertain"), include_payload=False)
                if pending:
                    raise ValueError("存在未对账操作，请先恢复并完成当前推进单元")
            record["orchestration_backend"] = backend
            if backend == "langgraph":
                from app.orchestration.runtime import GraphRunner
                record.update(schema_version=1, graph_version=GraphRunner.graph_version)
                new = GraphRunner.from_payload(record, llm)
                command = new.repository.get_pending_command(conv_id)
                if record.get("pending_human_message") is not None and command is None:
                    new.repository.reserve_command(conv_id, {"content": record["pending_human_message"],
                                                             "target": record.get("pending_human_target")})
                new._sync_usage()
                new._persist()
            else:
                new = ConversationRunner.from_payload(record, llm)
                db.update_conversation(conv_id, new.to_dict(), "paused")
            RUNNERS[conv_id] = new
            return new


conversation_service = ConversationService()
