from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.schemas.aniu import PersistentSessionMessagesPageRead, PersistentSessionRead
from app.services.aniu_service import aniu_service

router = APIRouter(tags=["aniu-persistent-session"])


@router.get("/persistent-session", response_model=PersistentSessionRead)
def get_persistent_session(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> PersistentSessionRead:
    return aniu_service.get_persistent_session(db)


@router.get(
    "/persistent-session/messages",
    response_model=PersistentSessionMessagesPageRead,
)
def list_persistent_session_messages(
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> PersistentSessionMessagesPageRead:
    session, messages, next_before_id, has_more = aniu_service.list_persistent_session_messages(
        db,
        limit=limit,
        before_id=before_id,
    )
    return {
        "session": session.model_dump(mode="json"),
        "messages": [m.model_dump(mode="json") for m in messages],
        "next_before_id": next_before_id,
        "has_more": has_more,
    }


@router.delete("/persistent-session", status_code=204)
def delete_persistent_session(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> None:
    aniu_service.delete_persistent_session(db)
    db.commit()
