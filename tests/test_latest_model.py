"""모델: 요청할 때 최신 Sonnet을 자동으로 쓰고, 사용자가 모델을 고르는 기능은 없다 (llm_service.current_model)

- 모델 ID를 코드에 고정하지 않는다: Anthropic Models API 목록에서 가장 최근에 나온 Sonnet (출시일로 비교)
- 목록은 한 시간 캐시하고, 새로 받지 못하면 마지막으로 받은 목록, 한 번도 받지 못했으면 FALLBACK_MODEL
- 사고 설정은 고른 모델이 지원하는 방식을 따른다 (adaptive / enabled)
- 요청의 modelId는 무시한다. 화면·Slack에 모델 선택이 없다

모델 목록과 출시일은 테스트용 예시다 (Anthropic Models API 응답과 같은 모양).
"""
import json
import threading

import pytest

from conftest import ROOT, load_service_module

# 목록은 최신 모델부터 온다 (Models API와 같은 순서)
MODELS = [
    {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "created_at": "2026-05-01T00:00:00Z"},
    {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8", "created_at": "2026-04-01T00:00:00Z"},
    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6", "created_at": "2026-02-01T00:00:00Z"},
    {"id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5", "created_at": "2025-10-01T00:00:00Z"},
    {"id": "claude-sonnet-4-5-20250929", "display_name": "Claude Sonnet 4.5", "created_at": "2025-09-29T00:00:00Z"},
    {"id": "claude-opus-4-1-20250805", "display_name": "Claude Opus 4.1", "created_at": "2025-08-05T00:00:00Z"},
]
NEWEST_SONNET = "claude-sonnet-5"


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture
def llm(aws, monkeypatch):
    module = load_service_module("services/llm", "llm_service")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    module._models_cache.update(at=0.0, models=[])
    return module


def serve(module, monkeypatch, pages, status=200):
    """requests.get을 가짜로 바꾼다. pages: 차례로 돌려줄 페이지들"""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(params or {}))
        return FakeResponse(status, pages[min(len(calls) - 1, len(pages) - 1)])

    monkeypatch.setattr(module.requests, "get", fake_get)
    return calls


def capable(model_id, adaptive=False, enabled=False, created="2026-01-01T00:00:00Z"):
    """Models API가 주는 모양 (사고 지원 방식 포함)."""
    return {"id": model_id, "display_name": model_id, "created_at": created,
            "capabilities": {"thinking": {"supported": adaptive or enabled, "types": {
                "adaptive": {"supported": adaptive}, "enabled": {"supported": enabled}}}}}


# ---------------------------------------------------------------- 최신 Sonnet 고르기

def test_latest_sonnet_by_release_date(llm):
    assert llm.latest_sonnet(MODELS)["id"] == NEWEST_SONNET
    # ID 형식이 세대마다 달라 ID가 아니라 출시일로 비교한다. 다른 계열(Haiku·Opus)이 더 최근이어도 고르지 않는다
    models = [{"id": "claude-sonnet-4-5-20250929", "created_at": "2025-09-29T00:00:00Z"},
              {"id": "claude-sonnet-4-6", "created_at": "2026-02-01T00:00:00Z"},
              {"id": "claude-opus-4-8", "created_at": "2026-04-01T00:00:00Z"},
              {"id": "claude-sonnet-unknown", "created_at": ""}]  # 출시일이 없으면 고르지 않는다
    assert llm.latest_sonnet(models)["id"] == "claude-sonnet-4-6"
    assert llm.latest_sonnet([m for m in MODELS if "sonnet" not in m["id"]]) is None


def test_new_sonnet_is_picked_up_without_a_deploy(llm, monkeypatch):
    serve(llm, monkeypatch, [{"data": MODELS, "has_more": False}])
    assert llm.current_model()["id"] == NEWEST_SONNET
    # 한 시간 뒤 목록에 새 Sonnet이 있으면 그 모델로 넘어간다. 이번 질문은 목록 조회를 기다리지 않고 캐시로 답하고,
    # 뒤에서 새로 받은 목록은 다음 질문부터 쓰인다
    llm._models_cache["at"] = 0.0
    newer = {"id": "claude-sonnet-6", "display_name": "Claude Sonnet 6", "created_at": "2026-12-01T00:00:00Z"}
    serve(llm, monkeypatch, [{"data": [newer] + MODELS, "has_more": False}])
    # 뒤에서 받는 조회를 잠시 붙잡아 둔다 (가짜 조회는 순식간이라, 붙잡지 않으면 확인하기 전에 끝날 수 있다)
    gate = threading.Event()
    refresh = llm._refresh_models
    monkeypatch.setattr(llm, "_refresh_models", lambda timeout=10: (gate.wait(5), refresh(timeout)))
    assert llm.current_model()["id"] == NEWEST_SONNET
    gate.set()
    llm._models_refresh.join(timeout=5)
    assert llm.current_model()["id"] == "claude-sonnet-6"


def test_models_are_listed_across_all_pages(llm, monkeypatch):
    calls = serve(llm, monkeypatch, [
        {"data": MODELS[:3], "has_more": True, "last_id": MODELS[2]["id"]},
        {"data": MODELS[3:], "has_more": False, "last_id": MODELS[-1]["id"]},
    ])
    assert [m["id"] for m in llm.get_anthropic_models()] == [m["id"] for m in MODELS]
    assert calls == [{"limit": 1000}, {"limit": 1000, "after_id": "claude-sonnet-4-6"}]


