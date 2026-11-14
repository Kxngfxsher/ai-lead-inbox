"""Конвейер обработки: движок → сверка фактов → оценка лида → маршрут.

Если LLM недоступна, обращение не теряется: его разбирают правила, а в карточке остаётся пометка.
"""

from . import textutils as tu
from .engines import Engine, EngineError
from .routing import route
from .schemas import Analysis, Category, Extraction, IncomingMessage
from .scoring import score_lead

REVIEW_THRESHOLD = 0.6  # ниже этой уверенности обращение помечается «на проверку»


def crosscheck(extraction: Extraction, msg: IncomingMessage) -> tuple[Extraction, list[str]]:
    """Детерминированная сверка ответа движка с текстом обращения.

    Контакты, которых нет в обращении, удаляются: оператор не должен звонить по выдуманному номеру.
    Возвращает исправленные факты и список сделанных правок (он виден оператору).
    """
    text = f"{msg.subject or ''}\n{msg.body}"
    checks: list[str] = []
    contact = extraction.contact.model_copy()

    # Телефон: оставляем только номер, цифры которого реально встречаются в обращении.
    known_digits = tu.digits_only(f"{text} {msg.sender}")
    if contact.phone:
        tail = tu.digits_only(contact.phone)[-10:]
        if len(tail) >= 7 and tail in known_digits:
            contact.phone = tu.normalize_phone(contact.phone) or contact.phone.strip()
        else:
            contact.phone = None
            checks.append("Телефон из ответа модели не найден в обращении и удалён")
    if not contact.phone:
        text_phones = tu.find_phones(text)
        sender_phones = tu.find_phones(msg.sender)
        if text_phones:
            contact.phone = text_phones[0]
            checks.append("Телефон добавлен по шаблону номера")
        elif sender_phones:
            contact.phone = sender_phones[0]
            checks.append("Телефон взят из номера отправителя")

    # E-mail: тот же принцип.
    text_emails = tu.find_emails(text)
    sender_emails = tu.find_emails(msg.sender)
    if contact.email:
        email = contact.email.strip().lower()
        if email in text_emails or email in sender_emails:
            contact.email = email
        else:
            contact.email = None
            checks.append("E-mail из ответа модели не найден в обращении и удалён")
    if not contact.email:
        if text_emails:
            contact.email = text_emails[0]
            checks.append("E-mail добавлен по шаблону адреса")
        elif sender_emails:
            contact.email = sender_emails[0]

    contact.name = (contact.name or "").strip() or None
    contact.company = (contact.company or "").strip() or None

    budget = extraction.budget_rub
    if budget is not None and (extraction.category != Category.lead or budget < 1000):
        budget = None

    updated = extraction.model_copy(
        update={
            "contact": contact,
            "budget_rub": budget,
            "summary": extraction.summary.strip() or tu.summarize(msg.body),
            "interest": (extraction.interest or "").strip() or None,
            "deadline": (extraction.deadline or "").strip() or None,
            "suggested_reply": "" if extraction.category == Category.spam else extraction.suggested_reply.strip(),
            "confidence": round(min(1.0, max(0.0, extraction.confidence)), 2),
        }
    )
    return updated, checks


class Pipeline:
    def __init__(self, engine: Engine, fallback: Engine, company: dict):
        self.engine = engine
        self.fallback = fallback
        self.company = company

    def run(self, msg: IncomingMessage, force_rules: bool = False) -> Analysis:
        engine = self.fallback if force_rules else self.engine
        try:
            extraction, meta = engine.analyze(msg)
        except EngineError as exc:
            extraction, meta = self.fallback.analyze(msg)
            meta.note = f"LLM недоступна ({exc}); обращение разобрано правилами"
        return self.finalize(extraction, msg, meta)

    def finalize(self, extraction: Extraction, msg: IncomingMessage, meta, checks: list[str] | None = None) -> Analysis:
        """Сверка, оценка и маршрут. Вызывается и после правки категории оператором."""
        extraction, new_checks = crosscheck(extraction, msg)
        score = score_lead(extraction)
        return Analysis(
            extraction=extraction,
            score=score,
            routing=route(extraction, score, self.company),
            meta=meta,
            needs_review=extraction.confidence < REVIEW_THRESHOLD or extraction.category == Category.other,
            checks=new_checks if checks is None else checks,
        )
