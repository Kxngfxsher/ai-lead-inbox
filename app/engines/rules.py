"""Движок правил: ключевые слова и регулярные выражения. Работает без интернета и API-ключей.

Это честный базовый уровень: он бесплатен и предсказуем, но понимает только те формулировки,
которые в нём прописаны. Насколько он уступает LLM, показывает scripts/evaluate.py.
"""

import re
import time

from .. import textutils as tu
from ..schemas import Contact, EngineMeta, Extraction, IncomingMessage

# Ключевые слова записаны в нижнем регистре и через «е» (текст нормализуется так же).
KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "spam": [
        ("продвижени", 2), ("раскрутк", 3), ("топ-10", 3), ("в топ", 2), ("seo", 2),
        ("одобрен кредит", 4), ("кредит без", 3), ("без залога", 3), ("базу клиентов", 4),
        ("база клиентов", 4), ("холодных звонков", 2), ("рассылк", 2), ("заработ", 3),
        ("зарабатыва", 3), ("перейдите по ссылке", 3), ("жмите", 2), ("только до конца", 2),
        ("скидка 50", 2), ("количество предложений ограничено", 3), ("казино", 4), ("выигра", 3),
        ("не выходя из дома", 3),
    ],
    "complaint": [
        ("жалоб", 4), ("претензи", 4), ("требую", 3), ("роспотребнадзор", 4), ("в суд", 3),
        ("возврат денег", 3), ("верните деньги", 3), ("безобрази", 3), ("возмущен", 3), ("обман", 3),
        ("ужасн", 2), ("отвратительн", 3), ("хамств", 3), ("течет", 2), ("испорчен", 2),
        ("третий раз", 3), ("никто не перезвонил", 3), ("никто не звонил", 2), ("до сих пор", 2),
        ("не приехал", 3), ("должен был приехать", 2), ("некачествен", 3), ("компенсир", 3),
    ],
    "billing": [
        ("акт сверки", 4), ("акта сверки", 4), ("закрывающие документы", 4), ("счет-фактур", 4),
        ("упд", 3), ("перевыставить счет", 4), ("выставить счет", 3), ("выставьте счет", 3),
        ("платежное поручение", 3), ("платежк", 2), ("реквизит", 3), ("оплатили счет", 3),
        ("поступление оплаты", 3), ("бухгалтер", 3), ("эдо", 2), ("взаиморасчет", 3),
        ("по счету №", 1),
    ],
    "support": [
        ("по заказу", 2), ("статус заказа", 4), ("номер заказа", 2), ("заказ №", 3), ("заказа №", 3),
        ("договор №", 2), ("перенести", 3), ("гаранти", 2), ("на пульте", 2), ("как включить", 3),
        ("как настроить", 3), ("как вызвать мастера", 3), ("режим", 1), ("инструкци", 2),
        ("купили у вас", 3), ("вы ставили", 2), ("устанавливали", 1), ("диагностик", 2),
        ("начал шуметь", 2), ("подскажите", 1),
    ],
    "partnership": [
        ("предлагаем вам", 3), ("предлагаем", 2), ("сотрудничеств", 3), ("оптовые поставки", 3),
        ("оптов", 2), ("дилерск", 3), ("импортер", 2), ("прайс-лист", 2), ("наша компания", 2),
        ("партнер", 3), ("мы —", 1), ("мы - ", 1), ("отсрочк", 2), ("подрядчик", 1),
        ("условия сотрудничества", 2),
    ],
    "hr": [
        ("резюме", 4), ("ваканс", 4), ("ищу работу", 4), ("опыт работы", 3), ("стажировк", 3),
        ("соискател", 3), ("трудоустро", 3), ("готов выйти", 2), ("зарплат", 3), ("график работы", 2),
    ],
    "lead": [
        ("нужен кондиционер", 3), ("нужен ", 1), ("нужна ", 1), ("нужно ", 1), ("нужны ", 1),
        ("хочу купить", 3), ("хотим купить", 3), ("хотел бы заказать", 3), ("хотим заказать", 3),
        ("сколько стоит", 3), ("сколько примерно стоит", 3), ("стоимост", 2), ("цена ", 1),
        ("цены", 1), ("расчет", 2), ("рассчита", 2), ("просим подготовить", 2),
        ("подготовить коммерческое предложение", 3), ("коммерческое предложение", 1), ("замер", 3),
        ("бюджет", 3), ("интересует", 2), ("подобрать", 2), ("подберите", 2), ("в наличии", 2),
        ("установк", 1), ("монтаж", 1), ("под ключ", 2), ("есть ли у вас", 1), ("присматрива", 1),
        ("рассматриваю", 1),
    ],
}
# При равенстве баллов выигрывает категория, где ошибка дороже.
TIE_ORDER = ["complaint", "billing", "support", "hr", "partnership", "spam", "lead"]

