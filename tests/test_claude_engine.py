"""Проверка интеграции с SDK Anthropic без сети: HTTP-транспорт подменён заглушкой.

Тесты подтверждают, что запрос собирается так, как задумано (модель, схема ответа, fallback,
кэширование), и что ответы и ошибки API корректно превращаются в результат или EngineError.
Реальный вызов API они не заменяют: его проверяет scripts/evaluate.py --engine claude.
"""

import json

import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from app.engines import EngineError  # noqa: E402
from app.engines.claude import FALLBACK_BETA, ClaudeEngine, estimate_cost  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402

EXTRACTION = {
    "category": "lead",
    "urgency": "high",
    "summary": "Срочно нужен кондиционер в детскую с установкой завтра",
    "contact": {"name": "Игорь", "phone": "+7 900 000-00-19", "email": None, "company": None},
    "interest": "кондиционер в детскую с установкой",
    "budget_rub": 70000,
    "deadline": "завтра или послезавтра",
    "suggested_reply": "Здравствуйте, Игорь! Подберём модель из наличия и согласуем установку.",
    "confidence": 0.94,
}
MESSAGE = IncomingMessage(
    channel="whatsapp", sender="+7 900 000-00-19", subject=None, body="Срочно нужен кондиционер в детскую, до 70 тыс. Игорь"
)


def api_response(model: str = "claude-opus-5-5", stop_reason: str = "end_turn", text: str | None = None) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text if text is not None else json.dumps(EXTRACTION, ensure_ascii=False)}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 300,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        },
    }


def make_engine(company, handler, model: str = "claude-opus-5-5") -> ClaudeEngine:
    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeEngine(company, model=model, effort="low", client=client)


def test_request_shape_and_parsed_result(company):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx2.Response(200, json=api_response())

    extraction, meta = make_engine(company, handler).analyze(MESSAGE)

    request = captured[0]
    body = json.loads(request.content)
    assert request.url.path.endswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in request.headers["anthropic-beta"]
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    schema = body["output_config"]["format"]["schema"]
    assert set(schema["required"]) == set(EXTRACTION)
    assert schema["additionalProperties"] is False
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Прохлада-Сервис" in body["system"][0]["text"]
    assert "Срочно нужен кондиционер" in body["messages"][0]["content"]
    assert "thinking" not in body and "temperature" not in body

    assert extraction.category.value == "lead"
    assert extraction.contact.name == "Игорь"
    assert meta.engine == "claude" and meta.model == "claude-opus-5-5"
    assert meta.input_tokens == 1200 and meta.output_tokens == 200
    assert meta.cost_usd == pytest.approx((300 * 4 + 200 * 20 + 900 * 0.2) / 1_000_000)


def test_model_without_server_fallback_uses_plain_endpoint(company):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx2.Response(200, json=api_response(model="claude-haiku-5-5"))

    _, meta = make_engine(company, handler, model="claude-haiku-5-5").analyze(MESSAGE)
    body = json.loads(captured[0].content)
    assert "fallbacks" not in body
    assert FALLBACK_BETA not in captured[0].headers.get("anthropic-beta", "")
    assert meta.model == "claude-haiku-5-5"


def test_fallback_model_is_reported(company):
    engine = make_engine(company, lambda request: httpx2.Response(200, json=api_response(model="claude-opus-4-8")))
    _, meta = engine.analyze(MESSAGE)
    assert meta.model == "claude-opus-4-8"
    assert "запасная модель" in meta.note
    assert meta.cost_usd is None  # цены этой модели нет в таблице, стоимость не выдумываем


@pytest.mark.parametrize(
    "stop_reason, text, expected",
    [
        ("refusal", "", "не соответствует схеме"),  # пустой ответ при отказе: parse() не проходит схему
        ("refusal", None, "отказалась"),  # отказ с формально корректным JSON
        ("max_tokens", '{"category": "le', "не соответствует схеме"),  # ответ оборван на полуслове
        ("max_tokens", None, "обрезан"),
    ],
)
def test_refusal_and_truncation_raise_engine_error(company, stop_reason, text, expected):
    engine = make_engine(company, lambda request: httpx2.Response(200, json=api_response(stop_reason=stop_reason, text=text)))
    with pytest.raises(EngineError, match=expected):
        engine.analyze(MESSAGE)


@pytest.mark.parametrize(
    "status, error_type, expected",
    [
        (401, "authentication_error", "ключ API не принят"),
        (429, "rate_limit_error", "лимит запросов"),
        (400, "invalid_request_error", "запрос отклонён"),
        (529, "overloaded_error", "ошибка API 529"),
    ],
)
def test_api_errors_become_engine_errors(company, status, error_type, expected):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": error_type, "message": "boom"}})

    with pytest.raises(EngineError, match=expected):
        make_engine(company, handler).analyze(MESSAGE)


def test_connection_error_becomes_engine_error(company):
    def handler(request):
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(EngineError, match="нет связи"):
        make_engine(company, handler).analyze(MESSAGE)


def test_cost_is_not_invented_for_unknown_models():
    assert estimate_cost("some-future-model", object()) is None
