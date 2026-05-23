from __future__ import annotations

import inspect
import json
import logging
import app.services.account_service as account_service_module
import queue
import secrets
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.auth import create_access_token
from app.core.config import get_settings
from app.core.constants import DEFAULT_SYSTEM_PROMPT
from app.db.database import session_scope
from app.db.models import (
    AppSettings,
    ChatMessageRecord,
    ChatSession,
    StrategyRun,
    StrategySchedule,
    TradeOrder,
)
from app.domain.schedule.policy import (
    ANALYSIS_TASK_NAMES,
    SCHEDULE_MAX_RETRIES,
    SCHEDULE_RETRY_DELAY,
    assume_utc as schedule_assume_utc,
    compute_next_run_at,
    now_utc as schedule_now_utc,
    now_shanghai as schedule_now_shanghai,
    resolve_schedule_run_type,
)
from app.domain.trading.intents import (
    PolicyDecision,
    TradeExecutionIntent,
    TradeProposal,
    intents_from_proposals,
    intents_to_records,
    proposals_to_records,
)
from app.domain.trading.risk_gate import risk_gate
from app.schemas.aniu import AppSettingsUpdate, ChatRequest, ScheduleUpdate
from app.schemas.aniu import ChatMessageRead, PersistentSessionRead
from app.skills.providers import build_skill_context
from app.services.event_bus import event_bus
from app.events.publisher import run_event_publisher
from app.services.automation_session_service import (
    AUTOMATION_COMPACTION_TRIGGER_RATIO,
    AUTOMATION_DEFAULT_CONTEXT_WINDOW_TOKENS,
    AUTOMATION_DEFAULT_IDLE_SUMMARY_HOURS,
    AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT,
    AUTOMATION_SESSION_SLUG,
    AUTOMATION_SESSION_TITLE,
    PersistentRunSessionContext,
    automation_session_service,
)
from app.services.llm_service import LLMStreamCancelled, llm_service
from app.services.account_service import account_service
from app.services.run_service import run_service
from app.services.run_service import RunInvocationError
from app.services.run_query_service import run_query_service
from app.services.schedule_service import schedule_service
from app.services.settings_service import settings_service
from app.services.token_estimator import estimate_messages_tokens, estimate_text_tokens
from app.services.trading_calendar_service import trading_calendar_service
from skills.mx_core.client import MXClient
from skills.mx_core.execution import mx_execution_service


logger = logging.getLogger(__name__)

RAW_TOOL_PREVIEW_MAX_CHARS = 6000

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT_PREFETCH_TOOL_NAMES = (
    "mx_get_balance",
    "mx_get_positions",
    "mx_get_orders",
)
ACCOUNT_OVERVIEW_CACHE_MAX_WORKERS = 3
def now_utc() -> datetime:
    return schedule_now_utc()


def now_shanghai() -> datetime:
    return schedule_now_shanghai()


def _assume_utc(value: datetime | None) -> datetime | None:
    return schedule_assume_utc(value)


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100.0


def _scaled_decimal(value: Any, decimal_places: Any) -> float | None:
    numeric = _parse_float(value)
    if numeric is None:
        return None

    decimals = _parse_float(decimal_places)
    scale = int(decimals) if decimals is not None else 0
    if scale <= 0:
        return numeric
    return numeric / (10**scale)


def _market_suffix(value: Any) -> str:
    mapping = {
        0: "SZ",
        1: "SH",
    }
    numeric = _parse_float(value)
    if numeric is None:
        return ""
    return mapping.get(int(numeric), "")


def _format_open_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text or None


