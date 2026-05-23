from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core import rate_limit as rate_limit_module
from app.db import database as database_module
from app.db.database import session_scope
from app.db.models import ChatMessageRecord, ChatSession, StrategyRun
from app.main import create_app
from app.skills import skill_registry
from app.services.event_bus import event_bus
from app.services.llm_service import llm_service
from app.services.scheduler_service import scheduler_service
from app.services.chat_session_service import chat_session_service
from app.services.trading_calendar_service import trading_calendar_service


def create_test_client(monkeypatch, tmp_path) -> TestClient:
    from app.services.aniu_service import aniu_service

    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(trading_calendar_service, "ensure_years", lambda years: None)
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    rate_limit_module._limiter.reset()
    aniu_service._account_overview_cache = None
    aniu_service._account_overview_cache_expires_at = None
    app = create_app()
    return TestClient(app)


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/aniu/login",
        json={"password": "release-pass"},
    )
    payload = response.json()
    return {"Authorization": f"Bearer {payload['token']}"}


def test_login_endpoint_accepts_configured_credentials(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/aniu/login",
            json={"password": "release-pass"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["token"]
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_login_endpoint_rejects_invalid_credentials(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/aniu/login",
            json={"password": "wrong-password"},
        )

    assert response.status_code == 401
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_authenticate_login_uses_compare_digest(monkeypatch, tmp_path) -> None:
    from app.services import aniu_service as aniu_service_module

    captured: dict[str, str] = {}

    def fake_compare_digest(left: str, right: str) -> bool:
        captured["left"] = left
        captured["right"] = right
        return True

    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(aniu_service_module.secrets, "compare_digest", fake_compare_digest)
    get_settings.cache_clear()

    payload = aniu_service_module.aniu_service.authenticate_login("release-pass")

    assert payload["authenticated"] is True
    assert captured == {
        "left": "release-pass",
        "right": "release-pass",
    }

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_login_rate_limit_ignores_spoofed_forwarded_for_by_default(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(trading_calendar_service, "ensure_years", lambda years: None)
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    rate_limit_module._limiter.reset()
    app = create_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        for index in range(10):
            response = client.post(
                "/api/aniu/login",
                json={"password": "wrong-password"},
                headers={"X-Forwarded-For": f"198.51.100.{index}"},
            )
            assert response.status_code == 401

        blocked = client.post(
            "/api/aniu/login",
            json={"password": "wrong-password"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )

    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "请求过于频繁，请稍后再试。"

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_login_rate_limit_can_trust_forwarded_for_when_enabled(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("TRUST_X_FORWARDED_FOR", "true")

    with create_test_client(monkeypatch, tmp_path) as client:
        responses = [
            client.post(
                "/api/aniu/login",
                json={"password": "wrong-password"},
                headers={"X-Forwarded-For": f"198.51.100.{index}"},
            )
            for index in range(11)
        ]

    assert all(response.status_code == 401 for response in responses)

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_run_stream_endpoint_is_rate_limited(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    monkeypatch.setattr(aniu_service, "start_run_async", lambda **kwargs: 42)

    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(trading_calendar_service, "ensure_years", lambda years: None)
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    rate_limit_module._limiter.reset()
    app = create_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        headers = _auth_headers(client)

        for _ in range(5):
            response = client.post("/api/aniu/run-stream", headers=headers)
            assert response.status_code == 200
            assert response.json()["run_id"] == 42

        blocked = client.post("/api/aniu/run-stream", headers=headers)

    assert blocked.status_code == 429

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_run_stream_endpoint_passes_manual_trade_run_type(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    captured: dict[str, object] = {}

    def fake_start_run_async(**kwargs):
      captured.update(kwargs)
      return 99

    monkeypatch.setattr(aniu_service, "start_run_async", fake_start_run_async)
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(trading_calendar_service, "ensure_years", lambda years: None)
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None
    rate_limit_module._limiter.reset()
    app = create_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        headers = _auth_headers(client)
        response = client.post("/api/aniu/run-stream?run_type=trade", headers=headers)

    assert response.status_code == 200
    assert response.json()["run_id"] == 99
    assert captured["trigger_source"] == "manual"
    assert captured["schedule_id"] is None
    assert captured["manual_run_type"] == "trade"

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_app_startup_requires_current_year_trading_calendar(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APP_LOGIN_PASSWORD", "release-pass")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(
        trading_calendar_service,
        "ensure_years",
        lambda years: (_ for _ in ()).throw(RuntimeError("calendar unavailable"))
        if years == [2026]
        else None,
    )
    monkeypatch.setattr(scheduler_service, "start", lambda: None)
    monkeypatch.setattr(scheduler_service, "stop", lambda: None)
    monkeypatch.setattr("app.main.date", type("FakeDate", (), {"today": staticmethod(lambda: type("Today", (), {"year": 2026})())}))
    get_settings.cache_clear()
    database_module._engine = None
    database_module._session_local = None

    app = create_app()

    with pytest.raises(RuntimeError, match="calendar unavailable"):
        with TestClient(app):
            pass

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_chat_endpoint_returns_assistant_message(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    monkeypatch.setattr(
        aniu_service,
        "chat",
        lambda payload: {
            "message": {
                "role": "assistant",
                "content": "测试回复",
            },
            "context": {
                "system_prompt_included": True,
                "tool_access_account_summary": True,
                "tool_access_positions": True,
                "tool_access_orders": True,
                "tool_access_runs": True,
            },
        },
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/api/aniu/chat",
            json={
                "messages": [
                    {"role": "user", "content": "你好"},
                ],
            },
            headers=headers,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"]["role"] == "assistant"
    assert payload["message"]["content"] == "测试回复"

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_settings_endpoint_updates_max_context_tokens(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.put(
            "/api/aniu/settings",
            json={
                "provider_name": "openai-compatible",
                "mx_api_key": None,
                "llm_base_url": "https://example.com/v1",
                "llm_api_key": "sk-test",
                "llm_model": "gpt-5.4",
                "automation_context_window_tokens": 128000,
                "system_prompt": "system prompt",
                "automation_session_id": None,
                "automation_recent_message_limit": 24,
                "automation_enable_auto_compaction": True,
                "automation_idle_summary_hours": 12,
            },
            headers=headers,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["llm_model"] == "gpt-5.4"
    assert payload["automation_context_window_tokens"] == 128000

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_chat_endpoint_rejects_empty_messages(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.post(
            "/api/aniu/chat",
            json={
                "messages": [],
            },
            headers=headers,
        )

    assert response.status_code == 422
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_chat_tools_available_for_chat_run_type(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        tool_names = {
            spec["function"]["name"] for spec in skill_registry.build_tools(run_type="chat")
        }

    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "edit_file" in tool_names
    assert "list_dir" in tool_names
    assert "glob" in tool_names
    assert "grep" in tool_names
    assert "exec" in tool_names
    assert "web_search" in tool_names
    assert "web_fetch" in tool_names
    assert "http_get" in tool_names
    assert "http_post" in tool_names
    assert "file_read" not in tool_names
    assert "file_write" not in tool_names
    assert "file_list" not in tool_names
    assert "bash_exec" not in tool_names
    assert "chat_get_account_summary" in tool_names
    assert "chat_get_positions" in tool_names
    assert "chat_get_orders" in tool_names
    assert "chat_list_runs" in tool_names
    assert "chat_get_run_detail" in tool_names
    assert "mx_query_market" in tool_names
    assert "mx_search_news" in tool_names
    assert "mx_screen_stocks" in tool_names
    assert "mx_get_positions" in tool_names
    assert "mx_get_balance" in tool_names
    assert "mx_get_orders" in tool_names
    assert "mx_get_self_selects" in tool_names
    assert "mx_manage_self_select" in tool_names
    assert "mx_moni_trade" in tool_names
    assert "mx_moni_cancel" in tool_names

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_runtime_read_file_can_access_builtin_skill_docs(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        target = Path(__file__).resolve().parents[1] / "skills" / "builtin_utils" / "SKILL.md"
        result = skill_registry.execute_tool(
            tool_name="read_file",
            arguments={"path": str(target), "offset": 1, "limit": 20},
            context={"run_type": "chat"},
        )

    assert result["ok"] is True
    assert "通用技能运行时" in result["result"]["content"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_runtime_read_file_can_access_chat_upload_text_files(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            session = chat_session_service.create_session(db, title="Upload Read")
            attachment = chat_session_service.save_attachment(
                db,
                filename="notes.md",
                mime_type="text/markdown",
                data=b"# hello\nworld",
                session_id=session.id,
            )

        upload_root = tmp_path / "chat_uploads"
        targets = list(upload_root.rglob("*.md"))
        assert len(targets) == 1

        result = skill_registry.execute_tool(
            tool_name="read_file",
            arguments={"path": str(targets[0]), "offset": 1, "limit": 20},
            context={"run_type": "chat"},
        )

    assert attachment.filename == "notes.md"
    assert result["ok"] is True
    assert "1| # hello" in result["result"]["content"]
    assert "2| world" in result["result"]["content"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_runtime_read_file_uses_skill_runtime_paths_from_context(monkeypatch, tmp_path) -> None:
    workspace_root = tmp_path / "custom_workspace"
    builtin_root = tmp_path / "custom_builtin"
    chat_uploads_root = tmp_path / "custom_uploads"
    target = chat_uploads_root / "notes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    with create_test_client(monkeypatch, tmp_path):
        result = skill_registry.execute_tool(
            tool_name="read_file",
            arguments={"path": str(target), "offset": 1, "limit": 20},
            context={
                "run_type": "chat",
                "skill_runtime_paths": {
                    "workspace_root": str(workspace_root),
                    "builtin_skills_root": str(builtin_root),
                    "chat_uploads_root": str(chat_uploads_root),
                },
            },
        )

    assert result["ok"] is True
    assert "1| alpha" in result["result"]["content"]
    assert "2| beta" in result["result"]["content"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_removed_runtime_aliases_are_no_longer_available(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        result = skill_registry.execute_tool(
            tool_name="file_read",
            arguments={"path": "skills/builtin_utils/SKILL.md"},
            context={"run_type": "chat"},
        )

    assert result["ok"] is False
    assert "未知工具调用" in result["error"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_chat_system_prompt_always_appends_confirmation_rule(monkeypatch) -> None:
    monkeypatch.setattr(
        skill_registry,
        "build_prompt_supplement",
        lambda *, run_type=None: "技能补充提示" if run_type == "chat" else "",
    )

    chat_prompt = llm_service._augment_system_prompt(
        "用户自定义系统提示词",
        run_type="chat",
    )
    analysis_prompt = llm_service._augment_system_prompt(
        "用户自定义系统提示词",
        run_type="analysis",
    )

    assert "用户自定义系统提示词" in chat_prompt
    assert "技能补充提示" in chat_prompt
    assert "必须先明确说明拟执行操作、影响范围和潜在风险" in chat_prompt
    assert "得到用户明确确认后才能调用工具或执行操作" in chat_prompt
    assert "必须先明确说明拟执行操作、影响范围和潜在风险" not in analysis_prompt


def test_chat_prompt_supplement_limits_read_file_to_plain_text(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        supplement = skill_registry.build_prompt_supplement(run_type="chat")
        tools = skill_registry.build_tools(run_type="chat")

    read_file_spec = next(
        spec for spec in tools if spec.get("function", {}).get("name") == "read_file"
    )
    description = read_file_spec.get("function", {}).get("description", "")

    assert "纯文本文件" in supplement
    assert "不要对 PDF、图片、docx/xlsx/pptx 等二进制附件调用 `read_file`" in supplement
    assert "plain text file" in description
    assert "Do not use for PDFs, images, Office files, or other binary documents." in description


def test_mx_core_tools_can_execute_in_chat_without_prebuilt_client(
    monkeypatch, tmp_path
) -> None:
    from skills.mx_core import handler as mx_core_handler

    captured: dict[str, object] = {}

    class DummyMXClient:
        def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        def __enter__(self):
            captured["entered"] = True
            return self

        def __exit__(self, *args: object) -> None:
            captured["exited"] = True

    def fake_execute_tool(*, client, app_settings, tool_name, arguments):
        captured["client"] = client
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["task_prompt"] = getattr(app_settings, "task_prompt", None)
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": "ok",
            "result": {"connected": True},
        }

    monkeypatch.setattr(mx_core_handler, "MXClient", DummyMXClient)
    monkeypatch.setattr(mx_core_handler.mx_skill_service, "execute_tool", fake_execute_tool)

    with create_test_client(monkeypatch, tmp_path):
        result = skill_registry.execute_tool(
            tool_name="mx_get_balance",
            arguments={},
            context={
                "run_type": "chat",
                "app_settings": SimpleNamespace(mx_api_key="mx-chat-key", task_prompt=""),
            },
        )

    assert result["ok"] is True
    assert captured["api_key"] == "mx-chat-key"
    assert captured["tool_name"] == "mx_get_balance"
    assert captured["entered"] is True
    assert captured["exited"] is True

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_mx_core_tools_use_mx_client_config_from_context(monkeypatch, tmp_path) -> None:
    from skills.mx_core import handler as mx_core_handler

    captured: dict[str, object] = {}

    class DummyMXClient:
        def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        def __enter__(self):
            captured["entered"] = True
            return self

        def __exit__(self, *args: object) -> None:
            captured["exited"] = True

    def fake_execute_tool(*, client, app_settings, tool_name, arguments):
        captured["client"] = client
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["task_prompt"] = getattr(app_settings, "task_prompt", None)
        return {
            "ok": True,
            "tool_name": tool_name,
            "summary": "ok",
            "result": {"connected": True},
        }

    monkeypatch.setattr(mx_core_handler, "MXClient", DummyMXClient)
    monkeypatch.setattr(mx_core_handler.mx_skill_service, "execute_tool", fake_execute_tool)

    with create_test_client(monkeypatch, tmp_path):
        result = skill_registry.execute_tool(
            tool_name="mx_get_balance",
            arguments={},
            context={
                "run_type": "chat",
                "app_settings": SimpleNamespace(mx_api_key="ignored-key", task_prompt=""),
                "mx_client_config": {
                    "api_key": "mx-context-key",
                    "base_url": "https://mx.example.test/api",
                },
            },
        )

    assert result["ok"] is True
    assert captured["api_key"] == "mx-context-key"
    assert captured["base_url"] == "https://mx.example.test/api"
    assert captured["tool_name"] == "mx_get_balance"
    assert captured["entered"] is True
    assert captured["exited"] is True

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_chat_context_tools_read_account_and_runs(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    monkeypatch.setattr(
        aniu_service,
        "get_account_overview",
        lambda **kwargs: {
            "open_date": "2026-01-01",
            "daily_profit_trade_date": "2026-04-18",
            "operating_days": 30,
            "initial_capital": 200000.0,
            "total_assets": 212345.67,
            "total_market_value": 156789.0,
            "cash_balance": 55556.67,
            "total_position_ratio": 73.8,
            "holding_profit": 12345.67,
            "total_return_ratio": 6.17,
            "nav": 1.0617,
            "daily_profit": 1234.5,
            "daily_return_ratio": 0.58,
            "positions": [
                {
                    "name": "东方财富",
                    "symbol": "300059.SZ",
                    "volume": 1000,
                    "available_volume": 800,
                    "profit_text": "+1234.00",
                },
                {
                    "name": "贵州茅台",
                    "symbol": "600519.SH",
                    "volume": 100,
                    "available_volume": 100,
                    "profit_text": "+5678.00",
                },
            ],
            "orders": [
                {
                    "order_id": "A001",
                    "name": "东方财富",
                    "symbol": "300059.SZ",
                    "side_text": "买入",
                    "status_text": "已报",
                }
            ],
            "trade_summaries": [],
            "errors": [],
        },
    )

    with create_test_client(monkeypatch, tmp_path):
        account_result = skill_registry.execute_tool(
            tool_name="chat_get_account_summary",
            arguments={},
            context={"run_type": "chat"},
        )
        positions_result = skill_registry.execute_tool(
            tool_name="chat_get_positions",
            arguments={"limit": 1},
            context={"run_type": "chat"},
        )
        orders_result = skill_registry.execute_tool(
            tool_name="chat_get_orders",
            arguments={"limit": 1},
            context={"run_type": "chat"},
        )

    assert account_result["ok"] is True
    assert account_result["result"]["account"]["total_assets"] == 212345.67
    assert positions_result["ok"] is True
    assert positions_result["result"]["total"] == 2
    assert len(positions_result["result"]["items"]) == 1
    assert orders_result["ok"] is True
    assert orders_result["result"]["items"][0]["order_id"] == "A001"

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_chat_context_tools_list_and_read_run_detail(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        with session_scope() as db:
            db.add(
                StrategyRun(
                    trigger_source="manual",
                    run_type="analysis",
                    schedule_name="盘前分析",
                    status="completed",
                    analysis_summary="早盘看多，建议关注券商。",
                    final_answer="最终结论：维持偏多判断，优先观察券商与AI方向。",
                    decision_payload={
                        "tool_calls": [
                            {"name": "mx_query_market"},
                        ]
                    },
                    llm_response_payload={
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 34,
                            "total_tokens": 46,
                        }
                    },
                )
            )
            db.flush()
            run_id = db.query(StrategyRun.id).order_by(StrategyRun.id.desc()).first()[0]

        list_result = skill_registry.execute_tool(
            tool_name="chat_list_runs",
            arguments={"limit": 5},
            context={"run_type": "chat"},
        )
        detail_result = skill_registry.execute_tool(
            tool_name="chat_get_run_detail",
            arguments={"run_id": run_id},
            context={"run_type": "chat"},
        )

    assert list_result["ok"] is True
    assert list_result["result"]["items"][0]["id"] == run_id
    assert list_result["result"]["items"][0]["content_preview"] == "早盘看多，建议关注券商。"
    assert detail_result["ok"] is True
    assert detail_result["result"]["id"] == run_id
    assert detail_result["result"]["final_answer"] == "最终结论：维持偏多判断，优先观察券商与AI方向。"
    assert detail_result["result"]["api_call_count"] == 1

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_runs_endpoint_returns_lightweight_summary(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            db.add(
                StrategyRun(
                    trigger_source="manual",
                    run_type="analysis",
                    schedule_name="盘前分析",
                    status="completed",
                    analysis_summary="摘要",
                    final_answer="详细输出",
                    decision_payload={
                        "tool_calls": [
                            {"name": "mx_query_market"},
                            {"name": "mx_moni_trade"},
                        ]
                    },
                    executed_actions=[{"action": "BUY", "symbol": "300059"}],
                    llm_response_payload={
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 22,
                            "total_tokens": 33,
                        }
                    },
                )
            )

        headers = _auth_headers(client)
        response = client.get("/api/aniu/runs?limit=20", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    run = payload[0]
    assert run["analysis_summary"] == "摘要"
    assert run["api_call_count"] == 1
    assert run["executed_trade_count"] == 1
    assert run["input_tokens"] == 11
    assert run["output_tokens"] == 22
    assert run["total_tokens"] == 33
    assert "final_answer" not in run
    assert "decision_payload" not in run
    assert "executed_actions" not in run

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_runs_endpoint_filters_by_date(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            db.add_all(
                [
                    StrategyRun(
                        trigger_source="manual",
                        run_type="analysis",
                        status="completed",
                        analysis_summary="today",
                        started_at=datetime(2026, 4, 14, 8, 30, 0),
                    ),
                    StrategyRun(
                        trigger_source="manual",
                        run_type="analysis",
                        status="completed",
                        analysis_summary="yesterday",
                        started_at=datetime(2026, 4, 13, 8, 30, 0),
                    ),
                ]
            )

        headers = _auth_headers(client)
        response = client.get("/api/aniu/runs?date=2026-04-14&limit=20", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["analysis_summary"] == "today"

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_runs_feed_returns_pagination_metadata(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            db.add_all(
                [
                    StrategyRun(
                        trigger_source="manual",
                        run_type="analysis",
                        status="completed",
                        analysis_summary=f"run-{index}",
                        started_at=datetime(2026, 4, 14, 8, index, 0),
                    )
                    for index in range(3)
                ]
            )

        headers = _auth_headers(client)
        response = client.get("/api/aniu/runs-feed?limit=2", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 2
    assert payload["has_more"] is True
    assert payload["next_before_id"] is not None

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_run_events_endpoint_emits_failed_event_when_event_bus_stream_errors(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        event_bus,
        "stream",
        lambda run_id: (_ for _ in ()).throw(RuntimeError(f"stream boom: {run_id}")),
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.get("/api/aniu/runs/123/events", headers=headers)

    assert response.status_code == 200
    assert "event: failed" in response.text
    assert "stream boom: 123" in response.text

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_runtime_overview_endpoint_returns_aggregated_stats(monkeypatch, tmp_path) -> None:
    shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            db.add_all(
                [
                    StrategyRun(
                        trigger_source="manual",
                        run_type="analysis",
                        status="completed",
                        analysis_summary="today-1",
                        decision_payload={
                            "tool_calls": [
                                {"name": "mx_query_market"},
                                {"name": "mx_search_news"},
                                {"name": "mx_moni_trade"},
                            ]
                        },
                        executed_actions=[{"action": "BUY", "symbol": "300059"}],
                        llm_response_payload={
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 20,
                                "total_tokens": 30,
                            }
                        },
                        started_at=shanghai_now.replace(tzinfo=None),
                        finished_at=shanghai_now.replace(tzinfo=None),
                    ),
                    StrategyRun(
                        trigger_source="manual",
                        run_type="analysis",
                        status="failed",
                        analysis_summary="today-2",
                        decision_payload={
                            "tool_calls": [
                                {"name": "mx_get_balance"},
                            ]
                        },
                        llm_response_payload={
                            "usage": {
                                "prompt_tokens": 5,
                                "completion_tokens": 6,
                                "total_tokens": 11,
                            }
                        },
                        started_at=shanghai_now.replace(tzinfo=None),
                        finished_at=shanghai_now.replace(tzinfo=None),
                    ),
                ]
            )

def test_delete_run_endpoint_removes_run_and_related_messages(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            session = ChatSession(title="自动化交易会话", kind="automation", slug="automation-default")
            db.add(session)
            db.flush()
            run = StrategyRun(
                trigger_source="manual",
                run_type="analysis",
                status="completed",
                chat_session_id=session.id,
            )
            db.add(run)
            db.flush()
            db.add_all(
                [
                    ChatMessageRecord(
                        session_id=session.id,
                        role="user",
                        content="u",
                        run_id=run.id,
                    ),
                    ChatMessageRecord(
                        session_id=session.id,
                        role="assistant",
                        content="a",
                        run_id=run.id,
                    ),
                ]
            )
            run_id = run.id

        headers = _auth_headers(client)
        response = client.delete(f"/api/aniu/runs/{run_id}", headers=headers)

        assert response.status_code == 204

        with session_scope() as db:
            assert db.get(StrategyRun, run_id) is None
            linked_messages = (
                db.query(ChatMessageRecord)
                .filter(ChatMessageRecord.run_id == run_id)
                .all()
            )
            assert linked_messages == []

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_delete_run_endpoint_rejects_running_task(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            run = StrategyRun(
                trigger_source="manual",
                run_type="analysis",
                status="running",
            )
            db.add(run)
            db.flush()
            run_id = run.id

        headers = _auth_headers(client)
        response = client.delete(f"/api/aniu/runs/{run_id}", headers=headers)

        assert response.status_code == 409
        assert "不可删除" in response.json()["detail"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_delete_run_endpoint_force_deletes_stuck_running_task(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            run = StrategyRun(
                trigger_source="manual",
                run_type="analysis",
                status="running",
            )
            db.add(run)
            db.flush()
            run_id = run.id

        headers = _auth_headers(client)
        response = client.delete(f"/api/aniu/runs/{run_id}?force=true", headers=headers)

        assert response.status_code == 204

        with session_scope() as db:
            assert db.get(StrategyRun, run_id) is None

    assert aniu_service._run_lock.locked() is False
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_delete_run_endpoint_force_still_rejects_when_service_is_busy(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            run = StrategyRun(
                trigger_source="manual",
                run_type="analysis",
                status="running",
            )
            db.add(run)
            db.flush()
            run_id = run.id

        acquired = aniu_service._run_lock.acquire(blocking=False)
        assert acquired is True
        try:
            headers = _auth_headers(client)
            response = client.delete(f"/api/aniu/runs/{run_id}?force=true", headers=headers)
        finally:
            aniu_service._run_lock.release()

        assert response.status_code == 409
        assert "当前仍有任务正在执行" in response.json()["detail"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_persistent_session_endpoint_returns_summary(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            session = ChatSession(
                title="自动化交易会话",
                kind="automation",
                slug="automation-default",
                archived_summary="## 当前策略\n- 继续观察",
                summary_revision=3,
            )
            db.add(session)
            db.flush()
            db.add(
                ChatMessageRecord(
                    session_id=session.id,
                    role="assistant",
                    content="summary",
                )
            )

        headers = _auth_headers(client)
        response = client.get("/api/aniu/persistent-session", headers=headers)

        assert response.status_code == 200
        payload = response.json()
        assert payload["title"] == "自动化交易会话"
        assert payload["slug"] == "automation-default"
        assert payload["message_count"] == 1
        assert payload["summary_revision"] == 3
        assert "继续观察" in payload["archived_summary"]

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_context_summary_system_message_uses_compressed_summary_label(
    monkeypatch, tmp_path
) -> None:
    with create_test_client(monkeypatch, tmp_path):
        from app.services.aniu_service import aniu_service

        session = ChatSession(
            title="自动化交易会话",
            kind="automation",
            slug="automation-default",
            archived_summary="## 当前策略\n- 继续观察",
        )

        message = aniu_service._build_persistent_session_context_system_message(
            session=session,
        )

    assert message == {
        "role": "system",
        "content": "[上下文压缩摘要]\n## 当前策略\n- 继续观察",
    }
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_history_messages_no_longer_append_tool_summaries(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path):
        from app.services.aniu_service import aniu_service

        records = [
            ChatMessageRecord(
                role="assistant",
                content="原始回复",
                tool_calls=[
                    {
                        "name": "mx_query_market",
                        "result": {"summary": "已读取行情"},
                    }
                ],
            )
        ]

        messages = aniu_service._build_persistent_session_history_messages(records)

    assert messages == [{"role": "assistant", "content": "原始回复"}]
    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_persistent_session_messages_endpoint_returns_messages(monkeypatch, tmp_path) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            session = ChatSession(
                title="自动化交易会话",
                kind="automation",
                slug="automation-default",
            )
            db.add(session)
            db.flush()
            db.add_all(
                [
                    ChatMessageRecord(
                        session_id=session.id,
                        role="user",
                        content="first",
                    ),
                    ChatMessageRecord(
                        session_id=session.id,
                        role="assistant",
                        content="second",
                    ),
                ]
            )

        headers = _auth_headers(client)
        response = client.get(
            "/api/aniu/persistent-session/messages?limit=10",
            headers=headers,
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["session"]["slug"] == "automation-default"
        assert [item["content"] for item in payload["messages"]] == ["first", "second"]
        assert payload["has_more"] is False

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_delete_persistent_session_endpoint_clears_messages_and_summary(
    monkeypatch, tmp_path
) -> None:
    with create_test_client(monkeypatch, tmp_path) as client:
        with session_scope() as db:
            session = ChatSession(
                title="自动化交易会话",
                kind="automation",
                slug="automation-default",
                archived_summary="## 当前策略\n- 继续观察",
                summary_revision=2,
                last_compacted_message_id=99,
                last_compacted_run_id=88,
            )
            db.add(session)
            db.flush()
            db.add_all(
                [
                    ChatMessageRecord(
                        session_id=session.id,
                        role="user",
                        content="first",
                    ),
                    ChatMessageRecord(
                        session_id=session.id,
                        role="assistant",
                        content="second",
                    ),
                ]
            )

        headers = _auth_headers(client)
        response = client.delete("/api/aniu/persistent-session", headers=headers)

        assert response.status_code == 204

        summary_response = client.get("/api/aniu/persistent-session", headers=headers)
        assert summary_response.status_code == 200
        summary_payload = summary_response.json()
        assert summary_payload["title"] == "自动化交易会话"
        assert summary_payload["slug"] == "automation-default"
        assert summary_payload["message_count"] == 0
        assert summary_payload["archived_summary"] is None
        assert summary_payload["summary_revision"] == 0

        messages_response = client.get(
            "/api/aniu/persistent-session/messages?limit=10",
            headers=headers,
        )
        assert messages_response.status_code == 200
        assert messages_response.json()["messages"] == []

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_account_endpoint_excludes_raw_payloads_by_default(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    monkeypatch.setattr(
        aniu_service,
        "get_account_overview",
        lambda **kwargs: {
            "open_date": None,
            "daily_profit_trade_date": None,
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
            "errors": [],
        },
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.get("/api/aniu/account", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert "raw_balance" not in payload
    assert "raw_positions" not in payload
    assert "raw_orders" not in payload

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()


def test_account_debug_endpoint_includes_raw_payloads(monkeypatch, tmp_path) -> None:
    from app.services.aniu_service import aniu_service

    monkeypatch.setattr(
        aniu_service,
        "get_account_overview",
        lambda **kwargs: {
            "open_date": None,
            "daily_profit_trade_date": None,
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
            "raw_balance": {"a": 1},
            "raw_positions": {"b": 2},
            "raw_orders": {"c": 3},
            "errors": [],
        },
    )

    with create_test_client(monkeypatch, tmp_path) as client:
        headers = _auth_headers(client)
        response = client.get("/api/aniu/account/debug", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["raw_balance"] == {"a": 1}
    assert payload["raw_positions"] == {"b": 2}
    assert payload["raw_orders"] == {"c": 3}

    database_module._engine = None
    database_module._session_local = None
    get_settings.cache_clear()
