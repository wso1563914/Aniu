from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.schemas.aniu import ScheduleRead, ScheduleUpdate
from app.services.schedule_service import schedule_service

router = APIRouter(prefix="/schedule", tags=["aniu-schedule"])


@router.get("", response_model=list[ScheduleRead])
def get_schedule(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[ScheduleRead]:
    return schedule_service.list_schedules(db)


@router.put("", response_model=list[ScheduleRead])
def update_schedule(
    payload: list[ScheduleUpdate],
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[ScheduleRead]:
    return schedule_service.replace_schedules(db, payload)
