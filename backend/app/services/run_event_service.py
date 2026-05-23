from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RunEvent


class RunEventService:
    def list_events(self, db: Session, run_id: int) -> list[dict[str, Any]]:
        events = db.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.sequence.asc(), RunEvent.id.asc())
        ).all()
        payloads: list[dict[str, Any]] = []
        for event in events:
            payload = dict(event.payload or {})
            payload.setdefault("type", event.event_type)
            payload.setdefault("run_id", run_id)
            payload.setdefault("sequence", int(event.sequence or 0))
            payload.setdefault(
                "ts",
                event.created_at.timestamp() if event.created_at is not None else None,
            )
            if event.state_name and "state" not in payload and "stage" not in payload:
                payload["state"] = event.state_name
            payloads.append(payload)
        return payloads


run_event_service = RunEventService()
