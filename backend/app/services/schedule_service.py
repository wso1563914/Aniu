from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.models import StrategySchedule
from app.domain.schedule.policy import assume_utc, compute_next_run_at, resolve_schedule_run_type
from app.schemas.aniu import ScheduleUpdate

logger = logging.getLogger(__name__)


class ScheduleService:
    def resolve_run_type(self, schedule: StrategySchedule | None) -> str:
        if schedule is None:
            return "analysis"
        return resolve_schedule_run_type(schedule.name, schedule.run_type)

    def compute_next_run_at(
        self,
        cron_expression: str | None,
        *,
        from_time=None,
    ):
        return compute_next_run_at(cron_expression, from_time=from_time)

    def list_schedules(self, db: Session) -> list[StrategySchedule]:
        schedules = list(db.query(StrategySchedule).order_by(StrategySchedule.id.asc()).all())
        mutated = False
        for schedule in schedules:
            if not schedule.name:
                schedule.name = "默认任务"
                mutated = True
            normalized_run_type = resolve_schedule_run_type(schedule.name, schedule.run_type)
            if str(schedule.run_type or "").strip() != normalized_run_type:
                schedule.run_type = normalized_run_type
                mutated = True
            if not schedule.cron_expression:
                schedule.cron_expression = "*/30 * * * *"
                mutated = True
            if not schedule.task_prompt:
                schedule.task_prompt = "请根据当前市场和持仓情况生成交易决策。"
                mutated = True
            if not schedule.timeout_seconds or schedule.timeout_seconds <= 0:
                schedule.timeout_seconds = 1800
                mutated = True
            if schedule.retry_count < 0:
                schedule.retry_count = 0
                mutated = True
            if schedule.enabled and schedule.next_run_at is None:
                schedule.next_run_at = compute_next_run_at(schedule.cron_expression)
                mutated = True
        if mutated:
            db.commit()
            for schedule in schedules:
                db.refresh(schedule)
        if not schedules:
            instance = StrategySchedule(
                name="默认任务",
                run_type="analysis",
                cron_expression="*/30 * * * *",
                task_prompt="请根据当前市场和持仓情况生成交易决策。",
                timeout_seconds=1800,
                enabled=False,
            )
            db.add(instance)
            db.commit()
            db.refresh(instance)
            schedules = [instance]
        for schedule in schedules:
            schedule.retry_count = max(int(schedule.retry_count or 0), 0)
            schedule.last_run_at = assume_utc(schedule.last_run_at)
            schedule.next_run_at = assume_utc(schedule.next_run_at)
            schedule.retry_after_at = assume_utc(schedule.retry_after_at)
            schedule.created_at = assume_utc(schedule.created_at)
            schedule.updated_at = assume_utc(schedule.updated_at)
        return schedules

    def replace_schedules(
        self,
        db: Session,
        payloads: list[ScheduleUpdate],
    ) -> list[StrategySchedule]:
        existing = {item.id: item for item in self.list_schedules(db)}
        keep_ids: set[int] = set()

        for payload in payloads:
            data = payload.model_dump()
            schedule_id = data.pop("id", None)
            if schedule_id is not None and schedule_id in existing:
                instance = existing[schedule_id]
            else:
                instance = StrategySchedule()
                db.add(instance)
                db.flush()

            for field, value in data.items():
                setattr(instance, field, value)

            instance.run_type = resolve_schedule_run_type(instance.name, instance.run_type)
            instance.next_run_at = compute_next_run_at(instance.cron_expression)
            db.add(instance)
            db.flush()
            keep_ids.add(instance.id)

        for schedule_id, instance in existing.items():
            if schedule_id not in keep_ids:
                db.delete(instance)

        db.commit()
        logger.info(
            "schedules replaced: kept=%s, deleted=%s",
            keep_ids,
            set(existing.keys()) - keep_ids,
        )
        return self.list_schedules(db)


schedule_service = ScheduleService()