def test_model_list_is_cached_and_survives_errors(llm, monkeypatch):
    calls = serve(llm, monkeypatch, [{"data": MODELS, "has_more": False}])
    llm.current_model()
    llm.current_model()
    assert len(calls) == 1  # 한 시간 안에는 다시 조회하지 않는다
    # 캐시가 오래됐는데 새로 받지 못하면, 마지막으로 받은 목록을 계속 쓴다
    llm._models_cache["at"] = 0.0
    serve(llm, monkeypatch, [{}], status=503)
    assert llm.current_model()["id"] == NEWEST_SONNET
    llm._models_refresh.join(timeout=5)
    assert llm.current_model()["id"] == NEWEST_SONNET


def test_fallback_when_the_list_was_never_received(llm, monkeypatch):
    serve(llm, monkeypatch, [{"error": {"type": "authentication_error"}}], status=401)
    assert llm.get_anthropic_models() == []
    assert llm.current_model() == llm.FALLBACK_MODEL
    assert llm.thinking_config(llm.FALLBACK_MODEL) == {"type": "adaptive", "display": "summarized"}


# ---------------------------------------------------------------- 사고 설정은 고른 모델을 따른다

def test_thinking_config_follows_model_capabilities(llm, monkeypatch):
    serve(llm, monkeypatch, [{"data": [capable("claude-sonnet-5", adaptive=True, created="2026-05-01T00:00:00Z"),
                                       capable("claude-sonnet-4-0", enabled=True)], "has_more": False}])
    models = {m["id"]: m for m in llm.available_models()}
    # 최신 모델: adaptive만 받는다 (budget_tokens를 보내면 400). 사고 요약은 요청해야 온다
    assert llm.thinking_config(models["claude-sonnet-5"]) == {"type": "adaptive", "display": "summarized"}
    # 예전 모델: enabled + budget_tokens (max_tokens보다 작아야 한다)
    from mcp_anthropic_client import MAX_TOKENS
    config = llm.thinking_config(models["claude-sonnet-4-0"])
    assert config["type"] == "enabled" and 1024 <= config["budget_tokens"] < MAX_TOKENS
    assert llm.thinking_config({"id": "x", "thinking": None}) is None


# ---------------------------------------------------------------- 요청·클라이언트·/health

def test_client_uses_the_latest_sonnet_and_requested_model_is_ignored(llm, monkeypatch):
    serve(llm, monkeypatch, [{"data": [capable("claude-sonnet-5", adaptive=True, created="2026-05-01T00:00:00Z"),
                                       capable("claude-haiku-4-5", enabled=True)], "has_more": False}])
    made = []

    class FakeClient:
        def __init__(self, **kwargs):
            made.append(kwargs)
            self.model_id = kwargs["model_id"]

        def initialize(self):
            pass

    import mcp_anthropic_client
    monkeypatch.setattr(mcp_anthropic_client, "AnthropicMCPClient", FakeClient)
    monkeypatch.setattr(llm, "client_cache", {})
    client = llm.get_client()
    assert made[0]["model_id"] == NEWEST_SONNET
    assert made[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert llm.get_client() is client and len(made) == 1  # 같은 모델이면 클라이언트를 다시 만들지 않는다

    # 요청에 modelId가 있어도(예전 화면·Slack 설정) 고르지 않는다: get_client는 모델을 받지 않는다
    asked = []

    def stop(*args):
        asked.append(args)
        raise RuntimeError("모델만 확인하고 멈춘다")

    monkeypatch.setattr(llm, "get_client", stop)
    llm.handle_llm1_with_mcp({"text": "안녕", "modelId": "claude-haiku-4-5"}, "https://x", caller_id="alice")
    # 넘기는 것은 시간 기록(timing.Stopwatch)뿐이고 모델은 넘기지 않는다
    assert len(asked) == 1 and [type(arg).__name__ for arg in asked[0]] == ["Stopwatch"]


def test_health_reports_the_current_model(aws, monkeypatch):
    module = load_service_module("services/llm", "lambda_function")
    import llm_service
    monkeypatch.setattr(llm_service, "available_models", lambda: MODELS)
    response = module.lambda_handler({"path": "/health", "httpMethod": "GET", "headers": {}}, None)
    assert json.loads(response["body"]) == {"status": "ok",
                                            "model": {"id": NEWEST_SONNET, "display_name": "Claude Sonnet 5"}}


def test_anthropic_client_requires_a_model(aws):
    load_service_module("services/llm", "llm_service")
    from mcp_anthropic_client import AnthropicMCPClient
    with pytest.raises(ValueError, match="model_id"):
        AnthropicMCPClient(mcp_url="https://example.invalid", api_key="k")


def test_no_model_selection_anywhere():
    # Slack: /models 명령·경로·모델 선택 저장이 없다
    slack = (ROOT / "services" / "slackbot" / "slackbot_service.py").read_text(encoding="utf-8")
    routes = (ROOT / "services" / "slackbot" / "lambda_function.py").read_text(encoding="utf-8")
    template = (ROOT / "cloudformation" / "slackbot.yaml").read_text(encoding="utf-8")
    assert "selected_model" not in slack and "modelId" not in slack and "handle_models_command" not in slack
    assert '"/models"' not in routes and "PathPart: \"models\"" not in template
    # 화면: 모델 목록 저장소와 고르는 칸이 없고, 요청에 modelId를 보내지 않는다
    frontend = ROOT / "frontend" / "src"
    assert not (frontend / "stores" / "modelsStore.ts").exists()
    for path in (frontend / "features" / "chat" / "Composer.tsx", frontend / "stores" / "chatStore.ts"):
        text = path.read_text(encoding="utf-8")
        assert "modelId" not in text and "useModelsStore" not in text, path
