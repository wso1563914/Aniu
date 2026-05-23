from __future__ import annotations

import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.schemas.aniu import RawToolPreviewDetailRead, RunDetailRead, RunSummaryPageRead, RunSummaryRead
from app.services.aniu_service import aniu_service
from app.services.event_bus import event_bus
from app.services.run_event_service import run_event_service
from app.services.run_query_service import run_query_service

router = APIRouter(tags=["aniu-runs"])


@router.post("/run", response_model=RunDetailRead)
def run_once(
    schedule_id: int | None = Query(default=None, ge=1),
    run_type: Literal["analysis", "trade"] | None = Query(default=None),
    _user: str = Depends(get_current_user),
) -> RunDetailRead:
    try:
        return aniu_service.execute_run(
            trigger_source="manual",
            schedule_id=schedule_id,
            manual_run_type=run_type,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/run-stream")
def run_stream(
    schedule_id: int | None = Query(default=None, ge=1),
    run_type: Literal["analysis", "trade"] | None = Query(default=None),
    _user: str = Depends(get_current_user),
) -> dict:
    try:
        run_id = aniu_service.start_run_async(
            trigger_source="manual",
            schedule_id=schedule_id,
            manual_run_type=run_type,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"run_id": run_id}


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> StreamingResponse:
    def _generator():
        try:
            persisted = run_event_service.list_events(db, run_id)
            seen_sequences = {
                int(item.get("sequence"))
                for item in persisted
                if isinstance(item.get("sequence"), int)
            }
            for event in persisted:
                event_type = str(event.get("type") or "message")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
            for event in event_bus.stream(run_id):
                sequence = event.get("sequence")
                if isinstance(sequence, int) and sequence in seen_sequences:
                    continue
                event_type = str(event.get("type") or "message")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"type": "failed", "message": str(exc)}, ensure_ascii=False)
            yield f"event: failed\ndata: {err}\n\n"

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/runs", response_model=list[RunSummaryRead])
def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    run_date: date | None = Query(default=None, alias="date"),
    status: str | None = Query(default=None),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[RunSummaryRead]:
    return run_query_service.list_runs(
        db,
        limit=limit,
        run_date=run_date,
        status=status,
        before_id=before_id,
    )


@router.get("/runs-feed", response_model=RunSummaryPageRead)
def list_runs_feed(
    limit: int = Query(default=20, ge=1, le=100),
    run_date: date | None = Query(default=None, alias="date"),
    status: str | None = Query(default=None),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> RunSummaryPageRead:
    return run_query_service.list_runs_page(
        db,
        limit=limit,
        run_date=run_date,
        status=status,
        before_id=before_id,
    )


@router.get("/runs/{run_id}", response_model=RunDetailRead)
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> RunDetailRead:
    run = run_query_service.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在。")
    return run


@router.get(
    "/runs/{run_id}/raw-tool-previews/{preview_index}",
    response_model=RawToolPreviewDetailRead,
)
def get_run_raw_tool_preview(
    run_id: int,
    preview_index: int = Path(ge=0),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> RawToolPreviewDetailRead:
    try:
        return run_query_service.get_run_raw_tool_preview(db, run_id, preview_index)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(
    run_id: int,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> None:
    try:
        aniu_service.delete_run(db, run_id, force=force)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
