from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.trading_calendar_service import trading_calendar_service

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ANALYSIS_TASK_NAMES = {"盘前分析", "午间复盘", "收盘分析"}
SCHEDULE_RETRY_DELAY = timedelta(minutes=5)
SCHEDULE_MAX_RETRIES = 3


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_shanghai() -> datetime:
    return now_utc().astimezone(SHANGHAI_TZ)


def assume_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_schedule_run_type(schedule_name: str | None, run_type: str | None) -> str:
    normalized = str(run_type or "").strip()
    if normalized in {"analysis", "trade"}:
        return normalized

    name = str(schedule_name or "").strip()
    if name.startswith("上午运行") or name.startswith("下午运行"):
        return "trade"
    return "analysis"


def compute_next_run_at(
    cron_expression: str | None,
    *,
    from_time: datetime | None = None,
) -> datetime | None:
    if not cron_expression:
        return None

    parts = cron_expression.strip().split()
    if len(parts) != 5:
        return None

    minute_expr, hour_expr, day_of_month_expr, month_expr, day_of_week_expr = parts
    try:
        minute_values = parse_cron_values(minute_expr, 0, 59)
        hour_values = parse_cron_values(hour_expr, 0, 23)
        day_of_month_values = parse_cron_values(day_of_month_expr, 1, 31)
        month_values = parse_cron_values(month_expr, 1, 12)
        day_of_week_values = parse_cron_values(
            day_of_week_expr,
            0,
            6,
            allow_seven_as_zero=True,
        )
    except ValueError:
        return None

    current_base = from_time or now_shanghai()
    if current_base.tzinfo is None:
        current_base = current_base.replace(tzinfo=SHANGHAI_TZ)
    else:
        current_base = current_base.astimezone(SHANGHAI_TZ)

    current = current_base.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(60 * 24 * 366 * 2):
        if not trading_calendar_service.is_trading_day(current.date()):
            next_day = trading_calendar_service.next_trading_day(current.date())
            current = datetime.combine(next_day, datetime.min.time(), tzinfo=SHANGHAI_TZ)
            continue

        if (
            current.minute in minute_values
            and current.hour in hour_values
            and current.month in month_values
            and matches_cron_day(
                current,
                day_of_month_values=day_of_month_values,
                day_of_week_values=day_of_week_values,
                day_of_month_expr=day_of_month_expr,
                day_of_week_expr=day_of_week_expr,
            )
        ):
            return current.astimezone(timezone.utc)
        current += timedelta(minutes=1)
    return None


def parse_cron_values(
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
            start = normalize_cron_value(
                int(start_text),
                minimum=minimum,
                maximum=maximum,
                allow_seven_as_zero=allow_seven_as_zero,
            )
            end = normalize_cron_value(
                int(end_text),
                minimum=minimum,
                maximum=maximum,
                allow_seven_as_zero=allow_seven_as_zero,
            )
            if start > end:
                raise ValueError("invalid cron range")
        else:
            numeric = normalize_cron_value(
                int(range_part),
                minimum=minimum,
                maximum=maximum,
                allow_seven_as_zero=allow_seven_as_zero,
            )
            start = numeric
            end = numeric

        allowed.update(range(start, end + 1, step))

    return allowed


def normalize_cron_value(
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


def matches_cron_day(
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
