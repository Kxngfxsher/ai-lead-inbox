"""Детерминированные помощники: телефоны, e-mail, суммы, имена, компании.

Используются дважды: движком правил и как перепроверка ответа LLM. Регулярное выражение
не «придумывает» телефон, поэтому контакты из ответа модели всегда сверяются с текстом.
"""

import re

_SPACES = "   "

PHONE_RE = re.compile(r"(?<![\d+])(?:\+?7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def normalize(text: str) -> str:
    """Нижний регистр и ё→е, чтобы ключевые слова не зависели от написания."""
    return text.lower().replace("ё", "е")


def digits_only(text: str) -> str:
    return re.sub(r"\D", "", text)


def normalize_phone(raw: str | None) -> str | None:
    """Приводит российский номер к виду +7XXXXXXXXXX; для непохожих строк возвращает None."""
    if not raw:
        return None
    digits = digits_only(raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


def find_phones(text: str) -> list[str]:
    found: list[str] = []
    for match in PHONE_RE.finditer(text):
        phone = normalize_phone(match.group())
        if phone and phone not in found:
            found.append(phone)
    return found


def find_emails(text: str) -> list[str]:
    found: list[str] = []
    for match in EMAIL_RE.finditer(text):
        email = match.group().rstrip(".").lower()
        if email not in found:
            found.append(email)
    return found


# --- Суммы -------------------------------------------------------------------------------------

_NUM = rf"\d{{1,3}}(?:[{_SPACES}]\d{{3}})+|\d+(?:[.,]\d+)?"
_BIG_UNIT = r"млн|миллион\w*|тыс\w*|т\.\s?р\.?|тр\b"
_UNIT = rf"{_BIG_UNIT}|руб\w*|р\.|₽"
_AMOUNT_RE = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?", re.IGNORECASE)
_RANGE_RE = re.compile(
    rf"(?P<a>\d+(?:[.,]\d+)?)\s*[–—-]\s*(?P<b>\d+(?:[.,]\d+)?)\s*(?P<unit>{_BIG_UNIT})", re.IGNORECASE
)
_BUDGET_CUE_RE = re.compile(r"бюджет\w*|цена вопроса|в пределах|не дороже|уложиться в", re.IGNORECASE)
_UPTO_RE = re.compile(rf"\bдо\s+(?P<num>{_NUM})\s*(?P<unit>{_UNIT})", re.IGNORECASE)


def _to_rub(num: str, unit: str | None) -> int | None:
    cleaned = re.sub(rf"[{_SPACES}]", "", num).replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    unit = (unit or "").lower()
    if unit.startswith(("млн", "миллион")):
        value *= 1_000_000
    elif unit.startswith(("тыс", "т.", "тр")):
        value *= 1_000
    return int(round(value))


def _first_amount(window: str) -> int | None:
    """Первая сумма в окне текста. Диапазон «200–250 тысяч» читается как верхняя граница."""
    range_match = _RANGE_RE.search(window)
    range_pos = range_match.start() if range_match else None
    range_value = _to_rub(range_match.group("b"), range_match.group("unit")) if range_match else None
    for match in _AMOUNT_RE.finditer(window):
        value = _to_rub(match.group("num"), match.group("unit"))
        if value is None or (not match.group("unit") and value < 1000):
            continue
        if range_pos is None or match.start() < range_pos:
            return value
        break
    return range_value


def parse_budget(text: str) -> int | None:
    """Бюджет в рублях, если клиент его назвал («бюджет до 55 000 руб.», «до 70 тыс.»)."""
    cleaned = EMAIL_RE.sub(" ", PHONE_RE.sub(" ", text))
    values: list[int] = []
    for cue in _BUDGET_CUE_RE.finditer(cleaned):
        value = _first_amount(cleaned[cue.end() : cue.end() + 70])
        if value:
            values.append(value)
    if not values:
        for match in _UPTO_RE.finditer(cleaned):
            value = _to_rub(match.group("num"), match.group("unit"))
            if value:
                values.append(value)
    values = [value for value in values if value >= 1000]
    return max(values) if values else None


# --- Имена и компании --------------------------------------------------------------------------

FIRST_NAMES = frozenset(
    """
    Александр Алексей Анатолий Андрей Антон Аркадий Арсений Артём Артем Артур Богдан Борис Вадим
    Валентин Валерий Василий Виктор Виталий Владимир Владислав Вячеслав Геннадий Георгий Глеб
    Григорий Даниил Денис Дмитрий Евгений Егор Иван Игорь Илья Кирилл Константин Лев Леонид
    Максим Марат Марк Матвей Михаил Никита Николай Олег Павел Пётр Петр Роман Руслан Семён Семен
    Сергей Станислав Степан Тимофей Тимур Фёдор Федор Эдуард Юрий Ярослав
    Александра Алёна Алена Алина Алиса Алла Анастасия Анжела Анна Антонина Валентина Валерия
    Варвара Вера Вероника Виктория Галина Дарья Диана Ева Евгения Екатерина Елена Елизавета Жанна
    Зинаида Зоя Инна Ирина Карина Кира Кристина Ксения Лариса Лидия Лилия Любовь Людмила
    Маргарита Марина Мария Милана Надежда Наталья Наталия Нина Оксана Олеся Ольга Полина Раиса
    Регина Светлана Софья София Тамара Татьяна Ульяна Юлия Яна
    """.split()
)
_NAME_RE = re.compile(r"(?<![А-Яа-яЁё])([А-ЯЁ][а-яё]+)(?: ([А-ЯЁ][а-яё]+))?")
_NAME_STOP = frozenset({"Тел", "Телефон", "Моб", "Почта", "Спасибо"})


def find_person_name(text: str) -> str | None:
    """Имя отправителя по словарю имён. Берём последнее вхождение: подпись обычно в конце."""
    result: str | None = None
    for match in _NAME_RE.finditer(text):
        first, second = match.group(1), match.group(2)
        if first in FIRST_NAMES:
            result = f"{first} {second}" if second and second not in _NAME_STOP else first
        elif second in FIRST_NAMES:
            result = second
    return result


_QUOTED = r"[«\"“][^»\"”\n]{2,60}[»\"”]"
_COMPANY_RE = re.compile(
    rf"(?<![А-Яа-яЁё])(?:ООО|АО|ПАО|ЗАО)\s+{_QUOTED}"
    r"|(?<![А-Яа-яЁё])ИП\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.)?"
    rf"|(?:[Кк]линика|[Кк]омпания|[Фф]ирма|[Мм]агазин|[Сс]алон|[Сс]тудия|[Оо]тель|[Рр]есторан|[Кк]афе)\s+{_QUOTED}"
)


def find_company(text: str, own_name: str = "") -> str | None:
    """Первая названная организация, кроме нашей собственной."""
    own = normalize(own_name)
    for match in _COMPANY_RE.finditer(text):
        candidate = " ".join(match.group().split())
        if own and own in normalize(candidate):
            continue
        return candidate
    return None


# --- Краткое изложение -------------------------------------------------------------------------

_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|здрасте|добрый день|добрый вечер|доброе утро|привет"
    r"|коллеги, добрый день|уважаем\w+\s+\w+)[\s!.,]*",
    re.IGNORECASE,
)


def summarize(body: str, limit: int = 170) -> str:
    """Первое содержательное предложение без приветствия (для движка правил)."""
    flat = " ".join(body.split())
    text = _GREETING_RE.sub("", flat, count=1).strip() or flat
    for match in re.finditer(r"[.!?…]+(?=\s|$)", text):
        if match.end() >= 40:
            text = text[: match.end()]
            break
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text[:1].upper() + text[1:]
