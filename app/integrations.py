"""Исходящие интеграции: универсальный вебхук, Битрикс24 и уведомление в Telegram.

Каждая включается своей переменной окружения и не влияет на остальные. Сбой интеграции
не ломает обработку: он записывается в журнал карточки.
"""

import httpx

from .config import Settings
from .schemas import CATEGORY_LABELS

BITRIX_SOURCES = {"site_form": "WEB", "email": "EMAIL"}


def bitrix24_lead_payload(card: dict) -> dict:
    """Тело запроса crm.lead.add для входящего вебхука Битрикс24."""
    extraction = card["analysis"]["extraction"]
    contact = extraction["contact"]
    score = card["analysis"]["score"]
    fields: dict = {
        "TITLE": f"{CATEGORY_LABELS[card['category']]}: {extraction['summary'][:120]}",
        "NAME": contact["name"] or "",
        "COMPANY_TITLE": contact["company"] or "",
        "COMMENTS": card["body"],
        "SOURCE_ID": BITRIX_SOURCES.get(card["channel"], "OTHER"),
        "SOURCE_DESCRIPTION": f"Оценка лида {score['value']}/100. " + "; ".join(score["reasons"]),
    }
    if contact["phone"]:
        fields["PHONE"] = [{"VALUE": contact["phone"], "VALUE_TYPE": "WORK"}]
    if contact["email"]:
        fields["EMAIL"] = [{"VALUE": contact["email"], "VALUE_TYPE": "WORK"}]
    if extraction["budget_rub"]:
        fields["OPPORTUNITY"] = extraction["budget_rub"]
        fields["CURRENCY_ID"] = "RUB"
    return {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}


def telegram_alert_text(card: dict) -> str:
    extraction = card["analysis"]["extraction"]
    contact = extraction["contact"]
    lines = [
        f"🔥 {CATEGORY_LABELS[card['category']]} · оценка {card['lead_score']}/100",
        extraction["summary"],
    ]
    who = ", ".join(part for part in (contact["name"], contact["phone"], contact["email"]) if part)
    if who:
        lines.append(f"Контакт: {who}")
    if extraction["budget_rub"]:
        lines.append(f"Бюджет: {extraction['budget_rub']:,} ₽".replace(",", " "))
    lines.append(f"Ответственный: {card['assignee'] or card['queue']}")
    return "\n".join(lines)


class Notifier:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=5.0)

    @property
    def enabled(self) -> list[str]:
        names = []
        if self.settings.outgoing_webhook_url:
            names.append("webhook")
        if self.settings.bitrix24_webhook_url:
            names.append("bitrix24")
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            names.append("telegram")
        return names

    def dispatch(self, card: dict) -> list[tuple[str, str]]:
        """Отправляет карточку во включённые системы. Возвращает события для журнала."""
        events: list[tuple[str, str]] = []
        settings = self.settings

        if settings.outgoing_webhook_url:
            events.append(self._post("Вебхук", settings.outgoing_webhook_url, card))

        if settings.bitrix24_webhook_url and card["category"] == "lead":
            url = settings.bitrix24_webhook_url.rstrip("/") + "/crm.lead.add.json"
            events.append(self._post("Битрикс24", url, bitrix24_lead_payload(card)))

        if settings.telegram_bot_token and settings.telegram_chat_id and card["priority"] == "hot":
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            payload = {"chat_id": settings.telegram_chat_id, "text": telegram_alert_text(card)}
            events.append(self._post("Telegram", url, payload))
        return events

    def _post(self, name: str, url: str, payload: dict) -> tuple[str, str]:
        try:
            response = self.client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return "integration_failed", f"{name}: сервер ответил {exc.response.status_code}"
        except httpx.HTTPError as exc:
            return "integration_failed", f"{name}: нет связи ({type(exc).__name__})"
        return "integration_ok", f"{name}: карточка передана"
