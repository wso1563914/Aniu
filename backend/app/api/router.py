from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api.routes.account import router as account_router
from app.api.routes.persistent_session import router as persistent_session_router
from app.api.routes.runs import router as runs_router
from app.api.routes.schedules import router as schedule_router
from app.api.routes.settings import router as settings_router
from app.core.auth import get_current_user
from app.db.database import get_db
from app.schemas.aniu import (
    AccountOverviewDebugRead,
    AccountOverviewRead,
    AppSettingsRead,
    AppSettingsUpdate,
    ChatAttachmentRead,
    ChatMessageRead,
    ChatRequest,
    ChatResponse,
    ChatSessionCreate,
    ChatSessionMessagesPageRead,
    ChatSessionRead,
    ChatSessionUpdate,
    PersistentSessionMessagesPageRead,
    PersistentSessionRead,
    ChatStreamRequest,
    LoginRequest,
    LoginResponse,
    RunDetailRead,
    RawToolPreviewDetailRead,
    RunSummaryRead,
    RunSummaryPageRead,
    ScheduleRead,
    ScheduleUpdate,
    SkillImportClawHubRequest,
    SkillImportSkillHubRequest,
    SkillInfoRead,
    SkillListItemRead,
)
from app.services.aniu_service import aniu_service
from app.services.chat_session_service import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_UPLOAD_BYTES,
    chat_session_service,
)
from app.services.event_bus import event_bus
from app.services.skill_admin_service import (
    MAX_SKILL_ARCHIVE_BYTES,
    skill_admin_service,
)

router = APIRouter(prefix="/api/aniu", tags=["aniu"])
router.include_router(account_router)
router.include_router(persistent_session_router)
router.include_router(settings_router)
router.include_router(schedule_router)
router.include_router(runs_router)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    try:
        return aniu_service.authenticate_login(payload.password)
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.get("/skills", response_model=list[SkillListItemRead])
def list_skills(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[SkillListItemRead]:
    return skill_admin_service.list_skills(db)


@router.post("/skills/import-clawhub", response_model=SkillInfoRead)
def import_clawhub_skill(
    payload: SkillImportClawHubRequest,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.import_from_clawhub(db, payload.slug_or_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/skills/import-skillhub", response_model=SkillInfoRead)
def import_skillhub_skill(
    payload: SkillImportSkillHubRequest,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.import_from_skillhub(db, payload.slug_or_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/skills/import-zip", response_model=SkillInfoRead)
def import_zip_skill(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    archive_bytes = file.file.read(MAX_SKILL_ARCHIVE_BYTES + 1)
    if len(archive_bytes) > MAX_SKILL_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"技能压缩包过大，当前最多支持 "
                f"{MAX_SKILL_ARCHIVE_BYTES // (1024 * 1024)}MB。"
            ),
        )
    try:
        return skill_admin_service.import_from_zip(
            db,
            filename=file.filename or "skill.zip",
            archive_bytes=archive_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/skills/reload", response_model=list[SkillListItemRead])
def reload_skills(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[SkillListItemRead]:
    return skill_admin_service.reload(db)


@router.post("/skills/{skill_id}/enable", response_model=SkillInfoRead)
def enable_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.set_enabled(db, skill_id=skill_id, enabled=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/skills/{skill_id}/disable", response_model=SkillInfoRead)
def disable_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> SkillInfoRead:
    try:
        return skill_admin_service.set_enabled(db, skill_id=skill_id, enabled=False)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/skills/{skill_id}", status_code=204)
def delete_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> None:
    try:
        skill_admin_service.delete_skill(db, skill_id=skill_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    _user: str = Depends(get_current_user),
) -> ChatResponse:
    try:
        return aniu_service.chat(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat-stream")
def chat_stream(
    payload: ChatRequest,
    _user: str = Depends(get_current_user),
) -> StreamingResponse:
    try:
        event_iter = aniu_service.chat_stream(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _generator():
        try:
            for event in event_iter:
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


@router.get("/chat/sessions", response_model=list[ChatSessionRead])
def list_chat_sessions(
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> list[ChatSessionRead]:
    return chat_session_service.list_sessions(db)


@router.post("/chat/sessions", response_model=ChatSessionRead)
def create_chat_session(
    payload: ChatSessionCreate | None = None,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatSessionRead:
    title = payload.title if payload else None
    result = chat_session_service.create_session(db, title=title)
    db.commit()
    return result


@router.patch("/chat/sessions/{session_id}", response_model=ChatSessionRead)
def rename_chat_session(
    session_id: int,
    payload: ChatSessionUpdate,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatSessionRead:
    try:
        result = chat_session_service.rename_session(
            db, session_id, title=payload.title
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return result


@router.delete("/chat/sessions/{session_id}", status_code=204)
def delete_chat_session(
    session_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> None:
    try:
        chat_session_service.delete_session(db, session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()


@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=ChatSessionMessagesPageRead,
)
def list_chat_messages(
    session_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatSessionMessagesPageRead:
    try:
        session, messages, next_before_id, has_more = chat_session_service.list_messages(
            db,
            session_id,
            limit=limit,
            before_id=before_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "session": session.model_dump(mode="json"),
        "messages": [m.model_dump(mode="json") for m in messages],
        "next_before_id": next_before_id,
        "has_more": has_more,
    }


@router.post("/chat/uploads", response_model=ChatAttachmentRead)
def upload_chat_attachment(
    file: UploadFile = File(...),
    session_id: int | None = Form(default=None),
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> ChatAttachmentRead:
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大，最大允许 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB。",
        )
    try:
        result = chat_session_service.save_attachment(
            db,
            filename=file.filename or "upload.bin",
            mime_type=file.content_type or "application/octet-stream",
            data=data,
            session_id=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.get("/chat/uploads/{attachment_id}")
def download_chat_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
    _user: str = Depends(get_current_user),
) -> FileResponse:
    try:
        path, mime_type, filename = chat_session_service.get_attachment_file(
            db, attachment_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(str(path), media_type=mime_type, filename=filename)


@router.post("/chat/stream")
def chat_session_stream(
    payload: ChatStreamRequest,
    _user: str = Depends(get_current_user),
) -> StreamingResponse:
    try:
        event_iter = chat_session_service.stream_chat(payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _generator():
        try:
            for event in event_iter:
                event_type = str(event.get("type") or "message")
                data = json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = json.dumps(
                {"type": "failed", "message": str(exc)}, ensure_ascii=False
            )
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
