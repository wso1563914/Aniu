from __future__ import annotations

import logging
from threading import Thread
from types import SimpleNamespace
from typing import Any

from app.agent.kernel.fsm import AgentRunContext
from app.agent.kernel.runner import AgentRunner
from app.domain.trading.intents import intents_from_records
from app.events.publisher import run_event_publisher

logger = logging.getLogger(__name__)


class RunInvocationError(Exception):
    def __init__(
        self,
        original_exception: Exception,
        *,
        settings: Any,
        session_context: Any = None,
        phase: str = "llm",
    ) -> None:
        super().__init__(str(original_exception))
        self.original_exception = original_exception
        self.settings = settings
        self.session_context = session_context
        self.phase = phase


class RunService:
    def __init__(self) -> None:
        self._hooks: dict[str, Any] = {}

    def configure_hooks(self, **hooks: Any) -> None:
        self._hooks = {**self._hooks, **hooks}

    def _require_hook(self, name: str) -> Any:
        value = self._hooks.get(name)
        if value is None:
            raise RuntimeError(f"run service hook '{name}' is not configured")
        return value

    def _call_hook(self, name: str, *args: Any, **kwargs: Any) -> Any:
        value = self._require_hook(name)
        if isinstance(value, tuple) and len(value) == 2:
            owner, attr_name = value
            target = getattr(owner, attr_name)
            return target(*args, **kwargs)
        if callable(value):
            return value(*args, **kwargs)
        if args or kwargs:
            raise RuntimeError(f"run service hook '{name}' is not callable")
        return value

    def execute_run(
        self,
        *,
        trigger_source: str = "manual",
        schedule_id: int | None = None,
        manual_run_type: str | None = None,
    ):
        run_lock = self._require_hook("run_lock")

        if not run_lock.acquire(blocking=False):
            raise RuntimeError("已有运行中的任务，请稍后再试。")
        try:
            run_id, settings_snapshot = self._call_hook(
                "prepare_run",
                trigger_source,
                schedule_id,
                manual_run_type,
            )

            def _publish(event_type: str, **data: Any) -> None:
                run_event_publisher.publish(
                    run_id=run_id,
                    event_type=event_type,
                    data=data or None,
                )

            setattr(_publish, "_persist_run_events", True)

            return self._call_hook(
                "run_body",
                run_id=run_id,
                settings_snapshot=settings_snapshot,
                trigger_source=trigger_source,
                schedule_id=schedule_id,
                emit=_publish,
            )
        finally:
            run_lock.release()

    def start_run_async(
        self,
        *,
        trigger_source: str = "manual",
        schedule_id: int | None = None,
        manual_run_type: str | None = None,
    ) -> int:
        run_lock = self._require_hook("run_lock")

        if not run_lock.acquire(blocking=False):
            raise RuntimeError("已有运行中的任务，请稍后再试。")

        run_id: int | None = None
        try:
            run_id, settings_snapshot = self._call_hook(
                "prepare_run",
                trigger_source,
                schedule_id,
                manual_run_type,
            )
        except Exception:
            run_lock.release()
            raise

        def _publish(event_type: str, **data: Any) -> None:
            run_event_publisher.publish(
                run_id=run_id,
                event_type=event_type,
                data=data or None,
            )

        setattr(_publish, "_persist_run_events", True)

        def _worker() -> None:
            try:
                self._call_hook(
                    "run_body",
                    run_id=run_id,
                    settings_snapshot=settings_snapshot,
                    trigger_source=trigger_source,
                    schedule_id=schedule_id,
                    emit=_publish,
                    return_full_run=False,
                )
            except Exception:
                logger.exception("async run worker failed: run_id=%s", run_id)
            finally:
                run_lock.release()

        Thread(target=_worker, name=f"run-{run_id}", daemon=True).start()
        return int(run_id)

    def process_due_schedule(self) -> None:
        session_scope = self._require_hook("session_scope")
        now_shanghai = self._require_hook("now_shanghai")
        assume_utc = self._require_hook("assume_utc")
        trading_calendar_service = self._require_hook("trading_calendar_service")

        due_schedule_id: int | None = None
        with session_scope() as db:
            schedules = self._call_hook("list_schedules", db)
            now = now_shanghai()
            earliest_due_at = None
            for schedule in schedules:
                if not schedule.enabled:
                    continue
                if schedule.next_run_at is None:
                    schedule.next_run_at = self._call_hook(
                        "compute_next_run_at",
                        schedule.cron_expression,
                    )
                    db.add(schedule)
                    continue
                if not trading_calendar_service.is_trading_day(now.date()):
                    schedule.next_run_at = self._call_hook(
                        "compute_next_run_at",
                        schedule.cron_expression,
                        from_time=now,
                    )
                    db.add(schedule)
                    continue
                retry_after_at = assume_utc(schedule.retry_after_at)
                if retry_after_at is not None:
                    retry_due = retry_after_at.astimezone(now.tzinfo)
                    if retry_due <= now:
                        if earliest_due_at is None or retry_due < earliest_due_at:
                            earliest_due_at = retry_due
                            due_schedule_id = schedule.id
                        continue
                if (
                    schedule.next_run_at is not None
                    and schedule.next_run_at.astimezone(now.tzinfo) <= now
                ):
                    schedule_due = schedule.next_run_at.astimezone(now.tzinfo)
                    if earliest_due_at is None or schedule_due < earliest_due_at:
                        earliest_due_at = schedule_due
                        due_schedule_id = schedule.id

        if due_schedule_id is not None:
            try:
                self._call_hook(
                    "execute_run",
                    trigger_source="schedule",
                    schedule_id=due_schedule_id,
                )
            except RuntimeError as exc:
                if "已有运行中的任务" in str(exc):
                    logger.info(
                        "process_due_schedule skipped because another run is active: schedule_id=%s",
                        due_schedule_id,
                    )
                    return
                raise

    def build_runtime_context(
        self,
        *,
        run_id: int,
        settings_snapshot: dict[str, Any],
        trigger_source: str,
        schedule_id: int | None,
        emit: Any = None,
    ) -> tuple[AgentRunContext, AgentRunner]:
        context = AgentRunContext(
            run_id=run_id,
            settings_snapshot=settings_snapshot,
            trigger_source=trigger_source,
            schedule_id=schedule_id,
        )
        return context, AgentRunner(emit=emit)

    def invoke_llm_run(
        self,
        *,
        agent_runner: AgentRunner,
        runtime_context: AgentRunContext,
        session_scope: Any,
        prepare_persistent_session_context: Any,
        llm_runner: Any,
        settings_snapshot: dict[str, Any],
        trigger_source: str,
        schedule_id: int | None,
        build_skill_context: Any,
        mx_client_cls: Any,
        emit: Any,
    ) -> tuple[Any, Any, Any, Any, Any, Any]:
        settings = SimpleNamespace(**settings_snapshot)
        mx_client_config = build_skill_context(
            run_type=getattr(settings, "run_type", "analysis"),
            app_settings=settings,
        )["mx_client_config"]
        if not mx_client_config.get("api_key"):
            raise RuntimeError("未配置 MX API Key，请先在设置页保存后再运行。")

        client = mx_client_cls(
            api_key=mx_client_config.get("api_key"),
            base_url=mx_client_config.get("base_url"),
        )
        session_context = None
        try:
            agent_runner.transition(
                context=runtime_context,
                phase="observe",
                stage="llm",
                message="正在准备运行上下文",
            )
            with session_scope() as db:
                session_context = prepare_persistent_session_context(
                    db=db,
                    run_id=runtime_context.run_id,
                    settings=settings,
                    trigger_source=trigger_source,
                    schedule_id=schedule_id,
                )
            agent_runner.transition(
                context=runtime_context,
                phase="analyze",
                stage="llm",
                message="正在调用大模型",
            )
            decision, llm_request, llm_response, runtime_trace = llm_runner(
                app_settings=settings,
                client=client,
                messages=session_context.messages,
                emit=emit,
            )
            return settings, session_context, decision, llm_request, llm_response, runtime_trace
        except Exception as exc:
            raise RunInvocationError(
                exc,
                settings=settings,
                session_context=session_context,
                phase="llm",
            ) from exc
        finally:
            client.close()

    def persist_successful_run(
        self,
        *,
        session_scope: Any,
        run_id: int,
        session_context: Any,
        skill_payloads: dict[str, Any],
        llm_request: Any,
        llm_response: Any,
        decision: dict[str, Any],
        proposals: list[dict[str, Any]],
        policy_decisions: list[dict[str, Any]],
        executed_actions: list[dict[str, Any]],
        schedule_id: int | None,
        completed_at: Any,
        completed_at_shanghai: Any,
        build_analysis_summary: Any,
        parse_price: Any,
        trade_order_cls: Any,
        strategy_run_cls: Any,
        strategy_schedule_cls: Any,
        chat_session_cls: Any,
        build_assistant_content: Any,
        persist_assistant_message: Any,
        maybe_compact_session: Any,
        persist_system_message: Any,
        compute_next_run_at: Any,
        emit_db: Any,
        settings: Any,
        tool_calls: Any,
        return_full_run: bool,
    ) -> None:
        with session_scope() as db:
            run = db.get(strategy_run_cls, run_id)
            if run is None:
                raise RuntimeError("运行记录不存在。")
            run.chat_session_id = session_context.session_id if session_context else None
            run.prompt_message_id = session_context.prompt_message_id if session_context else None
            run.context_summary_version = session_context.summary_revision if session_context else None
            run.context_tokens_estimate = (
                session_context.context_tokens_estimate if session_context else None
            )
            run.skill_payloads = skill_payloads
            run.llm_request_payload = llm_request
            run.llm_response_payload = llm_response
            run.decision_payload = {
                **decision,
                "proposals": proposals,
                "policy_decisions": policy_decisions,
            }
            run.analysis_summary = build_analysis_summary(decision.get("final_answer"))
            run.final_answer = str(decision.get("final_answer") or "").strip() or None
            run.executed_actions = executed_actions
            run.status = "completed"
            run.finished_at = completed_at
            db.add(run)

            for action in intents_from_records(executed_actions):
                if str(action.action or "") not in {"BUY", "SELL"}:
                    continue
                db.add(
                    trade_order_cls(
                        run_id=run_id,
                        symbol=action.symbol,
                        action=action.action,
                        quantity=action.quantity,
                        price_type=action.price_type,
                        price=parse_price(action.price),
                        status=action.status,
                        response_payload=action.response,
                    )
                )

            if schedule_id:
                schedule = db.get(strategy_schedule_cls, schedule_id)
                if schedule is not None:
                    schedule.last_run_at = completed_at
                    schedule.retry_count = 0
                    schedule.retry_after_at = None
                    schedule.next_run_at = compute_next_run_at(
                        schedule.cron_expression,
                        from_time=completed_at_shanghai,
                    )
                    db.add(schedule)

            if session_context is not None:
                session = db.get(chat_session_cls, session_context.session_id)
                if session is not None:
                    previous_summary_revision = int(session.summary_revision or 0)
                    assistant_content = build_assistant_content(
                        run_id=run_id,
                        run_type=str(getattr(settings, "run_type", "analysis") or "analysis"),
                        status="completed",
                        final_answer=str(decision.get("final_answer") or "").strip() or None,
                        tool_calls=tool_calls if isinstance(tool_calls, list) else None,
                        executed_actions=executed_actions,
                    )
                    response_message = persist_assistant_message(
                        db=db,
                        session=session,
                        run_id=run_id,
                        content=assistant_content,
                        tool_calls=tool_calls if isinstance(tool_calls, list) else None,
                        status="completed",
                        meta_payload={
                            "run_type": str(getattr(settings, "run_type", "analysis") or "analysis"),
                            "executed_action_count": len(executed_actions),
                        },
                    )
                    session_context.response_message_id = response_message.id
                    run.response_message_id = response_message.id
                    archived_summary, summary_version = maybe_compact_session(
                        db=db,
                        session=session,
                        settings=settings,
                        estimated_tokens=int(session_context.context_tokens_estimate or 0),
                    )
                    if (
                        summary_version is not None
                        and int(summary_version) > previous_summary_revision
                        and str(archived_summary or "").strip()
                    ):
                        summary_message = persist_system_message(
                            db=db,
                            session=session,
                            run_id=run_id,
                            content="[上下文压缩摘要]\n" + str(archived_summary).strip(),
                            meta_payload={
                                "summary_revision": int(summary_version),
                                "last_compacted_run_id": session.last_compacted_run_id,
                            },
                        )
                        emit_db(
                            db,
                            "context_compacted",
                            message="已生成上下文压缩摘要",
                            content=summary_message.content,
                            summary_revision=int(summary_version),
                            message_id=summary_message.id,
                            run_id=run_id,
                        )
                    run.context_summary_version = (
                        int(summary_version)
                        if summary_version is not None
                        else run.context_summary_version
                    )
                    db.add(run)

            db.flush()

            if not return_full_run:
                emit_db(
                    db,
                    "completed",
                    message="任务完成",
                    actions=len(executed_actions),
                )

    def persist_failed_run(
        self,
        *,
        session_scope: Any,
        run_id: int,
        session_context: Any,
        schedule_id: int | None,
        trigger_source: str,
        settings_snapshot: dict[str, Any],
        automation_phase: str,
        error: Exception,
        now_utc: Any,
        now_shanghai: Any,
        compute_next_run_at: Any,
        schedule_max_retries: int,
        schedule_retry_delay: Any,
        strategy_run_cls: Any,
        strategy_schedule_cls: Any,
        chat_session_cls: Any,
        build_assistant_content: Any,
        persist_assistant_message: Any,
        emit_db: Any,
    ) -> None:
        with session_scope() as db:
            schedule = None
            run = db.get(strategy_run_cls, run_id)
            if run is not None:
                run.chat_session_id = session_context.session_id if session_context else None
                run.prompt_message_id = session_context.prompt_message_id if session_context else None
                run.response_message_id = session_context.response_message_id if session_context else None
                run.context_summary_version = session_context.summary_revision if session_context else None
                run.context_tokens_estimate = (
                    session_context.context_tokens_estimate if session_context else None
                )
                run.status = "failed"
                run.error_message = str(error)
                run.final_answer = None
                run.finished_at = now_utc()
                db.add(run)
                if session_context is not None:
                    session = db.get(chat_session_cls, session_context.session_id)
                    if session is not None:
                        assistant_content = build_assistant_content(
                            run_id=run_id,
                            run_type=str(settings_snapshot.get("run_type") or "analysis"),
                            status="failed",
                            final_answer=None,
                            tool_calls=None,
                            executed_actions=None,
                            error_message=str(error),
                            phase=automation_phase,
                        )
                        response_message = persist_assistant_message(
                            db=db,
                            session=session,
                            run_id=run_id,
                            content=assistant_content,
                            tool_calls=None,
                            status="failed",
                            meta_payload={
                                "phase": automation_phase,
                                "run_type": str(settings_snapshot.get("run_type") or "analysis"),
                            },
                        )
                        run.response_message_id = response_message.id
                        session_context.response_message_id = response_message.id
                        db.add(run)
                if schedule_id:
                    schedule = db.get(strategy_schedule_cls, schedule_id)
                    if schedule is not None:
                        schedule.last_run_at = now_utc()
                        schedule.next_run_at = compute_next_run_at(
                            schedule.cron_expression,
                            from_time=now_shanghai(),
                        )
                        if trigger_source == "schedule":
                            retry_count = max(int(schedule.retry_count or 0), 0)
                            if retry_count < schedule_max_retries:
                                schedule.retry_count = retry_count + 1
                                schedule.retry_after_at = now_utc() + schedule_retry_delay
                            else:
                                schedule.retry_count = 0
                                schedule.retry_after_at = None
                        else:
                            schedule.retry_count = max(int(schedule.retry_count or 0), 0)
                        db.add(schedule)
            emit_db(
                db,
                "stage",
                stage="failed",
                fsm_phase="failed",
                message=str(error),
            )
            emit_db(db, "failed", message=str(error))


run_service = RunService()
