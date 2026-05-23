from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import StrategyRun
from app.domain.schedule.policy import ANALYSIS_TASK_NAMES, assume_utc
from app.domain.trading.intents import intents_from_records

RAW_TOOL_PREVIEW_MAX_CHARS = 6000


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RunQueryService:
    def infer_run_type(self, run: StrategyRun) -> str:
        schedule_name = str(run.schedule_name or "").strip()
        if schedule_name in ANALYSIS_TASK_NAMES:
            return "analysis"
        if schedule_name.startswith("上午运行") or schedule_name.startswith("下午运行"):
            return "trade"

        if run.trade_orders:
            return "trade"

        executed_actions = intents_from_records(run.executed_actions)
        trade_actions = {"BUY", "SELL", "CANCEL"}
        if any(
            str(item.action or "").upper() in trade_actions
            for item in executed_actions
        ):
            return "trade"

        tool_calls = self.get_run_tool_calls(run)
        trade_tool_names = {"mx_moni_trade", "mx_moni_cancel"}
        if any(str(item.get("name") or "") in trade_tool_names for item in tool_calls):
            return "trade"

        stored_run_type = str(run.run_type or "").strip()
        if stored_run_type in {"trade", "analysis"}:
            return stored_run_type

        return "analysis"

    def list_runs(
        self,
        db: Session,
        *,
        limit: int = 20,
        run_date: date | None = None,
        status: str | None = None,
        before_id: int | None = None,
    ) -> list[StrategyRun]:
        stmt = select(StrategyRun)

        if run_date is not None:
            start_of_day = datetime.combine(run_date, datetime.min.time())
            end_of_day = start_of_day + timedelta(days=1)
            stmt = stmt.where(
                StrategyRun.started_at >= start_of_day,
                StrategyRun.started_at < end_of_day,
            )

        normalized_status = str(status or "").strip().lower()
        if normalized_status:
            stmt = stmt.where(StrategyRun.status == normalized_status)

        if before_id is not None:
            stmt = stmt.where(StrategyRun.id < before_id)

        stmt = stmt.order_by(StrategyRun.started_at.desc(), StrategyRun.id.desc()).limit(limit)
        runs = list(db.scalars(stmt).all())
        for run in runs:
            self.hydrate_run_datetimes(run, include_display_fields=False)
        return runs

    def list_runs_page(
        self,
        db: Session,
        *,
        limit: int = 20,
        run_date: date | None = None,
        status: str | None = None,
        before_id: int | None = None,
    ) -> dict[str, Any]:
        page_size = max(1, limit)
        runs = self.list_runs(
            db,
            limit=page_size + 1,
            run_date=run_date,
            status=status,
            before_id=before_id,
        )
        has_more = len(runs) > page_size
        items = runs[:page_size]
        next_before_id = items[-1].id if has_more and items else None
        return {
            "items": items,
            "next_before_id": next_before_id,
            "has_more": has_more,
        }

    def get_run(self, db: Session, run_id: int) -> StrategyRun | None:
        stmt = (
            select(StrategyRun)
            .where(StrategyRun.id == run_id)
            .options(selectinload(StrategyRun.trade_orders))
        )
        run = db.scalar(stmt)
        if run is not None:
            self.hydrate_run_datetimes(run, include_display_fields=True)
        return run

    def get_run_raw_tool_preview(
        self,
        db: Session,
        run_id: int,
        preview_index: int,
    ) -> dict[str, Any]:
        run = self.get_run(db, run_id)
        if run is None:
            raise LookupError("运行记录不存在。")

        preview = self.build_raw_tool_preview_by_index(run, preview_index)
        if preview is None:
            raise LookupError("原始工具预览不存在。")
        return preview

    def hydrate_run_datetimes(
        self,
        run: StrategyRun,
        *,
        include_display_fields: bool,
    ) -> None:
        run.started_at = assume_utc(run.started_at)
        run.finished_at = assume_utc(run.finished_at)
        run.run_type = self.infer_run_type(run)
        self.hydrate_run_summary_metrics(run)
        if include_display_fields:
            self.hydrate_run_display_fields(run)
            for order in run.trade_orders:
                order.created_at = assume_utc(order.created_at)

    def hydrate_run_summary_metrics(self, run: StrategyRun) -> None:
        token_usage = self.get_run_token_usage(run)
        run.api_call_count = self.count_run_api_calls(run)
        run.executed_trade_count = self.count_executed_actions(run)
        run.input_tokens = token_usage["input"]
        run.output_tokens = token_usage["output"]
        run.total_tokens = token_usage["total"]

    def hydrate_run_display_fields(self, run: StrategyRun) -> None:
        run.output_markdown = (
            str(run.final_answer or run.analysis_summary or run.error_message or "").strip()
            or None
        )
        run.api_details = self.build_run_api_details(run)
        run.raw_tool_previews = self.build_raw_tool_previews(run)
        run.trade_details = self.build_run_trade_details(run)

    def get_api_tool_text(self, name: str) -> dict[str, str]:
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

    def build_run_api_details(self, run: StrategyRun) -> list[dict[str, Any]]:
        trade_tool_names = {"mx_moni_trade", "mx_moni_cancel"}
        results: list[dict[str, Any]] = []
        for idx, item in enumerate(self.get_detail_tool_calls(run)):
            tool_name = str(item.get("name") or "")
            if tool_name in trade_tool_names:
                continue
            tool_text = self.get_api_tool_text(tool_name)
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
                    "tool_call_id": str(item.get("id") or item.get("tool_call_id") or "") or None,
                    "status": status,
                    "ok": ok,
                }
            )
        return results

    def build_raw_tool_previews(self, run: StrategyRun) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        for idx, item in enumerate(self.get_detail_tool_calls(run)):
            preview = self.build_raw_tool_preview_item(item, idx)
            if preview is not None:
                previews.append(preview)
        return previews

    def build_raw_tool_preview_by_index(
        self,
        run: StrategyRun,
        preview_index: int,
    ) -> dict[str, Any] | None:
        for idx, item in enumerate(self.get_detail_tool_calls(run)):
            if idx != preview_index:
                continue
            return self.build_raw_tool_preview_item(item, idx, truncate=False)
        return None

    def build_raw_tool_preview_item(
        self,
        item: dict[str, Any],
        preview_index: int,
        *,
        truncate: bool = True,
    ) -> dict[str, Any] | None:
        tool_name = str(item.get("name") or "")
        tool_text = self.get_api_tool_text(tool_name)
        result = item.get("result")
        if not isinstance(result, dict):
            return None
        raw_payload = result.get("result")
        preview_source = raw_payload if raw_payload is not None else result
        full_preview = self.format_tool_preview(preview_source, truncate=False)
        truncated = len(full_preview) > RAW_TOOL_PREVIEW_MAX_CHARS
        preview = self.format_tool_preview(preview_source) if truncate else full_preview
        return {
            "preview_index": preview_index,
            "tool_name": tool_name,
            "display_name": tool_text["name"],
            "summary": str(result.get("summary") or tool_text["summary"]),
            "preview": preview,
            "truncated": truncated if truncate else False,
            "full_preview": full_preview,
        }

    def format_tool_preview(
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

    def extract_trade_name(self, payload: Any) -> str:
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
            return self.extract_trade_name(result)
        return ""

    def get_trade_summary(self, action: str, symbol: str, volume: int) -> str:
        action_text = "卖出" if action == "sell" else "买入"
        display_symbol = symbol or "--"
        return f"挂单{action_text}{display_symbol}共计{volume}股。"

    def resolve_trade_detail_status(self, raw_status: Any) -> tuple[str, bool | None]:
        text = str(raw_status or "").strip().lower()
        if text and any(flag in text for flag in ("fail", "error", "reject")):
            return "failed", False
        return "done", True

    def build_run_trade_details(self, run: StrategyRun) -> list[dict[str, Any]]:
        tool_calls = self.get_detail_tool_calls(run)
        if run.trade_orders:
            details: list[dict[str, Any]] = []
            for order in run.trade_orders:
                action_name = str(order.action).upper()
                trade_action = "sell" if action_name == "SELL" else "buy"
                tool_name = self.match_trade_tool_name(tool_calls, order.symbol, action_name)
                detail_status, detail_ok = self.resolve_trade_detail_status(order.status)
                details.append(
                    {
                        "action": trade_action,
                        "action_text": "模拟卖出" if action_name == "SELL" else "模拟买入",
                        "symbol": order.symbol,
                        "name": self.extract_trade_name(order.response_payload) or order.symbol,
                        "volume": int(order.quantity),
                        "price": order.price,
                        "amount": round(float(order.price or 0) * int(order.quantity), 2)
                        if order.price is not None
                        else None,
                        "summary": self.get_trade_summary(trade_action, order.symbol, int(order.quantity)),
                        "tool_name": tool_name,
                        "preview_index": self.find_tool_call_index(tool_calls, tool_name, order.symbol),
                        "status": detail_status,
                        "ok": detail_ok,
                    }
                )
            return details

        executed_actions = intents_from_records(run.executed_actions)
        details: list[dict[str, Any]] = []
        for action in executed_actions:
            action_name = str(action.action or "").upper()
            if action_name not in {"BUY", "SELL"}:
                continue
            trade_action = "sell" if action_name == "SELL" else "buy"
            price = _parse_float(action.price)
            volume = int(action.quantity or 0)
            symbol = str(action.symbol or "--")
            tool_name = self.match_trade_tool_name(tool_calls, symbol, action_name)
            detail_status, detail_ok = self.resolve_trade_detail_status(action.status)
            details.append(
                {
                    "action": trade_action,
                    "action_text": "模拟卖出" if action_name == "SELL" else "模拟买入",
                    "symbol": symbol,
                    "name": str(action.name or "").strip() or symbol,
                    "volume": volume,
                    "price": price,
                    "amount": round((price or 0) * volume, 2) if price is not None else None,
                    "summary": self.get_trade_summary(trade_action, symbol, volume),
                    "tool_name": tool_name,
                    "preview_index": self.find_tool_call_index(tool_calls, tool_name, symbol),
                    "status": detail_status,
                    "ok": detail_ok,
                }
            )
        return details

    def match_trade_tool_name(
        self,
        tool_calls: list[dict[str, Any]],
        symbol: str,
        action_name: str,
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

    def find_tool_call_index(
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

    def get_run_token_usage(self, run: StrategyRun) -> dict[str, int | None]:
        response_usage = self.extract_usage(run.llm_response_payload)
        request_usage = self.extract_usage(run.llm_request_payload)

        prompt_tokens = self.coerce_token_value(
            response_usage.get("prompt_tokens") if response_usage is not None else None
            or request_usage.get("prompt_tokens") if request_usage is not None else None
        )
        completion_tokens = self.coerce_token_value(
            response_usage.get("completion_tokens") if response_usage is not None else None
            or request_usage.get("completion_tokens") if request_usage is not None else None
        )
        total_tokens = self.coerce_token_value(
            response_usage.get("total_tokens") if response_usage is not None else None
            or request_usage.get("total_tokens") if request_usage is not None else None
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

    def extract_usage(self, payload: Any) -> dict[str, Any] | None:
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

    def coerce_token_value(self, value: Any) -> int | None:
        numeric = _parse_float(value)
        if numeric is None or numeric <= 0:
            return None
        return int(numeric)

    def count_run_api_calls(self, run: StrategyRun) -> int:
        trade_tool_names = {"mx_moni_trade", "mx_moni_cancel"}
        return sum(
            1
            for item in self.get_detail_tool_calls(run)
            if str(item.get("name") or "") not in trade_tool_names
        )

    def count_executed_actions(self, run: StrategyRun) -> int:
        executed_actions = intents_from_records(run.executed_actions)
        trade_actions = {"BUY", "SELL"}
        return sum(
            1
            for item in executed_actions
            if str(item.action or "").upper() in trade_actions
        )

    def get_detail_tool_calls(self, run: StrategyRun) -> list[dict[str, Any]]:
        skill_payloads = run.skill_payloads if isinstance(run.skill_payloads, dict) else {}
        decision_payload = run.decision_payload if isinstance(run.decision_payload, dict) else {}

        tool_calls = skill_payloads.get("tool_calls")
        if not isinstance(tool_calls, list):
            tool_calls = decision_payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            return []
        return [item for item in tool_calls if isinstance(item, dict)]

    def get_run_tool_calls(self, run: StrategyRun) -> list[dict[str, Any]]:
        skill_payloads = run.skill_payloads if isinstance(run.skill_payloads, dict) else {}

        combined_tool_calls: list[dict[str, Any]] = []
        prefetched_tool_calls = skill_payloads.get("prefetched_tool_calls")
        if isinstance(prefetched_tool_calls, list):
            combined_tool_calls.extend(
                item for item in prefetched_tool_calls if isinstance(item, dict)
            )

        tool_calls = self.get_detail_tool_calls(run)
        if tool_calls:
            combined_tool_calls.extend(tool_calls)
        return combined_tool_calls


run_query_service = RunQueryService()