URGENT_TERMS = [
    "срочн", "немедленно", "как можно скорее", "сегодня-завтра", "течет", "заливает", "авари",
    "вышел из строя", "третий раз", "требую", "роспотребнадзор",
]
LOW_TERMS = ["не срочно", "несрочно", "на будущее", "присматрива", "в следующем году", "не горит"]
LOW_BY_DEFAULT = {"spam", "partnership", "hr", "other"}

INTEREST_TERMS = [
    ("тепловой насос", "тепловой насос"), ("вентиляц", "вентиляция"), ("вытяжк", "вытяжка"),
    ("мульти-сплит", "мульти-сплит"), ("сплит", "сплит-система"), ("кондиционер", "кондиционер"),
    ("кондей", "кондиционер"), ("обслуживан", "обслуживание"), ("чистк", "чистка"),
    ("заправк", "заправка"), ("монтаж", "монтаж"), ("установк", "монтаж"), ("замер", "выезд на замер"),
]
_AREA_RE = re.compile(r"(\d{2,4})\s*(?:м2|м²|кв\.?\s?м\b|квадрат\w*)", re.IGNORECASE)
_DEADLINE_RE = re.compile(
    r"сегодня-завтра|сегодня|послезавтра|завтра|на этой неделе|на следующей неделе"
    r"|до (?:понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья)"
    r"|до конца (?:недели|месяца|года)"
    r"|до \d{1,2} (?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"|через [а-я\s\d-]{0,25}?(?:дн[а-я]+|недел[а-я]+|месяц[а-я]*)"
    r"|в следующем году|как можно скорее"
)
_ORDER_RE = re.compile(r"(?:заказ[а-я]*|договор[а-я]*)\s*(?:№|#)?\s*(\d{3,6})")


def classify(normalized_text: str) -> tuple[str, float]:
    """Категория по сумме весов ключевых слов и уверенность по отрыву от второго места."""
    scores = {
        category: sum(weight for keyword, weight in keywords if keyword in normalized_text)
        for category, keywords in KEYWORDS.items()
    }
    best = max(TIE_ORDER, key=lambda category: (scores[category], -TIE_ORDER.index(category)))
    top = scores[best]
    if top == 0:
        return "other", 0.2
    second = max(score for category, score in scores.items() if category != best)
    confidence = min(0.95, 0.4 + 0.08 * (top - second) + 0.03 * top)
    return best, round(confidence, 2)


def detect_urgency(normalized_text: str, category: str) -> str:
    if any(term in normalized_text for term in LOW_TERMS) or category in LOW_BY_DEFAULT:
        return "low"
    if any(term in normalized_text for term in URGENT_TERMS):
        return "high"
    return "normal"


def detect_interest(normalized_text: str) -> str | None:
    hits: list[tuple[int, str]] = []
    for term, label in INTEREST_TERMS:
        position = normalized_text.find(term)
        if position >= 0 and label not in (existing for _, existing in hits):
            hits.append((position, label))
    if not hits:
        return None
    labels = [label for _, label in sorted(hits)[:3]]
    result = ", ".join(labels)
    area = _AREA_RE.search(normalized_text)
    if area:
        result += f" · {area.group(1)} м²"
    return result[:1].upper() + result[1:]


