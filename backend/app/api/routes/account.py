from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.schemas.aniu import AccountOverviewDebugRead, AccountOverviewRead
from app.services.aniu_service import aniu_service

router = APIRouter(prefix="/account", tags=["aniu-account"])


@router.get("", response_model=AccountOverviewRead)
def get_account(
    force_refresh: bool = Query(default=False),
    _user: str = Depends(get_current_user),
) -> AccountOverviewRead:
    try:
        return aniu_service.get_account_overview(force_refresh=force_refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/debug", response_model=AccountOverviewDebugRead)
def get_account_debug(
    force_refresh: bool = Query(default=False),
    _user: str = Depends(get_current_user),
) -> AccountOverviewDebugRead:
    try:
        return aniu_service.get_account_overview(include_raw=True, force_refresh=force_refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
