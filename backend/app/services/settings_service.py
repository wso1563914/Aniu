from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.constants import DEFAULT_SYSTEM_PROMPT
from app.db.models import AppSettings
from app.domain.schedule.policy import assume_utc
from app.schemas.aniu import AppSettingsUpdate

logger = logging.getLogger(__name__)


class SettingsService:
    def get_or_create_settings(self, db: Session) -> AppSettings:
        instance = db.scalar(select(AppSettings).limit(1))
        if instance is None:
            env = get_settings()
            instance = AppSettings(
                provider_name="openai-compatible",
                mx_api_key=env.mx_apikey,
                llm_base_url=env.openai_base_url,
                llm_api_key=env.openai_api_key,
                llm_model=env.openai_model,
                system_prompt=DEFAULT_SYSTEM_PROMPT,
            )
            db.add(instance)
            db.commit()
            db.refresh(instance)
        instance.created_at = assume_utc(instance.created_at)
        instance.updated_at = assume_utc(instance.updated_at)
        return instance

    def update_settings(self, db: Session, payload: AppSettingsUpdate) -> AppSettings:
        instance = self.get_or_create_settings(db)
        sensitive_fields = {"mx_api_key", "llm_api_key"}
        changed_fields: list[str] = []
        for field, value in payload.model_dump().items():
            if field in sensitive_fields and isinstance(value, str) and "****" in value:
                continue
            old_value = getattr(instance, field, None)
            if old_value != value:
                changed_fields.append(field)
            setattr(instance, field, value)
        db.add(instance)
        db.commit()
        db.refresh(instance)
        instance.created_at = assume_utc(instance.created_at)
        instance.updated_at = assume_utc(instance.updated_at)
        logger.info("settings updated: changed_fields=%s", changed_fields)
        return instance


settings_service = SettingsService()
