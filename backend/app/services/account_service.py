from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.database import session_scope
from app.db.models import StrategyRun
from app.domain.schedule.policy import now_utc, now_shanghai
from app.services.trading_calendar_service import trading_calendar_service
from skills.mx_core.client import MXClient

ACCOUNT_OVERVIEW_CACHE_MAX_WORKERS = 3


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
    mapping = {0: "SZ", 1: "SH"}
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
        return datetime.fromtimestamp(numeric, tz=now_shanghai().tzinfo).strftime(
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


class AccountService:
    def __init__(self) -> None:
        self._account_cache_lock = Lock()
        self._account_overview_cache: dict[str, Any] | None = None
        self._account_overview_cache_expires_at = None

    def empty_account_overview(self, errors: list[str] | None = None) -> dict[str, Any]:
        return {
            "open_date": None,
            "operating_days": None,
            "initial_capital": None,
            "total_assets": None,
            "total_market_value": None,
            "cash_balance": None,
            "total_position_ratio": None,
            "holding_profit": None,
            "total_return_ratio": None,
            "nav": None,
            "daily_profit": None,
            "daily_return_ratio": None,
            "positions": [],
            "orders": [],
            "trade_summaries": [],
            "errors": errors or [],
        }

    def with_account_raw(
        self,
        overview: dict[str, Any],
        *,
        include_raw: bool,
        balance_result: dict[str, Any] | None,
        positions_result: dict[str, Any] | None,
        orders_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if include_raw:
            overview["raw_balance"] = balance_result
            overview["raw_positions"] = positions_result
            overview["raw_orders"] = orders_result
        return overview

    def build_account_response(
        self,
        *,
        balance_result: dict[str, Any] | None,
        positions_result: dict[str, Any] | None,
        orders_result: dict[str, Any] | None,
        errors: list[str],
        include_raw: bool,
    ) -> dict[str, Any]:
        if (
            balance_result is None
            and positions_result is None
            and orders_result is None
        ):
            return self.with_account_raw(
                self.empty_account_overview(errors),
                include_raw=include_raw,
                balance_result=balance_result,
                positions_result=positions_result,
                orders_result=orders_result,
            )

        overview = self.build_account_overview(balance_result, positions_result)
        normalized_orders = self.build_orders_overview(orders_result)
        overview["orders"] = normalized_orders
        overview["trade_summaries"] = self.build_trade_summaries(
            normalized_orders,
            overview.get("positions") or [],
        )
        overview["errors"] = errors
        return self.with_account_raw(
            overview,
            include_raw=include_raw,
            balance_result=balance_result,
            positions_result=positions_result,
            orders_result=orders_result,
        )

    def get_cached_account_overview(self) -> dict[str, Any] | None:
        with self._account_cache_lock:
            if (
                self._account_overview_cache is None
                or self._account_overview_cache_expires_at is None
                or self._account_overview_cache_expires_at <= now_utc()
            ):
                self._account_overview_cache = None
                self._account_overview_cache_expires_at = None
                return None
            return dict(self._account_overview_cache)

    def set_cached_account_overview(self, overview: dict[str, Any]) -> None:
        ttl_seconds = max(0, int(get_settings().account_overview_cache_ttl_seconds))
        if ttl_seconds <= 0:
            with self._account_cache_lock:
                self._account_overview_cache = None
                self._account_overview_cache_expires_at = None
            return

        cached_overview = dict(overview)
        cached_overview.pop("raw_balance", None)
        cached_overview.pop("raw_positions", None)
        cached_overview.pop("raw_orders", None)

        with self._account_cache_lock:
            self._account_overview_cache = cached_overview
            self._account_overview_cache_expires_at = now_utc() + timedelta(seconds=ttl_seconds)

    def safe_call(self, func: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "result": func()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def fetch_live_account_payloads(self, client: MXClient) -> dict[str, dict[str, Any]]:
        with ThreadPoolExecutor(max_workers=ACCOUNT_OVERVIEW_CACHE_MAX_WORKERS) as executor:
            futures = {
                "balance": executor.submit(self.safe_call, client.get_balance),
                "positions": executor.submit(self.safe_call, client.get_positions),
                "orders": executor.submit(self.safe_call, client.get_orders),
            }
            return {name: future.result() for name, future in futures.items()}

    def extract_tool_result(
        self,
        tool_calls: list[dict[str, Any]],
        tool_name: str,
    ) -> dict[str, Any] | None:
        for item in reversed(tool_calls):
            if item.get("name") != tool_name:
                continue
            result = item.get("result")
            if not isinstance(result, dict) or not result.get("ok"):
                continue
            payload = result.get("result")
            if isinstance(payload, dict):
                return payload
        return None

    def get_recent_account_snapshot(
        self,
        db: Session,
        *,
        tool_call_loader: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        stmt = select(StrategyRun).order_by(StrategyRun.started_at.desc()).limit(20)

        balance_result: dict[str, Any] | None = None
        positions_result: dict[str, Any] | None = None
        orders_result: dict[str, Any] | None = None

        for run in db.scalars(stmt).all():
            tool_calls = tool_call_loader(run)
            if not tool_calls:
                continue

            if balance_result is None:
                balance_result = self.extract_tool_result(tool_calls, "mx_get_balance")
            if positions_result is None:
                positions_result = self.extract_tool_result(tool_calls, "mx_get_positions")
            if orders_result is None:
                orders_result = self.extract_tool_result(tool_calls, "mx_get_orders")

            if (
                balance_result is not None
                and positions_result is not None
                and orders_result is not None
            ):
                break

        return balance_result, positions_result, orders_result

    def get_account_overview(
        self,
        *,
        settings_loader: Any,
        recent_snapshot_loader: Any,
        include_raw: bool = False,
        force_refresh: bool = False,
        client_cls: type[MXClient] = MXClient,
    ) -> dict[str, Any]:
        if not force_refresh and not include_raw:
            cached_overview = self.get_cached_account_overview()
            if cached_overview is not None:
                return cached_overview

        with session_scope() as db:
            settings = settings_loader(db)
            cached_balance_result, cached_positions_result, cached_orders_result = recent_snapshot_loader(db)

        errors: list[str] = []
        balance_result = cached_balance_result
        positions_result = cached_positions_result
        orders_result = cached_orders_result
        client: MXClient | None = None

        if getattr(settings, "mx_api_key", None):
            try:
                client = client_cls(
                    api_key=getattr(settings, "mx_api_key", None),
                    base_url=getattr(settings, "mx_api_url", None),
                )
            except Exception as exc:
                if (
                    balance_result is None
                    and positions_result is None
                    and orders_result is None
                ):
                    return self.build_account_response(
                        balance_result=None,
                        positions_result=None,
                        orders_result=None,
                        errors=[str(exc)],
                        include_raw=include_raw,
                    )

                errors.append(f"{str(exc)}，当前展示最近一次任务缓存的账户数据。")
                return self.build_account_response(
                    balance_result=balance_result,
                    positions_result=positions_result,
                    orders_result=orders_result,
                    errors=errors,
                    include_raw=include_raw,
                )

        try:
            if client is not None:
                live_payloads = self.fetch_live_account_payloads(client)

                balance_payload = live_payloads["balance"]
                if not balance_payload.get("ok"):
                    if cached_balance_result is not None:
                        balance_result = cached_balance_result
                        errors.append(
                            f"{str(balance_payload.get('error') or '资金接口失败')}，当前展示最近一次任务缓存的账户资金。"
                        )
                    else:
                        balance_result = None
                        errors.append(str(balance_payload.get("error") or "资金接口失败"))
                else:
                    balance_result = balance_payload.get("result")

                positions_payload = live_payloads["positions"]
                if not positions_payload.get("ok"):
                    if cached_positions_result is not None:
                        positions_result = cached_positions_result
                        errors.append(
                            f"{str(positions_payload.get('error') or '持仓接口失败')}，当前展示最近一次任务缓存的持仓数据。"
                        )
                    else:
                        positions_result = None
                        errors.append(str(positions_payload.get("error") or "持仓接口失败"))
                else:
                    positions_result = positions_payload.get("result")

                orders_payload = live_payloads["orders"]
                if not orders_payload.get("ok"):
                    if cached_orders_result is not None:
                        orders_result = cached_orders_result
                        errors.append(
                            f"{str(orders_payload.get('error') or '委托接口失败')}，当前展示最近一次任务缓存的委托数据。"
                        )
                    else:
                        orders_result = None
                        errors.append(str(orders_payload.get("error") or "委托接口失败"))
                else:
                    orders_result = orders_payload.get("result")
            elif (
                balance_result is None
                and positions_result is None
                and orders_result is None
            ):
                return self.build_account_response(
                    balance_result=None,
                    positions_result=None,
                    orders_result=None,
                    errors=errors or ["未配置 MX API Key，且没有可用缓存账户数据。"],
                    include_raw=include_raw,
                )
        finally:
            if client is not None:
                client.close()

        overview = self.build_account_response(
            balance_result=balance_result,
            positions_result=positions_result,
            orders_result=orders_result,
            errors=errors,
            include_raw=include_raw,
        )
        self.set_cached_account_overview(
            self.build_account_response(
                balance_result=balance_result,
                positions_result=positions_result,
                orders_result=orders_result,
                errors=errors,
                include_raw=True,
            )
        )
        return overview

    def build_orders_overview(self, orders_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        orders_source = orders_payload.get("data") if isinstance(orders_payload, dict) else {}
        if isinstance(orders_source, dict):
            rows = (
                orders_source.get("rows")
                or orders_source.get("list")
                or orders_source.get("orderList")
                or orders_source.get("orders")
                or []
            )
        else:
            rows = orders_source or []

        normalized_orders: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue

            side_value = str(
                row.get("orderDrt")
                or row.get("drt")
                or row.get("bsFlag")
                or row.get("side")
                or row.get("tradeType")
                or ""
            ).strip()
            side = "sell" if side_value in {"2", "SELL", "sell"} else "buy"
            status_raw = str(
                row.get("orderStatus")
                or row.get("status")
                or row.get("dbStatus")
                or "unknown"
            ).strip()

            raw_symbol = str(row.get("stockCode") or row.get("secCode") or row.get("code") or "").strip()
            market_code = row.get("secMkt") if row.get("secMkt") is not None else row.get("market")
            suffix = _market_suffix(market_code)
            symbol = f"{raw_symbol}.{suffix}" if raw_symbol and suffix else raw_symbol

            order_quantity = int(
                _parse_float(
                    row.get("orderCount")
                    or row.get("count")
                    or row.get("quantity")
                    or row.get("orderQty")
                )
                or 0
            )
            filled_quantity = int(
                _parse_float(
                    row.get("dealCount")
                    or row.get("tradeCount")
                    or row.get("filledQuantity")
                    or row.get("filledQty")
                )
                or 0
            )

            normalized_orders.append(
                {
                    "order_id": str(row.get("orderId") or row.get("entrustNo") or row.get("id") or "--"),
                    "order_time": _format_timestamp(
                        row.get("orderTime") or row.get("entrustTime") or row.get("time")
                    ),
                    "name": str(row.get("stockName") or row.get("secName") or row.get("name") or "--").strip(),
                    "symbol": symbol,
                    "side": side,
                    "side_text": "卖出" if side == "sell" else "买入",
                    "status": status_raw.lower(),
                    "status_text": _order_status_text(
                        status_raw,
                        filled_quantity=filled_quantity,
                        order_quantity=order_quantity,
                        db_status=row.get("dbStatus"),
                    ),
                    "order_price": _scaled_decimal(
                        row.get("orderPrice") or row.get("price"),
                        row.get("priceDec") or row.get("orderPriceDec"),
                    ),
                    "order_quantity": order_quantity,
                    "filled_price": _scaled_decimal(
                        row.get("dealPrice") or row.get("tradePrice") or row.get("filledPrice"),
                        row.get("priceDec") or row.get("dealPriceDec"),
                    ),
                    "filled_quantity": filled_quantity,
                }
            )

        return normalized_orders

    def build_trade_summaries(
        self,
        orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        active_symbols = {
            str(position.get("symbol") or "").strip()
            for position in positions
            if isinstance(position, dict)
            and str(position.get("symbol") or "").strip()
            and int(_parse_float(position.get("volume")) or 0) > 0
        }

        grouped_orders: dict[str, list[dict[str, Any]]] = {}
        for order in orders:
            if not isinstance(order, dict):
                continue
            symbol = str(order.get("symbol") or "").strip()
            if not symbol:
                continue
            grouped_orders.setdefault(symbol, []).append(order)

        summaries: list[dict[str, Any]] = []
        for symbol, symbol_orders in grouped_orders.items():
            buy_lots: list[dict[str, Any]] = []
            matched_quantity = 0
            matched_buy_amount = 0.0
            matched_sell_amount = 0.0
            first_buy_time: str | None = None
            last_exit_time: str | None = None
            name = "--"

            sorted_orders = sorted(
                symbol_orders,
                key=lambda item: (str(item.get("order_time") or ""), str(item.get("order_id") or "")),
            )

            for order in sorted_orders:
                filled_quantity = int(_parse_float(order.get("filled_quantity")) or 0)
                if filled_quantity <= 0:
                    continue

                filled_price = _parse_float(order.get("filled_price"))
                if filled_price is None or filled_price <= 0:
                    filled_price = _parse_float(order.get("order_price"))
                if filled_price is None or filled_price <= 0:
                    continue

                order_name = str(order.get("name") or "").strip()
                if order_name:
                    name = order_name

                if str(order.get("side") or "") == "buy":
                    order_time = str(order.get("order_time") or "").strip() or None
                    if first_buy_time is None and order_time:
                        first_buy_time = order_time
                    buy_lots.append(
                        {
                            "quantity": filled_quantity,
                            "price": filled_price,
                            "order_time": order_time,
                        }
                    )
                    continue

                remaining_sell = filled_quantity
                while remaining_sell > 0 and buy_lots:
                    lot = buy_lots[0]
                    lot_quantity = int(lot.get("quantity") or 0)
                    lot_price = _parse_float(lot.get("price")) or 0.0
                    if lot_quantity <= 0 or lot_price <= 0:
                        buy_lots.pop(0)
                        continue

                    matched = min(remaining_sell, lot_quantity)
                    matched_quantity += matched
                    matched_buy_amount += lot_price * matched
                    matched_sell_amount += filled_price * matched
                    remaining_sell -= matched
                    lot["quantity"] = lot_quantity - matched
                    last_exit_time = str(order.get("order_time") or "").strip() or last_exit_time

                    if int(lot.get("quantity") or 0) <= 0:
                        buy_lots.pop(0)

            if matched_quantity <= 0:
                continue
            if symbol in active_symbols:
                continue
            if any(int(lot.get("quantity") or 0) > 0 for lot in buy_lots):
                continue
            if matched_buy_amount <= 0:
                continue

            profit = matched_sell_amount - matched_buy_amount
            summaries.append(
                {
                    "name": name or symbol,
                    "symbol": symbol,
                    "volume": matched_quantity,
                    "buy_amount": matched_buy_amount,
                    "sell_amount": matched_sell_amount,
                    "buy_price": matched_buy_amount / matched_quantity,
                    "sell_price": matched_sell_amount / matched_quantity,
                    "profit": profit,
                    "profit_ratio": profit / matched_buy_amount,
                    "opened_at": first_buy_time,
                    "closed_at": last_exit_time,
                }
            )

        summaries.sort(key=lambda item: str(item.get("closed_at") or ""), reverse=True)
        return summaries

    def build_account_overview(
        self,
        balance_payload: dict[str, Any] | None,
        positions_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        balance = balance_payload.get("data") if isinstance(balance_payload, dict) else {}
        positions_source = positions_payload.get("data") if isinstance(positions_payload, dict) else []
        if isinstance(positions_source, dict):
            rows = (
                positions_source.get("data")
                or positions_source.get("rows")
                or positions_source.get("list")
                or positions_source.get("posList")
                or []
            )
        else:
            rows = positions_source or []

        total_assets = None
        total_market_value = None
        holding_profit = None
        daily_profit = None
        daily_profit_trade_date = None
        open_date = None
        operating_days = None
        initial_capital = None
        cash_balance = None
        total_position_ratio = None
        nav = None

        if isinstance(balance, dict):
            open_date = _format_open_date(balance.get("openDate"))
            operating_days = int(_parse_float(balance.get("oprDays")) or 0) or None
            initial_capital = _parse_float(balance.get("initMoney"))
            total_assets = _parse_float(
                balance.get("totalAsset")
                or balance.get("totalAssets")
                or balance.get("asset")
                or balance.get("totalMoney")
                or (
                    (balance.get("result") or {}).get("totalAssets")
                    if isinstance(balance.get("result"), dict)
                    else None
                )
            )
            total_market_value = _parse_float(
                balance.get("marketValue")
                or balance.get("stockMarketValue")
                or balance.get("positionValue")
                or balance.get("totalPosValue")
            )
            cash_balance = _parse_float(
                balance.get("balanceActual")
                or balance.get("availBalance")
                or balance.get("cashBalance")
            )
            total_position_ratio = _normalize_percent(_parse_float(balance.get("totalPosPct")))
            holding_profit = _parse_float(
                balance.get("holdingProfit")
                or balance.get("positionProfit")
                or balance.get("floatProfit")
                or balance.get("totalProfit")
            )
            nav = _parse_float(balance.get("nav"))
            daily_profit = _parse_float(
                balance.get("todayProfit")
                or balance.get("dailyProfit")
                or balance.get("profitToday")
            )
            raw_trade_date = (
                balance.get("tradeDate")
                or balance.get("tradingDate")
                or balance.get("date")
                or balance.get("profitDate")
            )
            if raw_trade_date:
                text = str(raw_trade_date).strip()
                if len(text) == 8 and text.isdigit():
                    daily_profit_trade_date = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
                elif len(text) >= 10:
                    daily_profit_trade_date = text[:10]

        if holding_profit is None and isinstance(positions_source, dict):
            holding_profit = _parse_float(positions_source.get("totalProfit"))

        if daily_profit is None:
            daily_profit = sum(
                _parse_float(row.get("dayProfit")) or 0.0
                for row in rows
                if isinstance(row, dict)
            )

        total_return_ratio = None
        if nav is not None:
            total_return_ratio = nav - 1
        elif total_assets is not None and initial_capital not in (None, 0):
            total_return_ratio = total_assets / initial_capital - 1

        daily_return_ratio = None
        if daily_profit is not None and total_assets is not None:
            previous_assets = total_assets - daily_profit
            if previous_assets > 0:
                daily_return_ratio = daily_profit / previous_assets

        if daily_profit_trade_date is None:
            today = now_shanghai().date()
            if trading_calendar_service.is_trading_day(today):
                daily_profit_trade_date = today.isoformat()
            else:
                probe = today - timedelta(days=1)
                for _ in range(30):
                    if trading_calendar_service.is_trading_day(probe):
                        break
                    probe -= timedelta(days=1)
                daily_profit_trade_date = probe.isoformat()

        normalized_positions: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            amount = (
                _parse_float(
                    row.get("marketValue")
                    or row.get("market_amount")
                    or row.get("amount")
                    or row.get("positionValue")
                    or row.get("value")
                )
                or 0.0
            )
            profit_value = _parse_float(
                row.get("profitRatio")
                or row.get("profit_rate")
                or row.get("yieldRate")
                or row.get("profitPercent")
                or row.get("profitPct")
            )
            profit_ratio = _normalize_percent(profit_value)
            day_profit_ratio = _normalize_percent(_parse_float(row.get("dayProfitPct")))
            position_ratio = None
            if total_assets and total_assets > 0:
                position_ratio = max(0.0, min(1.0, amount / total_assets))
            if position_ratio is None:
                position_ratio = _normalize_percent(_parse_float(row.get("posPct")))

            raw_symbol = str(
                row.get("stockCode")
                or row.get("code")
                or row.get("SECURITY_CODE")
                or row.get("secCode")
                or ""
            ).strip()
            market_code = row.get("secMkt") if row.get("secMkt") is not None else row.get("market")
            suffix = _market_suffix(market_code)
            symbol = f"{raw_symbol}.{suffix}" if raw_symbol and suffix else raw_symbol

            normalized_positions.append(
                {
                    "name": str(
                        row.get("stockName")
                        or row.get("name")
                        or row.get("SECURITY_SHORT_NAME")
                        or row.get("secName")
                        or ""
                    ).strip(),
                    "symbol": symbol,
                    "amount": amount,
                    "volume": int(_parse_float(row.get("count")) or 0),
                    "available_volume": int(_parse_float(row.get("availCount")) or 0),
                    "day_profit": _parse_float(row.get("dayProfit")),
                    "day_profit_ratio": day_profit_ratio,
                    "profit": _parse_float(row.get("profit")),
                    "profit_ratio": profit_ratio,
                    "profit_text": self.format_profit_text(profit_ratio),
                    "current_price": _scaled_decimal(
                        _coalesce(row.get("price"), row.get("currentPrice")),
                        _coalesce(row.get("priceDec"), row.get("priceDecimal")),
                    ),
                    "cost_price": _scaled_decimal(
                        _coalesce(row.get("costPrice"), row.get("cost_price")),
                        _coalesce(row.get("costPriceDec"), row.get("costPriceDecimal")),
                    ),
                    "position_ratio": position_ratio,
                }
            )

        normalized_positions.sort(key=lambda item: item["amount"], reverse=True)
        return {
            "open_date": open_date,
            "daily_profit_trade_date": daily_profit_trade_date,
            "operating_days": operating_days,
            "initial_capital": initial_capital,
            "total_assets": total_assets,
            "total_market_value": total_market_value,
            "cash_balance": cash_balance,
            "total_position_ratio": total_position_ratio,
            "holding_profit": holding_profit
            if holding_profit is not None
            else _parse_float((positions_payload or {}).get("data", {}).get("totalProfit"))
            if isinstance((positions_payload or {}).get("data"), dict)
            else None,
            "daily_profit": daily_profit,
            "total_return_ratio": total_return_ratio,
            "nav": nav,
            "daily_return_ratio": daily_return_ratio,
            "positions": normalized_positions,
            "trade_summaries": [],
        }

    def format_profit_text(self, profit_ratio: float | None) -> str:
        if profit_ratio is None:
            return "--"
        return f"{profit_ratio * 100:.2f}%"


account_service = AccountService()
