from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import ChatMessageRecord, ChatSession, StrategyRun
from app.domain.trading.intents import intents_from_records, intents_to_records, TradeExecutionIntent
from app.schemas.aniu import ChatMessageRead, PersistentSessionRead
from app.services.token_estimator import estimate_messages_tokens, estimate_text_tokens

AUTOMATION_SESSION_SLUG = "automation-default"
AUTOMATION_SESSION_TITLE = "自动化交易会话"
AUTOMATION_DEFAULT_CONTEXT_WINDOW_TOKENS = 128000
AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT = 24
AUTOMATION_DEFAULT_IDLE_SUMMARY_HOURS = 12
AUTOMATION_COMPACTION_TRIGGER_RATIO = 0.85


@dataclass
class PersistentRunSessionContext:
    session_id: int
    prompt_message_id: int
    response_message_id: int | None
    summary_revision: int | None
    context_tokens_estimate: int | None
    messages: list[dict[str, Any]]


class AutomationSessionService:
    def __init__(self) -> None:
        self._hooks: dict[str, Any] = {}

    def configure_hooks(self, **hooks: Any) -> None:
        self._hooks = {**self._hooks, **hooks}

    def _require_hook(self, name: str) -> Any:
        value = self._hooks.get(name)
        if value is None:
            raise RuntimeError(f"automation session hook '{name}' is not configured")
        return value

    def get_persistent_session(self, db: Session) -> PersistentSessionRead:
        session = self.get_or_create_persistent_session(db)
        total_count = db.execute(
            select(func.count(ChatMessageRecord.id)).where(
                ChatMessageRecord.session_id == session.id
            )
        ).scalar_one()
        assume_utc = self._require_hook("assume_utc")
        return PersistentSessionRead(
            id=session.id,
            title=session.title,
            kind=str(session.kind or "automation"),
            slug=session.slug,
            created_at=assume_utc(session.created_at),
            updated_at=assume_utc(session.updated_at),
            last_message_at=assume_utc(session.last_message_at),
            message_count=int(total_count),
            archived_summary=session.archived_summary,
            summary_revision=int(session.summary_revision or 0),
            last_compacted_message_id=session.last_compacted_message_id,
            last_compacted_run_id=session.last_compacted_run_id,
        )

    def list_persistent_session_messages(
        self,
        db: Session,
        *,
        limit: int = 50,
        before_id: int | None = None,
    ) -> tuple[PersistentSessionRead, list[ChatMessageRead], int | None, bool]:
        session = self.get_or_create_persistent_session(db)
        page_size = max(1, int(limit))
        total_count = db.execute(
            select(func.count(ChatMessageRecord.id)).where(
                ChatMessageRecord.session_id == session.id
            )
        ).scalar_one()

        stmt = (
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session.id)
            .order_by(ChatMessageRecord.id.desc())
        )
        if before_id is not None:
            stmt = stmt.where(ChatMessageRecord.id < before_id)

        records = db.execute(stmt.limit(page_size + 1)).scalars().all()
        has_more = len(records) > page_size
        if has_more:
            records = records[:page_size]
        records.reverse()
        next_before_id = records[0].id if has_more and records else None

        assume_utc = self._require_hook("assume_utc")
        session_read = PersistentSessionRead(
            id=session.id,
            title=session.title,
            kind=str(session.kind or "automation"),
            slug=session.slug,
            created_at=assume_utc(session.created_at),
            updated_at=assume_utc(session.updated_at),
            last_message_at=assume_utc(session.last_message_at),
            message_count=int(total_count),
            archived_summary=session.archived_summary,
            summary_revision=int(session.summary_revision or 0),
            last_compacted_message_id=session.last_compacted_message_id,
            last_compacted_run_id=session.last_compacted_run_id,
        )
        return (
            session_read,
            [
                ChatMessageRead(
                    id=record.id,
                    role=record.role,
                    content=record.content,
                    tool_calls=record.tool_calls,
                    attachments=None,
                    created_at=assume_utc(record.created_at),
                )
                for record in records
            ],
            next_before_id,
            has_more,
        )

    def delete_persistent_session(self, db: Session) -> None:
        session = self.get_or_create_persistent_session(db)
        db.execute(delete(ChatMessageRecord).where(ChatMessageRecord.session_id == session.id))
        session.archived_summary = None
        session.summary_updated_at = None
        session.last_compacted_message_id = None
        session.last_compacted_run_id = None
        session.summary_revision = 0
        session.last_message_at = None
        session.title = AUTOMATION_SESSION_TITLE
        session.slug = AUTOMATION_SESSION_SLUG
        db.add(session)

    def prepare_persistent_session_context(
        self,
        *,
        db: Session,
        run_id: int,
        settings: Any,
        trigger_source: str,
        schedule_id: int | None,
    ) -> PersistentRunSessionContext:
        session = self.get_or_create_persistent_session(db)
        user_content = self.build_persistent_session_user_content(
            settings=settings,
            trigger_source=trigger_source,
            schedule_id=schedule_id,
            schedule_name=getattr(settings, "schedule_name", None),
            run_type=str(getattr(settings, "run_type", "analysis") or "analysis"),
            task_prompt=str(getattr(settings, "task_prompt", "") or ""),
            prefetched_context=None,
        )
        user_message = self.persist_persistent_session_user_message(
            db=db,
            session=session,
            run_id=run_id,
            content=user_content,
            schedule_id=schedule_id,
            schedule_name=getattr(settings, "schedule_name", None),
            run_type=str(getattr(settings, "run_type", "analysis") or "analysis"),
            trigger_source=trigger_source,
        )
        history_records = self.list_persistent_session_history_records(
            db=db,
            session_id=session.id,
            recent_limit=int(
                getattr(settings, "automation_recent_message_limit", 0)
                or AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT
            ),
        )
        history_messages = self.build_persistent_session_history_messages(history_records)
        messages = self.build_persistent_session_prompt_messages(
            session=session,
            history_messages=history_messages,
            memory_messages=self.retrieve_persistent_session_memory_messages(
                session=session,
                settings=settings,
                run_type=str(getattr(settings, "run_type", "analysis") or "analysis"),
                task_prompt=str(getattr(settings, "task_prompt", "") or ""),
            ),
        )
        context_tokens_estimate = self.estimate_persistent_session_context_tokens(
            session=session,
            settings=settings,
            messages=messages,
        )
        context_tokens_estimate = max(
            context_tokens_estimate,
            estimate_messages_tokens(history_messages),
        )
        with db.no_autoflush:
            run = db.get(StrategyRun, run_id)
            if run is not None:
                run.chat_session_id = session.id
                run.prompt_message_id = user_message.id
                run.context_tokens_estimate = context_tokens_estimate
                run.context_summary_version = int(session.summary_revision or 0)
                db.add(run)

        return PersistentRunSessionContext(
            session_id=session.id,
            prompt_message_id=user_message.id,
            response_message_id=None,
            summary_revision=int(session.summary_revision or 0),
            context_tokens_estimate=context_tokens_estimate,
            messages=messages,
        )

    def get_or_create_persistent_session(self, db: Session) -> ChatSession:
        settings_loader = self._require_hook("settings_loader")
        now_utc = self._require_hook("now_utc")

        settings = settings_loader(db)
        session_id = int(getattr(settings, "automation_session_id", 0) or 0)
        if session_id > 0:
            existing = db.get(ChatSession, session_id)
            if existing is not None and str(existing.kind or "") == "automation":
                return existing

        session = db.scalar(
            select(ChatSession).where(
                ChatSession.kind == "automation",
                ChatSession.slug == AUTOMATION_SESSION_SLUG,
            )
        )
        if session is None:
            session = ChatSession(
                title=AUTOMATION_SESSION_TITLE,
                kind="automation",
                slug=AUTOMATION_SESSION_SLUG,
            )
            db.add(session)
            db.flush()

        settings.automation_session_id = session.id
        settings.automation_context_window_tokens = int(
            getattr(settings, "automation_context_window_tokens", None)
            or AUTOMATION_DEFAULT_CONTEXT_WINDOW_TOKENS
        )
        settings.automation_recent_message_limit = int(
            getattr(settings, "automation_recent_message_limit", None)
            or AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT
        )
        settings.automation_idle_summary_hours = int(
            getattr(settings, "automation_idle_summary_hours", None)
            or AUTOMATION_DEFAULT_IDLE_SUMMARY_HOURS
        )
        settings.automation_context_source = (
            str(getattr(settings, "automation_context_source", "") or "").strip()
            or "default"
        )
        settings.automation_context_detected_at = now_utc()
        if hasattr(settings, "_sa_instance_state"):
            db.add(settings)
        return session

    def build_persistent_session_user_content(
        self,
        *,
        settings: Any,
        trigger_source: str,
        schedule_id: int | None,
        schedule_name: str | None,
        run_type: str,
        task_prompt: str,
        prefetched_context: str | None,
    ) -> str:
        now_shanghai = self._require_hook("now_shanghai")
        current_time = now_shanghai()
        run_time = (
            f"{current_time.year}年{current_time.month}月{current_time.day}日 "
            f"{current_time.strftime('%H:%M:%S')}"
        )
        trigger_source_text = (
            "定时触发" if str(trigger_source or "").strip() == "schedule" else "手动触发"
        )
        task_type_text = "交易任务" if str(run_type or "").strip() == "trade" else "分析任务"
        lines = [
            f"时间：{run_time}",
            f"来源: {trigger_source_text}",
            f"任务类型: {task_type_text}",
            "",
            "本轮任务:",
            str(task_prompt or "").strip() or "--",
        ]
        del settings, schedule_id, schedule_name, prefetched_context
        return "\n".join(lines).strip()

    def build_persistent_session_assistant_content(
        self,
        *,
        run_id: int,
        run_type: str,
        status: str,
        final_answer: str | None,
        tool_calls: list[dict[str, Any]] | None,
        executed_actions: list[dict[str, Any]] | None,
        error_message: str | None = None,
        phase: str | None = None,
    ) -> str:
        if executed_actions and executed_actions and isinstance(executed_actions[0], TradeExecutionIntent):
            executed_actions = intents_to_records(executed_actions)  # type: ignore[arg-type]
        else:
            executed_actions = intents_to_records(intents_from_records(executed_actions))

        content = str(final_answer or "").strip()
        if status == "completed":
            del executed_actions, tool_calls, run_id, run_type
            return content or "本轮已完成，但未生成额外说明。"

        del executed_actions, tool_calls, run_id, run_type, phase
        return f"执行失败：{str(error_message or '未知错误').strip() or '未知错误'}"

    def persist_persistent_session_user_message(
        self,
        *,
        db: Session,
        session: ChatSession,
        run_id: int,
        content: str,
        schedule_id: int | None,
        schedule_name: str | None,
        run_type: str,
        trigger_source: str,
    ) -> ChatMessageRecord:
        now_utc = self._require_hook("now_utc")
        record = ChatMessageRecord(
            session_id=session.id,
            role="user",
            content=content,
            source="automation_run",
            run_id=run_id,
            message_kind="live_turn",
            meta_payload={
                "trigger_source": trigger_source,
                "schedule_id": schedule_id,
                "schedule_name": schedule_name,
                "run_type": run_type,
            },
        )
        db.add(record)
        db.flush()
        session.last_message_at = now_utc()
        db.add(session)
        return record

    def slim_automation_tool_calls(
        self,
        tool_calls: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        if not isinstance(tool_calls, list):
            return None
        slimmed: list[dict[str, Any]] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            entry = {
                "name": item.get("name"),
                "tool_call_id": item.get("id") or item.get("tool_call_id"),
                "arguments": item.get("arguments"),
                "ok": result.get("ok"),
                "summary": result.get("summary") or result.get("error"),
            }
            executed_action = result.get("executed_action")
            if isinstance(executed_action, dict):
                entry["executed_action"] = executed_action
            slimmed.append(entry)
        return slimmed or None

    def persist_persistent_session_assistant_message(
        self,
        *,
        db: Session,
        session: ChatSession,
        run_id: int,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
        status: str,
        meta_payload: dict[str, Any] | None,
    ) -> ChatMessageRecord:
        now_utc = self._require_hook("now_utc")
        record = ChatMessageRecord(
            session_id=session.id,
            role="assistant",
            content=content,
            source="automation_run",
            run_id=run_id,
            message_kind="live_turn",
            tool_calls=self.slim_automation_tool_calls(tool_calls),
            meta_payload={"status": status, **(meta_payload or {})} or None,
        )
        db.add(record)
        db.flush()
        session.last_message_at = now_utc()
        db.add(session)
        return record

    def persist_persistent_session_system_message(
        self,
        *,
        db: Session,
        session: ChatSession,
        run_id: int,
        content: str,
        meta_payload: dict[str, Any] | None,
    ) -> ChatMessageRecord:
        now_utc = self._require_hook("now_utc")
        record = ChatMessageRecord(
            session_id=session.id,
            role="system",
            content=content,
            source="automation_run",
            run_id=run_id,
            message_kind="context_compaction",
            meta_payload=meta_payload or None,
        )
        db.add(record)
        db.flush()
        session.last_message_at = now_utc()
        db.add(session)
        return record

    def list_persistent_session_history_records(
        self,
        *,
        db: Session,
        session_id: int,
        recent_limit: int,
    ) -> list[ChatMessageRecord]:
        limit = max(4, int(recent_limit or AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT))
        session = db.get(ChatSession, session_id)
        last_compacted_message_id = int(getattr(session, "last_compacted_message_id", 0) or 0)
        stmt = (
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.id.desc())
            .limit(limit)
        )
        if last_compacted_message_id > 0:
            stmt = stmt.where(ChatMessageRecord.id > last_compacted_message_id)
        records = db.execute(stmt).scalars().all()
        records.reverse()
        return records

    def build_persistent_session_history_messages(
        self,
        records: list[ChatMessageRecord],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for record in records:
            if str(record.message_kind or "").strip() == "context_compaction":
                continue
            if record.role not in {"user", "assistant", "system"}:
                continue
            content = str(record.content or "").strip()
            if not content:
                continue
            messages.append({"role": record.role, "content": content})
        return messages

    def retrieve_persistent_session_memory_messages(
        self,
        *,
        session: ChatSession,
        settings: Any,
        run_type: str,
        task_prompt: str,
    ) -> list[dict[str, Any]]:
        del session, settings, run_type, task_prompt
        return []

    def estimate_persistent_session_context_tokens(
        self,
        *,
        session: ChatSession,
        settings: Any,
        messages: list[dict[str, Any]],
    ) -> int:
        del session
        estimate = estimate_messages_tokens(messages)
        estimate += estimate_text_tokens(getattr(settings, "system_prompt", None))
        return estimate

    def list_uncompacted_persistent_session_records(
        self,
        *,
        db: Session,
        session: ChatSession,
    ) -> list[ChatMessageRecord]:
        stmt = (
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session.id)
            .order_by(ChatMessageRecord.id.asc())
        )
        last_compacted_message_id = int(getattr(session, "last_compacted_message_id", 0) or 0)
        if last_compacted_message_id > 0:
            stmt = stmt.where(ChatMessageRecord.id > last_compacted_message_id)
        return db.execute(stmt).scalars().all()

    def build_persistent_session_context_system_message(
        self,
        *,
        session: ChatSession,
    ) -> dict[str, Any] | None:
        archived_summary = str(session.archived_summary or "").strip()
        if not archived_summary:
            return None
        return {"role": "system", "content": "[上下文压缩摘要]\n" + archived_summary}

    def build_persistent_session_prompt_messages(
        self,
        *,
        session: ChatSession,
        history_messages: list[dict[str, Any]],
        memory_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        context_message = self.build_persistent_session_context_system_message(session=session)
        if context_message is not None:
            messages.append(context_message)
        messages.extend(memory_messages)
        messages.extend(history_messages)
        return messages

    def build_compacted_summary_text(
        self,
        records: list[ChatMessageRecord],
    ) -> str | None:
        build_analysis_summary = self._require_hook("build_analysis_summary")
        if not records:
            return None
        assistant_records = [record for record in records if record.role == "assistant"]
        if not assistant_records:
            return None
        recent = assistant_records[-6:]
        lines = [
            "## 当前策略",
            "- 结合最近自动化运行的结论、失败记录和账户快照继续决策。",
            "## 已执行动作",
        ]
        for record in recent:
            summary = build_analysis_summary(record.content)
            run_id = record.run_id if record.run_id is not None else "--"
            if summary:
                lines.append(f"- run_id {run_id}: {summary}")
        lines.extend(
            [
                "## 当前约束",
                "- 原始运行记录和交易记录以 StrategyRun / TradeOrder 为准。",
                "- 账户实时数字应以本轮最新快照和工具结果为准。",
                "## 后续计划",
                "- 下一轮结合最新账户快照，延续、调整或推翻之前计划。",
            ]
        )
        return "\n".join(lines)

    def safe_prompt_budget(self, settings: Any) -> int:
        context_window = int(
            getattr(settings, "automation_context_window_tokens", 0)
            or AUTOMATION_DEFAULT_CONTEXT_WINDOW_TOKENS
        )
        return max(2048, int(context_window * AUTOMATION_COMPACTION_TRIGGER_RATIO))

    def should_compact_automation_session(
        self,
        *,
        session: ChatSession,
        records: list[ChatMessageRecord],
        settings: Any,
        estimated_tokens: int,
    ) -> bool:
        assume_utc = self._require_hook("assume_utc")
        now_utc = self._require_hook("now_utc")

        if not bool(getattr(settings, "automation_enable_auto_compaction", True)):
            return False
        recent_limit = int(
            getattr(settings, "automation_recent_message_limit", 0)
            or AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT
        )
        if len(records) > recent_limit:
            return True
        if estimated_tokens > self.safe_prompt_budget(settings):
            return True
        last_message_at = assume_utc(session.last_message_at)
        idle_hours = int(
            getattr(settings, "automation_idle_summary_hours", 0)
            or AUTOMATION_DEFAULT_IDLE_SUMMARY_HOURS
        )
        if last_message_at is not None and idle_hours > 0:
            if now_utc() - last_message_at >= timedelta(hours=idle_hours):
                return True
        return False

    def maybe_compact_persistent_session(
        self,
        *,
        db: Session,
        session: ChatSession,
        settings: Any,
        estimated_tokens: int,
    ) -> tuple[str | None, int | None]:
        now_utc = self._require_hook("now_utc")
        records = self.list_uncompacted_persistent_session_records(db=db, session=session)
        if not self.should_compact_automation_session(
            session=session,
            records=records,
            settings=settings,
            estimated_tokens=estimated_tokens,
        ):
            return session.archived_summary, session.summary_revision

        recent_limit = max(
            8,
            int(
                getattr(settings, "automation_recent_message_limit", 0)
                or AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT
            )
            // 2,
        )
        compact_cutoff = max(0, len(records) - recent_limit)
        compact_candidates = records[:compact_cutoff]
        if len(compact_candidates) < 2:
            return session.archived_summary, session.summary_revision
        if len(compact_candidates) % 2 == 1:
            compact_candidates = compact_candidates[:-1]
        if len(compact_candidates) < 2:
            return session.archived_summary, session.summary_revision

        summary = self.build_compacted_summary_text(compact_candidates)
        if not summary:
            return session.archived_summary, session.summary_revision

        session.archived_summary = summary
        session.summary_updated_at = now_utc()
        session.last_compacted_message_id = compact_candidates[-1].id
        last_run_id = compact_candidates[-1].run_id
        session.last_compacted_run_id = int(last_run_id) if last_run_id else None
        session.summary_revision = int(session.summary_revision or 0) + 1
        db.add(session)
        return session.archived_summary, session.summary_revision


automation_session_service = AutomationSessionService()
