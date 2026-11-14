import json

import httpx

from app.integrations import Notifier, bitrix24_lead_payload
from app.service import InboxService, to_message
from tests.conftest import make_settings


def service_with(tmp_path, handler, **integration_settings) -> InboxService:
    settings = make_settings(tmp_path / "test.db", **integration_settings)
    notifier = Notifier(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    return InboxService(settings, notifier=notifier)


def test_bitrix24_payload(tmp_path, dev_items):
    service = service_with(tmp_path, lambda request: httpx.Response(200, json={}))
    card = service.ingest(to_message(next(item for item in dev_items if item["id"] == "dev-24")))
    fields = bitrix24_lead_payload(card)["fields"]
    assert fields["TITLE"].startswith("Заявка: ")
    assert fields["NAME"] == "Антон"
    assert fields["COMPANY_TITLE"] == "ООО «Логистик-Про»"
    assert fields["PHONE"] == [{"VALUE": "+79000000034", "VALUE_TYPE": "WORK"}]
    assert fields["EMAIL"] == [{"VALUE": "admin@example.org", "VALUE_TYPE": "WORK"}]
    assert fields["OPPORTUNITY"] == 300_000 and fields["CURRENCY_ID"] == "RUB"
    assert fields["SOURCE_ID"] == "EMAIL"


def test_hot_lead_goes_to_all_enabled_systems(tmp_path, dev_items):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": 1})

    service = service_with(
        tmp_path,
        handler,
        outgoing_webhook_url="https://hooks.example.test/inbox",
        bitrix24_webhook_url="https://portal.example.test/rest/1/secret/",
        telegram_bot_token="TEST_TOKEN",
        telegram_chat_id="-100500",
    )
    card = service.ingest(to_message(next(item for item in dev_items if item["id"] == "dev-24")))
    service.notify(card["id"])

    urls = [str(request.url) for request in requests]
    assert urls == [
        "https://hooks.example.test/inbox",
        "https://portal.example.test/rest/1/secret/crm.lead.add.json",
        "https://api.telegram.org/botTEST_TOKEN/sendMessage",
    ]
    telegram = json.loads(requests[2].content)
    assert telegram["chat_id"] == "-100500"
    assert "оценка 100/100" in telegram["text"] and "+79000000034" in telegram["text"]
    kinds = [event["kind"] for event in service.card(card["id"])["events"]]
    assert kinds.count("integration_ok") == 3


def test_non_lead_skips_crm_and_failures_are_logged(tmp_path, dev_items):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    service = service_with(
        tmp_path,
        handler,
        outgoing_webhook_url="https://hooks.example.test/inbox",
        bitrix24_webhook_url="https://portal.example.test/rest/1/secret",
        telegram_bot_token="TEST_TOKEN",
        telegram_chat_id="-100500",
    )
    card = service.ingest(to_message(next(item for item in dev_items if item["id"] == "dev-10")))  # вопрос по сервису
    service.notify(card["id"])

    assert [str(request.url) for request in requests] == ["https://hooks.example.test/inbox"]
    events = service.card(card["id"])["events"]
    assert events[-1]["kind"] == "integration_failed"
    assert "500" in events[-1]["detail"]
    assert service.card(card["id"])["status"] == "new"  # сбой интеграции не ломает обработку


def test_nothing_is_sent_when_integrations_are_off(tmp_path, dev_items):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("запросов быть не должно")

    service = service_with(tmp_path, handler)
    card = service.ingest(to_message(dev_items[0]))
    service.notify(card["id"])
    assert service.notifier.enabled == []