def _format_timestamp(value: Any) -> str | None:
    numeric = _parse_float(value)
    if numeric is None:
        return None
    if numeric > 10_000_000_000:
        numeric = numeric / 1000
    try:
        return datetime.fromtimestamp(numeric, tz=SHANGHAI_TZ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _order_status_text(
    value: Any,
    *,
    filled_quantity: Any = None,
    order_quantity: Any = None,
    db_status: Any = None,
) -> str:
    filled = int(_parse_float(filled_quantity) or 0)
    total = int(_parse_float(order_quantity) or 0)
    if total > 0:
        if filled >= total and filled > 0:
            return "已成交"
        if 0 < filled < total:
            return "部分成交"

    mapping = {
        "0": "未知",
        "1": "已报",
        "2": "已报",
        "3": "已撤单",
        "4": "已成交",
        "8": "未成交",
        "9": "已撤单",
        "100": "处理中",
        "200": "已完成",
        "206": "已撤单",
    }
    text = str(value or "").strip()
    if text == "" and db_status is not None:
        text = str(db_status).strip()
    return mapping.get(text, text or "未知")


class AniuService:
    def __init__(self) -> None:
        self._run_lock = Lock()
        self._sync_account_cache_fields()
        self._configure_automation_session_hooks()
        self._configure_run_service_hooks()

    def _sync_account_cache_fields(self) -> None:
        self._account_cache_lock = account_service._account_cache_lock
        self._account_overview_cache = account_service._account_overview_cache
        self._account_overview_cache_expires_at = (
            account_service._account_overview_cache_expires_at
        )

    def _configure_automation_session_hooks(self) -> None:
        automation_session_service.configure_hooks(
            settings_loader=self.get_or_create_settings,
            assume_utc=_assume_utc,
            now_utc=now_utc,
            now_shanghai=now_shanghai,
            build_analysis_summary=self._build_analysis_summary,
        )

    def _configure_run_service_hooks(self) -> None:
        run_service.configure_hooks(
            run_lock=self._run_lock,
            prepare_run=(self, "_prepare_run"),
            run_body=(self, "_run_body"),
            list_schedules=(self, "list_schedules"),
            execute_run=(self, "execute_run"),
            session_scope=session_scope,
            now_shanghai=lambda: now_shanghai(),
            assume_utc=_assume_utc,
            compute_next_run_at=(self, "_compute_next_run_at"),
            trading_calendar_service=trading_calendar_service,
        )

    def _resolve_run_type(self, schedule: StrategySchedule | None) -> str:
        return schedule_service.resolve_run_type(schedule)

    def _resolve_manual_run_profile(
        self,
        *,
        settings: AppSettings | Any,
        manual_run_type: str | None,
    ) -> tuple[str, str]:
        normalized = str(manual_run_type or "").strip().lower()
        if normalized == "trade":
            return (
                "trade",
                "请根据当前市场、持仓和资金情况生成交易决策。"
                "必要时调用妙想工具获取数据，并在满足条件时执行模拟交易。"
                "最后用自然语言总结本次交易判断、依据和操作结果。",
            )
        task_prompt = str(getattr(settings, "task_prompt", "") or "").strip()
        if task_prompt:
            return ("analysis", task_prompt)
        return (
            "analysis",
            "请先分析当前情况，必要时自行调用妙想工具获取数据，并在需要时执行模拟交易。"
            "最后用自然语言总结本次判断、依据和操作结果。",
        )

    def _run_agent_supports_emit(self, run_agent: Any) -> bool:
        try:
            signature = inspect.signature(run_agent)
        except (TypeError, ValueError):
            return True

        for parameter in signature.parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if parameter.name == "emit":
                return True
        return False

    def _infer_run_type(self, run: StrategyRun) -> str:
        return run_query_service.infer_run_type(run)

    def authenticate_login(self, password: str) -> dict[str, Any]:
        settings = get_settings()
        expected_password = settings.app_login_password

        if not expected_password:
            raise RuntimeError("未配置登录密码，请先设置 APP_LOGIN_PASSWORD。")

        if not secrets.compare_digest(password, expected_password):
            raise RuntimeError("密码错误。")

        token = create_access_token("single-user")
        return {
            "authenticated": True,
            "token": token,
        }

    def get_or_create_settings(self, db: Session) -> AppSettings:
        return settings_service.get_or_create_settings(db)

    def list_schedules(self, db: Session) -> list[StrategySchedule]:
        return schedule_service.list_schedules(db)

    def update_settings(self, db: Session, payload: AppSettingsUpdate) -> AppSettings:
        return settings_service.update_settings(db, payload)

    def replace_schedules(
        self, db: Session, payloads: list[ScheduleUpdate]
    ) -> list[StrategySchedule]:
        return schedule_service.replace_schedules(db, payloads)

    def list_runs(
        self,
        db: Session,
        limit: int = 20,
        run_date: date | None = None,
        status: str | None = None,
        before_id: int | None = None,
    ) -> list[StrategyRun]:
        return run_query_service.list_runs(
            db,
            limit=limit,
            run_date=run_date,
            status=status,
            before_id=before_id,
        )

    def list_runs_page(
        self,
        db: Session,
        limit: int = 20,
        run_date: date | None = None,
        status: str | None = None,
        before_id: int | None = None,
    ) -> dict[str, Any]:
        return run_query_service.list_runs_page(
            db,
            limit=limit,
            run_date=run_date,
            status=status,
            before_id=before_id,
        )

    def get_run(self, db: Session, run_id: int) -> StrategyRun | None:
        return run_query_service.get_run(db, run_id)

    def get_run_raw_tool_preview(
        self, db: Session, run_id: int, preview_index: int
    ) -> dict[str, Any]:
        return run_query_service.get_run_raw_tool_preview(db, run_id, preview_index)

    def get_persistent_session(self, db: Session) -> PersistentSessionRead:
        return automation_session_service.get_persistent_session(db)

    def list_persistent_session_messages(
        self,
        db: Session,
        *,
        limit: int = 50,
        before_id: int | None = None,
    ) -> tuple[PersistentSessionRead, list[ChatMessageRead], int | None, bool]:
        return automation_session_service.list_persistent_session_messages(
            db,
            limit=limit,
            before_id=before_id,
        )

    def delete_persistent_session(self, db: Session) -> None:
        automation_session_service.delete_persistent_session(db)

    def delete_run(self, db: Session, run_id: int, *, force: bool = False) -> None:
        run = db.get(StrategyRun, run_id)
        if run is None:
            raise LookupError("运行记录不存在。")
        if str(run.status or "").strip().lower() in {"running", "pending"}:
            if not force:
                raise RuntimeError("运行中的任务不可删除，请等待任务结束后重试。")
            if self._run_lock.locked():
                raise RuntimeError("当前仍有任务正在执行，暂不能强制删除，请稍后重试。")

        related_session_id = run.chat_session_id
        db.execute(delete(ChatMessageRecord).where(ChatMessageRecord.run_id == run_id))
        db.delete(run)

        if related_session_id is not None:
            session = db.get(ChatSession, related_session_id)
            if session is not None:
                last_message = db.scalar(
                    select(ChatMessageRecord)
                    .where(ChatMessageRecord.session_id == related_session_id)
                    .order_by(ChatMessageRecord.id.desc())
                    .limit(1)
                )
                session.last_message_at = (
                    _assume_utc(last_message.created_at) if last_message is not None else None
                )
                db.add(session)

        db.commit()

    def _hydrate_run_datetimes(
        self, run: StrategyRun, *, include_display_fields: bool
    ) -> None:
        run_query_service.hydrate_run_datetimes(
            run,
            include_display_fields=include_display_fields,
        )

    def _hydrate_run_summary_metrics(self, run: StrategyRun) -> None:
        run_query_service.hydrate_run_summary_metrics(run)

    def _hydrate_run_display_fields(self, run: StrategyRun) -> None:
        run_query_service.hydrate_run_display_fields(run)

    def _format_token_count(self, value: int) -> str:
        if not isinstance(value, int) or value <= 0:
            return "--"
        if value >= 1000:
            return f"{value / 1000:.1f}k"
        return str(value)

    def _get_api_tool_text(self, name: str) -> dict[str, str]:
        mapping = {
            "mx_get_positions": {"name": "获取持仓", "summary": "读取当前账户持仓与仓位分布。"},
            "mx_get_balance": {"name": "获取资产", "summary": "读取账户总资产、现金和收益情况。"},
            "mx_get_orders": {"name": "获取委托", "summary": "读取近期委托和成交记录，用于判断交易状态。"},
            "mx_get_self_selects": {"name": "获取自选", "summary": "读取当前自选股列表，辅助观察候选标的。"},
            "mx_query_market": {"name": "查询行情", "summary": "获取目标股票的实时行情和基础市场数据。"},
            "mx_search_news": {"name": "搜索资讯", "summary": "查询相关新闻或公告，辅助判断市场事件影响。"},
            "mx_screen_stocks": {"name": "筛选股票", "summary": "按条件筛选候选标的，缩小分析范围。"},
            "mx_manage_self_select": {"name": "管理自选", "summary": "增删自选股，维护后续关注列表。"},
            "mx_moni_trade": {"name": "提交模拟交易", "summary": "向模拟交易系统提交买入或卖出指令。"},
            "mx_moni_cancel": {"name": "撤销委托", "summary": "撤销尚未完成的模拟委托单。"},
        }
        return mapping.get(name, {"name": name or "未命名调用", "summary": "执行一次系统或妙想工具调用。"})

    def _build_run_api_details(self, run: StrategyRun) -> list[dict[str, Any]]:
        trade_tool_names = {"mx_moni_trade", "mx_moni_cancel"}
        results: list[dict[str, Any]] = []
        for idx, item in enumerate(self._get_detail_tool_calls(run)):
            tool_name = str(item.get("name") or "")
            if tool_name in trade_tool_names:
                continue
            tool_text = self._get_api_tool_text(tool_name)
            result = item.get("result")
            ok: bool | None = None
            status = "done"
            if isinstance(result, dict) and "ok" in result:
                ok = bool(result.get("ok"))
                status = "done" if ok else "failed"
            results.append(
                {
                    "tool_name": tool_name,
                    "name": tool_text["name"],
                    "summary": tool_text["summary"],
                    "preview_index": idx,
                    "tool_call_id": str(
                        item.get("id") or item.get("tool_call_id") or ""
                    )
                    or None,
                    "status": status,
                    "ok": ok,
                }
            )
        return results

    def _build_raw_tool_previews(self, run: StrategyRun) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        for idx, item in enumerate(self._get_detail_tool_calls(run)):
            preview = self._build_raw_tool_preview_item(item, idx)
            if preview is not None:
                previews.append(preview)
        return previews

    def _build_raw_tool_preview_by_index(
        self, run: StrategyRun, preview_index: int
    ) -> dict[str, Any] | None:
        for idx, item in enumerate(self._get_detail_tool_calls(run)):
            if idx != preview_index:
                continue
            return self._build_raw_tool_preview_item(item, idx, truncate=False)
        return None

    def _build_raw_tool_preview_item(
        self,
        item: dict[str, Any],
        preview_index: int,
        *,
        truncate: bool = True,
    ) -> dict[str, Any] | None:
        tool_name = str(item.get("name") or "")
        tool_text = self._get_api_tool_text(tool_name)
        result = item.get("result")
        if not isinstance(result, dict):
            return None
        raw_payload = result.get("result")
        preview_source = raw_payload if raw_payload is not None else result
        full_preview = self._format_tool_preview(preview_source, truncate=False)
        truncated = len(full_preview) > RAW_TOOL_PREVIEW_MAX_CHARS
        preview = self._format_tool_preview(preview_source) if truncate else full_preview
        return {
            "preview_index": preview_index,
            "tool_name": tool_name,
            "display_name": tool_text["name"],
            "summary": str(result.get("summary") or tool_text["summary"]),
            "preview": preview,
            "truncated": truncated if truncate else False,
            "full_preview": full_preview,
        }

    def _format_tool_preview(
        self,
        payload: Any,
        max_chars: int = RAW_TOOL_PREVIEW_MAX_CHARS,
        *,
        truncate: bool = True,
    ) -> str:
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(payload)
        text = text.strip()
        if not truncate or len(text) <= max_chars:
            return text
        return text[: max_chars - 16].rstrip() + "\n...\n<已截断>"

    def _extract_trade_name(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""

        candidates = [
            payload.get("name"),
            payload.get("stock_name"),
            payload.get("stockName"),
            payload.get("security_name"),
            payload.get("securityName"),
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value

        result = payload.get("result")
        if result is not payload:
            return self._extract_trade_name(result)
        return ""

    def _get_trade_summary(
        self,
        action: str,
        symbol: str,
        volume: int,
    ) -> str:
        action_text = "卖出" if action == "sell" else "买入"
        display_symbol = symbol or "--"
        return f"挂单{action_text}{display_symbol}共计{volume}股。"

    def _resolve_trade_detail_status(self, raw_status: Any) -> tuple[str, bool | None]:
        text = str(raw_status or "").strip().lower()
        if text and any(flag in text for flag in ("fail", "error", "reject")):
            return "failed", False
        return "done", True

    def _build_run_trade_details(self, run: StrategyRun) -> list[dict[str, Any]]:
        tool_calls = self._get_detail_tool_calls(run)
        if run.trade_orders:
            details: list[dict[str, Any]] = []
            for order in run.trade_orders:
                action_name = str(order.action).upper()
                trade_action = "sell" if action_name == "SELL" else "buy"
                tool_name = self._match_trade_tool_name(tool_calls, order.symbol, action_name)
                detail_status, detail_ok = self._resolve_trade_detail_status(order.status)
                details.append(
                    {
                        "action": trade_action,
                        "action_text": "模拟卖出" if action_name == "SELL" else "模拟买入",
                        "symbol": order.symbol,
                        "name": self._extract_trade_name(order.response_payload) or order.symbol,
                        "volume": int(order.quantity),
                        "price": order.price,
                        "amount": round(float(order.price or 0) * int(order.quantity), 2)
                        if order.price is not None
                        else None,
                        "summary": self._get_trade_summary(trade_action, order.symbol, int(order.quantity)),
                        "tool_name": tool_name,
                        "preview_index": self._find_tool_call_index(tool_calls, tool_name, order.symbol),
                        "status": detail_status,
                        "ok": detail_ok,
                    }
                )
            return details

        executed_actions = run.executed_actions if isinstance(run.executed_actions, list) else []
        details: list[dict[str, Any]] = []
        for action in executed_actions:
            if not isinstance(action, dict):
                continue
            action_name = str(action.get("action") or "").upper()
            if action_name not in {"BUY", "SELL"}:
                continue
            trade_action = "sell" if action_name == "SELL" else "buy"
            price = _parse_float(action.get("price"))
            volume = int(action.get("quantity") or 0)
            symbol = str(action.get("symbol") or "--")
            tool_name = self._match_trade_tool_name(tool_calls, symbol, action_name)
            detail_status, detail_ok = self._resolve_trade_detail_status(
                action.get("status")
            )
            details.append(
                {
                    "action": trade_action,
                    "action_text": "模拟卖出" if action_name == "SELL" else "模拟买入",
                    "symbol": symbol,
                    "name": str(action.get("name") or "").strip() or symbol,
                    "volume": volume,
                    "price": price,
                    "amount": round((price or 0) * volume, 2) if price is not None else None,
                    "summary": self._get_trade_summary(trade_action, symbol, volume),
                    "tool_name": tool_name,
                    "preview_index": self._find_tool_call_index(tool_calls, tool_name, symbol),
                    "status": detail_status,
                    "ok": detail_ok,
                }
            )
        return details

    def _match_trade_tool_name(
        self, tool_calls: list[dict[str, Any]], symbol: str, action_name: str
    ) -> str | None:
        desired_name = "mx_moni_trade"
        target_symbol = str(symbol or "").strip()
        target_action = str(action_name or "").upper()
        for item in reversed(tool_calls):
            tool_name = str(item.get("name") or "")
            if tool_name != desired_name:
                continue
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            executed_action = result.get("executed_action")
            if not isinstance(executed_action, dict):
                continue
            if str(executed_action.get("symbol") or "").strip() != target_symbol:
                continue
            if str(executed_action.get("action") or "").upper() != target_action:
                continue
            return tool_name
        return None

    def _find_tool_call_index(
        self,
        tool_calls: list[dict[str, Any]],
        tool_name: str | None,
        symbol: str | None = None,
    ) -> int | None:
        if not tool_name:
            return None
        target_symbol = str(symbol or "").strip()
        for idx in range(len(tool_calls) - 1, -1, -1):
            item = tool_calls[idx]
            if str(item.get("name") or "") != tool_name:
                continue
            if not target_symbol:
                return idx
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            executed_action = result.get("executed_action")
            if isinstance(executed_action, dict) and str(executed_action.get("symbol") or "").strip() == target_symbol:
                return idx
        return None

    def _get_run_token_usage(self, run: StrategyRun) -> dict[str, int | None]:
        response_usage = self._extract_usage(run.llm_response_payload)
        request_usage = self._extract_usage(run.llm_request_payload)

        prompt_tokens = self._coerce_token_value(
            _coalesce(
                response_usage.get("prompt_tokens") if response_usage is not None else None,
                request_usage.get("prompt_tokens") if request_usage is not None else None,
            )
        )
        completion_tokens = self._coerce_token_value(
            _coalesce(
                response_usage.get("completion_tokens")
                if response_usage is not None
                else None,
                request_usage.get("completion_tokens")
                if request_usage is not None
                else None,
            )
        )
        total_tokens = self._coerce_token_value(
            _coalesce(
                response_usage.get("total_tokens") if response_usage is not None else None,
                request_usage.get("total_tokens") if request_usage is not None else None,
            )
        )

        if total_tokens is None and (
            prompt_tokens is not None or completion_tokens is not None
        ):
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

        return {
            "input": prompt_tokens,
            "output": completion_tokens,
            "total": total_tokens,
        }

    def _extract_usage(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        direct_usage = payload.get("usage")
        if isinstance(direct_usage, dict):
            return direct_usage

        responses = payload.get("responses")
        if not isinstance(responses, list):
            return None

        for item in reversed(responses):
            if not isinstance(item, dict):
                continue
            usage = item.get("usage")
            if isinstance(usage, dict):
                return usage
        return None

    def _coerce_token_value(self, value: Any) -> int | None:
        numeric = _parse_float(value)
        if numeric is None or numeric <= 0:
            return None
        return int(numeric)

    def _count_run_api_calls(self, run: StrategyRun) -> int:
        trade_tool_names = {"mx_moni_trade", "mx_moni_cancel"}
        return sum(
            1
            for item in self._get_detail_tool_calls(run)
            if str(item.get("name") or "") not in trade_tool_names
        )

    def _count_executed_actions(self, run: StrategyRun) -> int:
        executed_actions = (
            run.executed_actions if isinstance(run.executed_actions, list) else []
        )
        trade_actions = {"BUY", "SELL"}
        return sum(
            1
            for item in executed_actions
            if isinstance(item, dict)
            and str(item.get("action") or "").upper() in trade_actions
        )

    def _get_detail_tool_calls(self, run: StrategyRun) -> list[dict[str, Any]]:
        skill_payloads = (
            run.skill_payloads if isinstance(run.skill_payloads, dict) else {}
        )
        decision_payload = (
            run.decision_payload if isinstance(run.decision_payload, dict) else {}
        )

        tool_calls = skill_payloads.get("tool_calls")
        if not isinstance(tool_calls, list):
            tool_calls = decision_payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            return []
        return [item for item in tool_calls if isinstance(item, dict)]

    def _empty_account_overview(self, errors: list[str] | None = None) -> dict[str, Any]:
        return account_service.empty_account_overview(errors)

    def _with_account_raw(
        self,
        overview: dict[str, Any],
        *,
        include_raw: bool,
        balance_result: dict[str, Any] | None,
        positions_result: dict[str, Any] | None,
        orders_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return account_service.with_account_raw(
            overview,
            include_raw=include_raw,
            balance_result=balance_result,
            positions_result=positions_result,
            orders_result=orders_result,
        )

    def _build_account_response(
        self,
        *,
        balance_result: dict[str, Any] | None,
        positions_result: dict[str, Any] | None,
        orders_result: dict[str, Any] | None,
        errors: list[str],
        include_raw: bool,
    ) -> dict[str, Any]:
        return account_service.build_account_response(
            balance_result=balance_result,
            positions_result=positions_result,
            orders_result=orders_result,
            errors=errors,
            include_raw=include_raw,
        )

    def _get_cached_account_overview(self) -> dict[str, Any] | None:
        account_service._account_overview_cache = self._account_overview_cache
        account_service._account_overview_cache_expires_at = (
            self._account_overview_cache_expires_at
        )
        cached = account_service.get_cached_account_overview()
        self._sync_account_cache_fields()
        return cached

    def _set_cached_account_overview(self, overview: dict[str, Any]) -> None:
        account_service._account_overview_cache = self._account_overview_cache
        account_service._account_overview_cache_expires_at = (
            self._account_overview_cache_expires_at
        )
        account_service.set_cached_account_overview(overview)
        self._sync_account_cache_fields()

    def _fetch_live_account_payloads(
        self, client: MXClient
    ) -> dict[str, dict[str, Any]]:
        return account_service.fetch_live_account_payloads(client)

    def _extract_tool_result(
        self, tool_calls: list[dict[str, Any]], tool_name: str
    ) -> dict[str, Any] | None:
        return account_service.extract_tool_result(tool_calls, tool_name)

    def get_account_overview(
        self,
        *,
        include_raw: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        account_service._account_overview_cache = self._account_overview_cache
        account_service._account_overview_cache_expires_at = (
            self._account_overview_cache_expires_at
        )
        overview = account_service.get_account_overview(
            settings_loader=self.get_or_create_settings,
            recent_snapshot_loader=self._get_recent_account_snapshot,
            include_raw=include_raw,
            force_refresh=force_refresh,
            client_cls=MXClient,
        )
        self._sync_account_cache_fields()
        return overview

    def chat(self, payload: ChatRequest) -> dict[str, Any]:
        with session_scope() as db:
            settings = self.get_or_create_settings(db)

        if not settings.llm_base_url or not settings.llm_api_key:
            raise RuntimeError("未配置大模型接口，无法执行 AI 聊天。")

        messages = [
            {"role": item.role, "content": item.content} for item in payload.messages
        ]

        content = llm_service.chat(
            model=settings.llm_model,
            base_url=str(settings.llm_base_url),
            api_key=str(settings.llm_api_key),
            system_prompt=settings.system_prompt,
            messages=messages,
            timeout_seconds=1800,
            tool_context=build_skill_context(run_type="chat", app_settings=settings),
        )

        return {
            "message": {
                "role": "assistant",
                "content": content,
            },
            "context": {
                "system_prompt_included": True,
                "tool_access_account_summary": True,
                "tool_access_positions": True,
                "tool_access_orders": True,
                "tool_access_runs": True,
            },
        }

    def chat_stream(self, payload: ChatRequest) -> Iterator[dict[str, Any]]:
        """Yield SSE-style events from the chat agent loop in real time.

        Runs the LLM agent loop on a worker thread and forwards emitted events
        to subscribers via an in-process queue. No StrategyRun is created."""
        with session_scope() as db:
            settings = self.get_or_create_settings(db)

        if not settings.llm_base_url or not settings.llm_api_key:
            raise RuntimeError("未配置大模型接口，无法执行 AI 聊天。")

        messages = [
            {"role": item.role, "content": item.content} for item in payload.messages
        ]
        settings_snapshot = SimpleNamespace(
            mx_api_key=settings.mx_api_key,
            system_prompt=settings.system_prompt,
            llm_model=settings.llm_model,
            llm_base_url=str(settings.llm_base_url),
            llm_api_key=str(settings.llm_api_key),
        )

        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        cancel_event = Event()

        def _emit(event_type: str, **data: Any) -> None:
            event_queue.put(
                {"type": event_type, "ts": time.time(), **data}
            )

        def _worker() -> None:
            try:
                content = llm_service.chat(
                    model=settings_snapshot.llm_model,
                    base_url=settings_snapshot.llm_base_url,
                    api_key=settings_snapshot.llm_api_key,
                    system_prompt=settings_snapshot.system_prompt,
                    messages=messages,
                    timeout_seconds=180,
                    tool_context=build_skill_context(
                        run_type="chat",
                        app_settings=settings_snapshot,
                    ),
                    emit=_emit,
                    cancel_event=cancel_event,
                )
                _emit("completed", message=content)
            except LLMStreamCancelled:
                logger.info("chat_stream worker cancelled")
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat_stream worker failed")
                _emit(
                    "failed",
                    message=str(exc),
                    traceback=traceback.format_exc(limit=4),
                )
            finally:
                event_queue.put(None)

        worker = Thread(target=_worker, daemon=True, name="aniu-chat-stream")
        worker.start()

        terminal_event_seen = False
        try:
            while True:
                try:
                    event = event_queue.get(timeout=15.0)
                except queue.Empty:
                    yield {"type": "heartbeat", "ts": time.time()}
                    continue
                if event is None:
                    return
                if event.get("type") in {"completed", "failed"}:
                    terminal_event_seen = True
                yield event
                if terminal_event_seen:
                    # Drain any trailing events until sentinel so the thread exits cleanly.
                    while True:
                        trailing = event_queue.get()
                        if trailing is None:
                            return
        finally:
            cancel_event.set()
            worker.join(timeout=1.0)

    def _build_orders_overview(
        self, orders_payload: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        return account_service.build_orders_overview(orders_payload)

    def _build_trade_summaries(
        self,
        orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return account_service.build_trade_summaries(orders, positions)

    def _prepare_run(
        self,
        trigger_source: str,
        schedule_id: int | None,
        manual_run_type: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        with session_scope() as db:
            settings = self.get_or_create_settings(db)
            schedule = (
                db.get(StrategySchedule, schedule_id) if schedule_id else None
            )
            if schedule_id is not None and schedule is None:
                raise RuntimeError("指定的定时任务不存在。")
            manual_resolved_run_type, manual_task_prompt = self._resolve_manual_run_profile(
                settings=settings,
                manual_run_type=manual_run_type,
            )
            run = StrategyRun(
                trigger_source=trigger_source,
                run_type=schedule.run_type if schedule else manual_resolved_run_type,
                schedule_id=schedule.id if schedule else None,
                schedule_name=schedule.name if schedule else None,
                status="running",
            )
            db.add(run)
            db.flush()
            run_id = run.id
            settings_snapshot = {
                "id": settings.id,
                "mx_api_key": settings.mx_api_key,
                "llm_base_url": settings.llm_base_url,
                "llm_api_key": settings.llm_api_key,
                "llm_model": settings.llm_model,
                "run_type": schedule.run_type if schedule else manual_resolved_run_type,
                "schedule_id": schedule.id if schedule else None,
                "system_prompt": settings.system_prompt,
                "task_prompt": schedule.task_prompt if schedule else manual_task_prompt,
                "timeout_seconds": int(
                    schedule.timeout_seconds if schedule else 1800
                ),
                "automation_session_id": getattr(
                    settings, "automation_session_id", None
                ),
                "automation_context_window_tokens": getattr(
                    settings,
                    "automation_context_window_tokens",
                    AUTOMATION_DEFAULT_CONTEXT_WINDOW_TOKENS,
                ),
                "automation_recent_message_limit": getattr(
                    settings,
                    "automation_recent_message_limit",
                    AUTOMATION_DEFAULT_RECENT_MESSAGE_LIMIT,
                ),
                "automation_enable_auto_compaction": getattr(
                    settings, "automation_enable_auto_compaction", True
                ),
                "automation_idle_summary_hours": getattr(
                    settings,
                    "automation_idle_summary_hours",
                    AUTOMATION_DEFAULT_IDLE_SUMMARY_HOURS,
                ),
                "automation_context_source": getattr(
                    settings, "automation_context_source", "default"
                ),
            }
        return run_id, settings_snapshot

    def _run_body(
        self,
        *,
        run_id: int,
        settings_snapshot: dict[str, Any],
        trigger_source: str,
        schedule_id: int | None,
        emit: Any = None,
        return_full_run: bool = True,
    ) -> StrategyRun | None:
        session_context: PersistentRunSessionContext | None = None
        automation_phase = "llm"
        _emit = emit if callable(emit) else (lambda *_a, **_kw: None)
        runtime_context, agent_runner = run_service.build_runtime_context(
            run_id=run_id,
            settings_snapshot=settings_snapshot,
            trigger_source=trigger_source,
            schedule_id=schedule_id,
            emit=_emit,
        )

        def _emit_db(db: Session, event_type: str, **data: Any) -> None:
            if getattr(emit, "_persist_run_events", False):
                run_event_publisher.publish(
                    run_id=run_id,
                    event_type=event_type,
                    data=data or None,
                    db=db,
                )
                return
            _emit(event_type, **data)

        try:
            logger.info(
                "execute_run started: run_id=%s, trigger=%s, schedule_id=%s",
                run_id,
                trigger_source,
                schedule_id,
            )
            agent_runner.transition(
                context=runtime_context,
                phase="started",
                message="任务已启动",
                trigger_source=trigger_source,
                schedule_id=schedule_id,
            )
            try:
                (
                    settings,
                    session_context,
                    decision,
                    llm_request,
                    llm_response,
                    runtime_trace,
                ) = run_service.invoke_llm_run(
                    agent_runner=agent_runner,
                    runtime_context=runtime_context,
                    session_scope=session_scope,
                    prepare_persistent_session_context=self._prepare_persistent_session_context,
                    llm_runner=llm_service.run_agent_with_messages,
                    settings_snapshot=settings_snapshot,
                    trigger_source=trigger_source,
                    schedule_id=schedule_id,
                    build_skill_context=build_skill_context,
                    mx_client_cls=MXClient,
                    emit=_emit,
                )
            except RunInvocationError as run_exc:
                session_context = run_exc.session_context
                automation_phase = run_exc.phase
                raise run_exc.original_exception from run_exc

            tool_calls = decision.get("tool_calls")
            skill_payloads = {
                "tool_calls": tool_calls,
                "runtime_trace": runtime_trace,
            }
            agent_runner.transition(
                context=runtime_context,
                phase="propose",
                stage="llm",
                message="正在整理交易提案",
            )
            proposals = self._extract_trade_proposals(tool_calls)
            agent_runner.transition(
                context=runtime_context,
                phase="policy_check",
                stage="policy",
                message="正在执行策略裁决",
                proposal_count=len(proposals),
            )
            policy_decisions = self._evaluate_trade_proposals(
                proposals,
                run_type=str(getattr(settings, "run_type", "analysis") or "analysis"),
                trade_enabled=bool(getattr(settings, "trade_enabled", True)),
                trigger_source=trigger_source,
            )
            approved_count = sum(
                1 for item in policy_decisions if item.decision == "approved"
            )
            revised_count = sum(
                1 for item in policy_decisions if item.decision == "revise"
            )
            rejected_count = sum(
                1 for item in policy_decisions if item.decision == "rejected"
            )
            if approved_count:
                _emit(
                    "policy_approved",
                    message="存在可执行提案",
                    approved_count=approved_count,
                    revised_count=revised_count,
                    rejected_count=rejected_count,
                )
            if revised_count:
                agent_runner.transition(
                    context=runtime_context,
                    phase="replan",
                    stage="policy",
                    message="策略裁决已修正提案",
                    approved_count=approved_count,
                    revised_count=revised_count,
                    rejected_count=rejected_count,
                )
                _emit(
                    "policy_revise_requested",
                    message="策略裁决修正后继续执行",
                    revised_count=revised_count,
                )
            if rejected_count:
                _emit(
                    "policy_rejected",
                    message="存在被拒绝的提案",
                    approved_count=approved_count,
                    revised_count=revised_count,
                    rejected_count=rejected_count,
                )
            executed_intents = intents_from_proposals(policy_decisions)
            executed_actions = intents_to_records(executed_intents)
            persisted_trade_orders = [
                {
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "quantity": intent.quantity,
                    "price": intent.price,
                    "status": intent.status or "submitted",
                }
                for intent in executed_intents
                if str(intent.action or "") in {"BUY", "SELL"}
            ]
            completed_at = now_utc()
            completed_at_shanghai = completed_at.astimezone(SHANGHAI_TZ)

            if persisted_trade_orders:
                agent_runner.transition(
                    context=runtime_context,
                    phase="execute",
                    stage="trade",
                    message=f"正在写入交易执行记录（{len(persisted_trade_orders)} 条）",
                )
            else:
                if rejected_count and not approved_count and not revised_count:
                    agent_runner.transition(
                        context=runtime_context,
                        phase="skip",
                        stage="policy",
                        message="策略裁决拒绝执行，本轮跳过",
                        rejected_count=rejected_count,
                    )
                    _emit(
                        "skip",
                        message="策略裁决拒绝执行，本轮跳过",
                        rejected_count=rejected_count,
                    )
                agent_runner.transition(
                    context=runtime_context,
                    phase="review",
                    stage="review",
                    message="正在整理运行结果",
                )

            run_service.persist_successful_run(
                session_scope=session_scope,
                run_id=run_id,
                session_context=session_context,
                skill_payloads=skill_payloads,
                llm_request=llm_request,
                llm_response=llm_response,
                decision=decision,
                proposals=proposals_to_records(proposals),
                policy_decisions=[item.to_record() for item in policy_decisions],
                executed_actions=executed_actions,
                schedule_id=schedule_id,
                completed_at=completed_at,
                completed_at_shanghai=completed_at_shanghai,
                build_analysis_summary=self._build_analysis_summary,
                parse_price=_parse_float,
                trade_order_cls=TradeOrder,
                strategy_run_cls=StrategyRun,
                strategy_schedule_cls=StrategySchedule,
                chat_session_cls=ChatSession,
                build_assistant_content=self._build_persistent_session_assistant_content,
                persist_assistant_message=self._persist_persistent_session_assistant_message,
                maybe_compact_session=self._maybe_compact_persistent_session,
                persist_system_message=self._persist_persistent_session_system_message,
                compute_next_run_at=self._compute_next_run_at,
                emit_db=_emit_db,
                settings=settings,
                tool_calls=tool_calls,
                return_full_run=return_full_run,
            )

            for action in persisted_trade_orders:
                _emit(
                    "trade_order",
                    symbol=action.get("symbol"),
                    action=action.get("action"),
                    quantity=action.get("quantity"),
                    price=action.get("price"),
                    status=action.get("status") or "submitted",
                )

            if not return_full_run:
                logger.info(
                    "execute_run completed: run_id=%s, actions=%d",
                    run_id,
                    len(executed_actions),
                )
                return None

            with session_scope() as db:
                run = self.get_run(db, run_id)
                if run is None:
                    raise RuntimeError("运行记录不存在。")
                logger.info(
                    "execute_run completed: run_id=%s, actions=%d",
                    run_id,
                    len(executed_actions),
                )
                agent_runner.transition(
                    context=runtime_context,
                    phase="completed",
                    stage="completed",
                    message="任务完成",
                    actions=len(executed_actions),
                )
                _emit("completed", message="任务完成", actions=len(executed_actions))
                return run
        except Exception as exc:
            logger.error(
                "execute_run failed: run_id=%s, error=%s",
                run_id,
                exc,
            )
            run_service.persist_failed_run(
                session_scope=session_scope,
                run_id=run_id,
                session_context=session_context,
                schedule_id=schedule_id,
                trigger_source=trigger_source,
                settings_snapshot=settings_snapshot,
                automation_phase=automation_phase,
                error=exc,
                now_utc=now_utc,
                now_shanghai=now_shanghai,
                compute_next_run_at=self._compute_next_run_at,
                schedule_max_retries=SCHEDULE_MAX_RETRIES,
                schedule_retry_delay=SCHEDULE_RETRY_DELAY,
                strategy_run_cls=StrategyRun,
                strategy_schedule_cls=StrategySchedule,
                chat_session_cls=ChatSession,
                build_assistant_content=self._build_persistent_session_assistant_content,
                persist_assistant_message=self._persist_persistent_session_assistant_message,
                emit_db=_emit_db,
            )
            raise

    def execute_run(
        self,
        trigger_source: str = "manual",
        schedule_id: int | None = None,
        manual_run_type: str | None = None,
    ) -> StrategyRun:
        return run_service.execute_run(
            trigger_source=trigger_source,
            schedule_id=schedule_id,
            manual_run_type=manual_run_type,
        )

    def start_run_async(
        self,
        trigger_source: str = "manual",
        schedule_id: int | None = None,
        manual_run_type: str | None = None,
    ) -> int:
        return run_service.start_run_async(
            trigger_source=trigger_source,
            schedule_id=schedule_id,
            manual_run_type=manual_run_type,
        )

    def process_due_schedule(self) -> None:
        run_service.process_due_schedule()

    def _safe_call(self, func: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "result": func()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _get_recent_account_snapshot(
        self, db: Session
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
    ]:
        return account_service.get_recent_account_snapshot(
            db,
            tool_call_loader=self._get_run_tool_calls,
        )

    def _get_run_tool_calls(self, run: StrategyRun) -> list[dict[str, Any]]:
        skill_payloads = (
            run.skill_payloads if isinstance(run.skill_payloads, dict) else {}
        )
        decision_payload = (
            run.decision_payload if isinstance(run.decision_payload, dict) else {}
        )

        combined_tool_calls: list[dict[str, Any]] = []
        prefetched_tool_calls = skill_payloads.get("prefetched_tool_calls")
        if isinstance(prefetched_tool_calls, list):
            combined_tool_calls.extend(
                item for item in prefetched_tool_calls if isinstance(item, dict)
            )

        tool_calls = self._get_detail_tool_calls(run)
        if tool_calls:
            combined_tool_calls.extend(
                tool_calls
            )
        return combined_tool_calls

    def _extract_trade_proposals(self, tool_calls: Any) -> list[TradeProposal]:
        if not isinstance(tool_calls, list):
            return []

        proposals: list[TradeProposal] = []
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            result = item.get("result")
            if not isinstance(result, dict) or not result.get("ok"):
                continue
            executed_action = result.get("executed_action")
            if not isinstance(executed_action, dict):
                continue
            action_name = str(executed_action.get("action") or "").upper()
            entry = TradeProposal(
                symbol=str(
                    executed_action.get("symbol")
                    or executed_action.get("stock_code")
                    or ""
                ).strip(),
                name=str(executed_action.get("name") or "").strip() or None,
                action=action_name,  # type: ignore[arg-type]
                quantity=int(executed_action.get("quantity") or 0),
                price_type=str(executed_action.get("price_type") or "MARKET"),
                price=_parse_float(executed_action.get("price")),
                reason=str(executed_action.get("reason") or "").strip(),
                response=result.get("result") if isinstance(result.get("result"), dict) else None,
            )
            if action_name == "CANCEL":
                entry.price_type = "CANCEL"
            if action_name == "MANAGE_SELF_SELECT":
                entry.price_type = "SELF_SELECT"
                entry.symbol = str(executed_action.get("query") or "")
            proposals.append(entry)
        return proposals

    def _evaluate_trade_proposals(
        self,
        proposals: list[TradeProposal],
        *,
        run_type: str,
        trade_enabled: bool,
        trigger_source: str,
    ) -> list[PolicyDecision]:
        enforce_trade_run_type = not (
            trigger_source == "manual" and run_type == "analysis"
        )
        return [
            risk_gate.evaluate(
                proposal=proposal,
                run_type=run_type,
                trade_enabled=trade_enabled,
                enforce_trade_run_type=enforce_trade_run_type,
            )
            for proposal in proposals
        ]

    def _extract_executed_actions(self, tool_calls: Any) -> list[TradeExecutionIntent]:
        return intents_from_proposals(
            self._evaluate_trade_proposals(self._extract_trade_proposals(tool_calls))
        )

    def _build_analysis_summary(self, final_answer: Any) -> str | None:
        text = str(final_answer or "").strip()
        if not text:
            return None
        compact = " ".join(text.split())
        if len(compact) <= 120:
            return compact
        return compact[:117] + "..."

    def _prepare_persistent_session_context(
        self,
        *,
        db: Session,
        run_id: int,
        settings: Any,
        trigger_source: str,
        schedule_id: int | None,
    ) -> PersistentRunSessionContext:
        return automation_session_service.prepare_persistent_session_context(
            db=db,
            run_id=run_id,
            settings=settings,
            trigger_source=trigger_source,
            schedule_id=schedule_id,
        )

    def _get_or_create_persistent_session(self, db: Session) -> ChatSession:
        return automation_session_service.get_or_create_persistent_session(db)

    def _build_persistent_session_user_content(
        self,
        *,
        settings: Any,
        trigger_source: str,
        schedule_id: int | None,
        schedule_name: str | None,
        run_type: str,
        task_prompt: str,
        prefetched_context: str | None,
    ) -> str:
        return automation_session_service.build_persistent_session_user_content(
            settings=settings,
            trigger_source=trigger_source,
            schedule_id=schedule_id,
            schedule_name=schedule_name,
            run_type=run_type,
            task_prompt=task_prompt,
            prefetched_context=prefetched_context,
        )

    def _build_persistent_session_assistant_content(
        self,
        *,
        run_id: int,
        run_type: str,
        status: str,
        final_answer: str | None,
        tool_calls: list[dict[str, Any]] | None,
        executed_actions: list[dict[str, Any]] | None,
        error_message: str | None = None,
        phase: str | None = None,
    ) -> str:
        return automation_session_service.build_persistent_session_assistant_content(
            run_id=run_id,
            run_type=run_type,
            status=status,
            final_answer=final_answer,
            tool_calls=tool_calls,
            executed_actions=executed_actions,
            error_message=error_message,
            phase=phase,
        )

    def _persist_persistent_session_user_message(
        self,
        *,
        db: Session,
        session: ChatSession,
        run_id: int,
        content: str,
        schedule_id: int | None,
        schedule_name: str | None,
        run_type: str,
        trigger_source: str,
    ) -> ChatMessageRecord:
        return automation_session_service.persist_persistent_session_user_message(
            db=db,
            session=session,
            run_id=run_id,
            content=content,
            schedule_id=schedule_id,
            schedule_name=schedule_name,
            run_type=run_type,
            trigger_source=trigger_source,
        )

    def _slim_automation_tool_calls(
        self, tool_calls: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        return automation_session_service.slim_automation_tool_calls(tool_calls)

    def _persist_persistent_session_assistant_message(
        self,
        *,
        db: Session,
        session: ChatSession,
        run_id: int,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
        status: str,
        meta_payload: dict[str, Any] | None,
    ) -> ChatMessageRecord:
        return automation_session_service.persist_persistent_session_assistant_message(
            db=db,
            session=session,
            run_id=run_id,
            content=content,
            tool_calls=tool_calls,
            status=status,
            meta_payload=meta_payload,
        )

    def _persist_persistent_session_system_message(
        self,
        *,
        db: Session,
        session: ChatSession,
        run_id: int,
        content: str,
        meta_payload: dict[str, Any] | None,
    ) -> ChatMessageRecord:
        return automation_session_service.persist_persistent_session_system_message(
            db=db,
            session=session,
            run_id=run_id,
            content=content,
            meta_payload=meta_payload,
        )

    def _list_persistent_session_history_records(
        self,
        *,
        db: Session,
        session_id: int,
        recent_limit: int,
    ) -> list[ChatMessageRecord]:
        return automation_session_service.list_persistent_session_history_records(
            db=db,
            session_id=session_id,
            recent_limit=recent_limit,
        )

    def _build_persistent_session_history_messages(
        self, records: list[ChatMessageRecord]
    ) -> list[dict[str, Any]]:
        return automation_session_service.build_persistent_session_history_messages(records)

    def _retrieve_persistent_session_memory_messages(
        self,
        *,
        session: ChatSession,
        settings: Any,
        run_type: str,
        task_prompt: str,
    ) -> list[dict[str, Any]]:
        return automation_session_service.retrieve_persistent_session_memory_messages(
            session=session,
            settings=settings,
            run_type=run_type,
            task_prompt=task_prompt,
        )

    def _estimate_persistent_session_context_tokens(
        self,
        *,
        session: ChatSession,
        settings: Any,
        messages: list[dict[str, Any]],
    ) -> int:
        return automation_session_service.estimate_persistent_session_context_tokens(
            session=session,
            settings=settings,
            messages=messages,
        )

    def _list_uncompacted_persistent_session_records(
        self,
        *,
        db: Session,
        session: ChatSession,
    ) -> list[ChatMessageRecord]:
        return automation_session_service.list_uncompacted_persistent_session_records(
            db=db,
            session=session,
        )

    def _build_persistent_session_context_system_message(
        self,
        *,
        session: ChatSession,
    ) -> dict[str, Any] | None:
        return automation_session_service.build_persistent_session_context_system_message(
            session=session,
        )

    def _build_persistent_session_prompt_messages(
        self,
        *,
        session: ChatSession,
        history_messages: list[dict[str, Any]],
        memory_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return automation_session_service.build_persistent_session_prompt_messages(
            session=session,
            history_messages=history_messages,
            memory_messages=memory_messages,
        )

    def _build_compacted_summary_text(
        self, records: list[ChatMessageRecord]
    ) -> str | None:
        return automation_session_service.build_compacted_summary_text(records)

    def _safe_prompt_budget(self, settings: Any) -> int:
        return automation_session_service.safe_prompt_budget(settings)

    def _should_compact_automation_session(
        self,
        *,
        session: ChatSession,
        records: list[ChatMessageRecord],
        settings: Any,
        estimated_tokens: int,
    ) -> bool:
        return automation_session_service.should_compact_automation_session(
            session=session,
            records=records,
            settings=settings,
            estimated_tokens=estimated_tokens,
        )

    def _maybe_compact_persistent_session(
        self,
        *,
        db: Session,
        session: ChatSession,
        settings: Any,
        estimated_tokens: int,
    ) -> tuple[str | None, int | None]:
        return automation_session_service.maybe_compact_persistent_session(
            db=db,
            session=session,
            settings=settings,
            estimated_tokens=estimated_tokens,
        )

    def _compute_next_run_at(
        self,
        cron_expression: str | None,
        from_time: datetime | None = None,
    ) -> datetime | None:
        effective_from_time = from_time if from_time is not None else now_shanghai()
        return compute_next_run_at(cron_expression, from_time=effective_from_time)

    def _parse_cron_values(
        self,
        expression: str,
        minimum: int,
        maximum: int,
        *,
        allow_seven_as_zero: bool = False,
    ) -> set[int]:
        expr = expression.strip()
        allowed: set[int] = set()
        for part in expr.split(","):
            part = part.strip()
            if not part:
                raise ValueError("invalid cron expression")

            range_part = part
            step = 1
            if "/" in part:
                range_part, step_text = part.split("/", 1)
                step = int(step_text)
                if step <= 0:
                    raise ValueError("invalid cron step")

            if range_part == "*":
                start = minimum
                end = maximum
            elif "-" in range_part:
                start_text, end_text = range_part.split("-", 1)
                start = self._normalize_cron_value(
                    int(start_text),
                    minimum=minimum,
                    maximum=maximum,
                    allow_seven_as_zero=allow_seven_as_zero,
                )
                end = self._normalize_cron_value(
                    int(end_text),
                    minimum=minimum,
                    maximum=maximum,
                    allow_seven_as_zero=allow_seven_as_zero,
                )
                if start > end:
                    raise ValueError("invalid cron range")
            else:
                numeric = self._normalize_cron_value(
                    int(range_part),
                    minimum=minimum,
                    maximum=maximum,
                    allow_seven_as_zero=allow_seven_as_zero,
                )
                start = numeric
                end = numeric

            allowed.update(range(start, end + 1, step))

        return allowed

    def _normalize_cron_value(
        self,
        value: int,
        *,
        minimum: int,
        maximum: int,
        allow_seven_as_zero: bool = False,
    ) -> int:
        if allow_seven_as_zero and value == 7:
            value = 0
        if value < minimum or value > maximum:
            raise ValueError("cron value out of range")
        return value

    def _matches_cron_day(
        self,
        current: datetime,
        *,
        day_of_month_values: set[int],
        day_of_week_values: set[int],
        day_of_month_expr: str,
        day_of_week_expr: str,
    ) -> bool:
        day_of_month_matches = current.day in day_of_month_values
        current_day_of_week = (current.weekday() + 1) % 7
        day_of_week_matches = current_day_of_week in day_of_week_values

        day_of_month_is_wildcard = day_of_month_expr.strip() == "*"
        day_of_week_is_wildcard = day_of_week_expr.strip() == "*"

        if day_of_month_is_wildcard and day_of_week_is_wildcard:
            return True
        if day_of_month_is_wildcard:
            return day_of_week_matches
        if day_of_week_is_wildcard:
            return day_of_month_matches
        return day_of_month_matches or day_of_week_matches

    def _build_account_overview(
        self,
        balance_payload: dict[str, Any] | None,
        positions_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        original_now_shanghai = account_service_module.now_shanghai
        account_service_module.now_shanghai = now_shanghai
        try:
            return account_service.build_account_overview(balance_payload, positions_payload)
        finally:
            account_service_module.now_shanghai = original_now_shanghai

    def _format_profit_text(self, profit_ratio: float | None) -> str:
        return account_service.format_profit_text(profit_ratio)


aniu_service = AniuService()
