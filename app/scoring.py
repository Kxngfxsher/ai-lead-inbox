"""Оценка лида по прозрачной формуле.

Модель извлекает факты, а баллы начисляет код: руководитель видит, за что обращение получило
свою оценку, и может поменять веса под свой бизнес без переобучения чего-либо.
"""

from .schemas import Category, Extraction, Score

LARGE_DEAL_RUB = 150_000


def score_lead(extraction: Extraction) -> Score:
    if extraction.category != Category.lead:
        return Score(value=0, reasons=[])

    points = 0
    reasons: list[str] = []

    def add(delta: int, why: str) -> None:
        nonlocal points
        points += delta
        reasons.append(f"{'+' if delta > 0 else '−'}{abs(delta)} · {why}")

    contact = extraction.contact
    if contact.phone:
        add(25, "оставлен телефон")
    if contact.email:
        add(10, "есть e-mail")
    if extraction.budget_rub:
        add(20, "назван бюджет")
        if extraction.budget_rub >= LARGE_DEAL_RUB:
            add(10, "крупный чек")
    if extraction.interest:
        add(15, "понятна потребность")
    if extraction.deadline:
        add(10, "назван срок")
    if contact.company:
        add(10, "корпоративный клиент")
    if extraction.urgency == "high":
        add(10, "срочный запрос")
    elif extraction.urgency == "low":
        add(-15, "покупка не в ближайшее время")

    return Score(value=max(0, min(100, points)), reasons=reasons)
