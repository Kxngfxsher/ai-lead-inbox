"""Модели данных: входящее обращение, результат разбора и карточка для оператора."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Category(str, Enum):
    lead = "lead"
    support = "support"
    complaint = "complaint"
    billing = "billing"
    partnership = "partnership"
    hr = "hr"
    spam = "spam"
    other = "other"


CATEGORY_LABELS: dict[str, str] = {
    "lead": "Заявка",
    "support": "Сервис",
    "complaint": "Жалоба",
    "billing": "Документы и оплата",
    "partnership": "Партнёры и поставщики",
    "hr": "Соискатель",
    "spam": "Спам",
    "other": "Другое",
}

CHANNEL_LABELS: dict[str, str] = {
    "email": "e-mail",
    "site_form": "форма на сайте",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
}

Urgency = Literal["high", "normal", "low"]
Channel = Literal["email", "site_form", "telegram", "whatsapp"]


class IncomingMessage(BaseModel):
    """Обращение в том виде, в каком его присылает канал (почта, форма, мессенджер)."""

    channel: Channel = "email"
    sender: str = Field(min_length=1, max_length=200)
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=20000)


class Contact(BaseModel):
    name: str | None = Field(description="Имя отправителя, если он представился; иначе null")
    phone: str | None = Field(description="Телефон отправителя так, как он написан в тексте; иначе null")
    email: str | None = Field(description="E-mail отправителя, если указан; иначе null")
    company: str | None = Field(description="Компания отправителя, если названа; иначе null")


class Extraction(BaseModel):
    """Факты об обращении. Эту схему возвращают оба движка: правила и Claude.

    Все поля обязательные (без значений по умолчанию): так схема подходит для structured outputs.
    """

    category: Category = Field(description="Категория обращения")
    urgency: Urgency = Field(description="Срочность реакции")
    summary: str = Field(description="Суть обращения одним предложением, без приветствий")
    contact: Contact
    interest: str | None = Field(description="Что нужно клиенту: товар, услуга, параметры объекта; иначе null")
    budget_rub: int | None = Field(description="Верхняя граница бюджета в рублях, если клиент её назвал; иначе null")
    deadline: str | None = Field(description="Срок словами клиента, если он назван; иначе null")
    suggested_reply: str = Field(description="Черновик ответа клиенту; для спама пустая строка")
    confidence: float = Field(description="Уверенность в категории от 0 до 1")


class Score(BaseModel):
    value: int
    reasons: list[str]


class Routing(BaseModel):
    queue: str
    assignee: str | None
    sla_minutes: int | None
    priority: Literal["hot", "normal", "low"]
    auto_archive: bool = False


class EngineMeta(BaseModel):
    engine: Literal["rules", "claude"]
    model: str | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    note: str | None = None


class Analysis(BaseModel):
    """Полный результат конвейера: факты + оценка лида + маршрут + служебная информация."""

    extraction: Extraction
    score: Score
    routing: Routing
    meta: EngineMeta
    needs_review: bool
    checks: list[str] = []