def detect_deadline(normalized_text: str) -> str | None:
    match = _DEADLINE_RE.search(normalized_text)
    return match.group() if match else None


class RulesEngine:
    name = "rules"

    def __init__(self, company: dict):
        self.company = company

    def analyze(self, msg: IncomingMessage) -> tuple[Extraction, EngineMeta]:
        started = time.perf_counter()
        text = f"{msg.subject or ''}\n{msg.body}"
        normalized = tu.normalize(text)

        category, confidence = classify(normalized)
        phones = tu.find_phones(text) or tu.find_phones(msg.sender)  # в мессенджерах номер — это отправитель
        emails = tu.find_emails(text) or tu.find_emails(msg.sender)
        contact = Contact(
            name=tu.find_person_name(msg.body),
            phone=phones[0] if phones else None,
            email=emails[0] if emails else None,
            company=tu.find_company(text, own_name=self.company["name"]),
        )
        interest = detect_interest(normalized) if category in ("lead", "support", "complaint") else None
        extraction = Extraction(
            category=category,
            urgency=detect_urgency(normalized, category),
            summary=tu.summarize(msg.body),
            contact=contact,
            interest=interest,
            budget_rub=tu.parse_budget(text) if category == "lead" else None,
            deadline=detect_deadline(normalized),
            suggested_reply=self.reply_template(category, contact, interest, normalized),
            confidence=confidence,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return extraction, EngineMeta(engine="rules", latency_ms=latency_ms)

    def reply_template(self, category: str, contact: Contact, interest: str | None, normalized: str) -> str:
        """Шаблонный черновик ответа. В режиме Claude ответ пишется под конкретное обращение."""
        if category == "spam":
            return ""
        first_name = contact.name.split()[0] if contact.name else None
        if contact.name and len(contact.name.split()) > 1 and contact.name.split()[1].endswith(("вна", "вич", "ична")):
            first_name = contact.name  # «Елена Викторовна» оставляем с отчеством
        hello = f"Здравствуйте, {first_name}!" if first_name else "Здравствуйте!"
        name = self.company["name"]
        by_phone = f"по номеру {contact.phone}" if contact.phone else "в ответном сообщении"
        order = _ORDER_RE.search(normalized)

        if category == "lead":
            about = f" Ваш запрос: {interest.lower()}." if interest else ""
            body = (
                f"Спасибо за обращение в «{name}».{about} Менеджер свяжется с вами {by_phone} "
                "в ближайшее время, уточнит детали и подготовит расчёт. Если удобно, напишите адрес "
                "объекта и время, когда вам можно позвонить."
            )
        elif category == "support":
            tail = (
                f"Обращение привязано к заказу №{order.group(1)}."
                if order
                else "Если у вас есть номер заказа или договора, пришлите его, пожалуйста: так мы ответим быстрее."
            )
            body = f"Спасибо, что написали. Мы передали вопрос в сервисную службу, специалист ответит в ближайшее время. {tail}"
        elif category == "complaint":
            body = (
                "Приносим извинения за сложившуюся ситуацию. Обращение передано руководителю сервисной "
                f"службы и взято на контроль. Мы свяжемся с вами {by_phone} сегодня, чтобы согласовать решение."
            )
        elif category == "billing":
            body = (
                "Ваш запрос передан в бухгалтерию. Подготовим документы и направим их вам в течение "
                "рабочего дня. Если нужны оригиналы, уточните, пожалуйста, способ доставки: ЭДО, почта или курьер."
            )
        elif category == "partnership":
            body = (
                "Благодарим за предложение. Мы передали его в отдел закупок; если оно окажется нам "
                "интересно, коллеги свяжутся с вами."
            )
        elif category == "hr":
            body = (
                f"Спасибо за интерес к работе в «{name}». Мы передали ваше сообщение в отдел персонала. "
                "Если ваш опыт подойдёт под открытые вакансии, с вами свяжутся."
            )
        else:
            body = "Спасибо за сообщение. Мы передали его ответственному сотруднику и вернёмся с ответом в ближайшее время."
        return f"{hello} {body}\n\n{self.company['signature']}"
