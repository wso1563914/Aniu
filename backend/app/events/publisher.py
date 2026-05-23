from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.database import session_scope
from app.db.models import RunEvent
from app.services.event_bus import event_bus


class RunEventPublisher:
    def __init__(self) -> None:
        self._locks: dict[int, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, run_id: int) -> threading.Lock:
        with self._global_lock:
            lock = self._locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[run_id] = lock
            return lock

    def publish(
        self,
        *,
        run_id: int,
        event_type: str,
        data: dict[str, Any] | None = None,
        db: Session | None = None,
    ) -> None:
        payload = dict(data or {})
        state_name = payload.get("state") or payload.get("stage")
        if state_name is not None:
            state_name = str(state_name)

        lock = self._get_lock(run_id)
        with lock:
            if db is not None:
                max_sequence = db.scalar(
                    select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
                )
                sequence = int(max_sequence or 0) + 1
                db.add(
                    RunEvent(
                        run_id=run_id,
                        sequence=sequence,
                        event_type=event_type,
                        state_name=state_name,
                        payload=payload or None,
                    )
                )
            else:
                with session_scope() as managed_db:
                    max_sequence = managed_db.scalar(
                        select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
                    )
                    sequence = int(max_sequence or 0) + 1
                    managed_db.add(
                        RunEvent(
                            run_id=run_id,
                            sequence=sequence,
                            event_type=event_type,
                            state_name=state_name,
                            payload=payload or None,
                        )
                    )
            payload.setdefault("sequence", sequence)
            event_bus.publish(run_id, event_type, payload or None)


run_event_publisher = RunEventPublisher()
