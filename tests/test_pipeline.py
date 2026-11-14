from app.engines import EngineError
from app.engines.rules import RulesEngine
from app.pipeline import Pipeline, crosscheck
from app.schemas import Contact, EngineMeta, Extraction, IncomingMessage
from app.service import to_message


def extraction(**overrides) -> Extraction:
    values = dict(
        category="lead",
        urgency="normal",
        summary="Нужен кондиционер",
        contact=Contact(name=None, phone=None, email=None, company=None),
        interest="кондиционер",
        budget_rub=None,
        deadline=None,
        suggested_reply="Здравствуйте!",
        confidence=0.9,
    )
    values.update(overrides)
    return Extraction(**values)


def message(body: str, sender: str = "client@example.com", channel: str = "email") -> IncomingMessage:
    return IncomingMessage(channel=channel, sender=sender, subject=None, body=body)


def test_rules_engine_passes_dev_set(company, dev_items):
    """Регрессия: правила обязаны верно классифицировать набор, на котором настраивались."""
    engine = RulesEngine(company)
    wrong = []
    for item in dev_items:
        result, _ = engine.analyze(to_message(item))
        if result.category.value != item["expected"]["category"]:
            wrong.append((item["id"], result.category.value))
    assert wrong == []


def test_spam_gets_no_reply_and_complaint_gets_apology(company, dev_items):
    engine = RulesEngine(company)
    by_id = {item["id"]: item for item in dev_items}
    spam, _ = engine.analyze(to_message(by_id["dev-05"]))
    complaint, _ = engine.analyze(to_message(by_id["dev-04"]))
    assert spam.suggested_reply == ""
    assert "извинения" in complaint.suggested_reply
    assert complaint.suggested_reply.startswith("Здравствуйте, Елена Викторовна!")


def test_crosscheck_removes_phone_that_is_not_in_text():
    msg = message("Нужен кондиционер, перезвоните.")
    fixed, checks = crosscheck(extraction(contact=Contact(name=None, phone="+7 999 123-45-67", email=None, company=None)), msg)
    assert fixed.contact.phone is None
    assert any("удалён" in check for check in checks)


def test_crosscheck_normalizes_real_phone_and_adds_missing_one():
    msg = message("Нужен кондиционер. Звоните: 8 (900) 000-00-11")
    kept, _ = crosscheck(extraction(contact=Contact(name=None, phone="8 (900) 000-00-11", email=None, company=None)), msg)
    assert kept.contact.phone == "+79000000011"
    added, checks = crosscheck(extraction(), msg)
    assert added.contact.phone == "+79000000011"
    assert checks == ["Телефон добавлен по шаблону номера"]


def test_crosscheck_uses_sender_contacts():
    whatsapp = message("кондей нужен", sender="+7 900 000-00-41", channel="whatsapp")
    fixed, _ = crosscheck(extraction(), whatsapp)
    assert fixed.contact.phone == "+79000000041"
    email, _ = crosscheck(extraction(), message("Пришлите расчёт"))
    assert email.contact.email == "client@example.com"


def test_crosscheck_drops_invented_email_and_non_lead_budget():
    msg = message("Пришлите акт сверки")
    fixed, checks = crosscheck(
        extraction(
            category="billing",
            budget_rub=500_000,
            confidence=1.7,
            contact=Contact(name=None, phone=None, email="boss@invented.example", company=None),
        ),
        msg,
    )
    assert fixed.contact.email == "client@example.com"  # выдуманный адрес заменён адресом отправителя
    assert any("E-mail" in check for check in checks)
    assert fixed.budget_rub is None
    assert fixed.confidence == 1.0


def test_hot_lead_scoring_and_routing(company, dev_items):
    pipeline = Pipeline(RulesEngine(company), RulesEngine(company), company)
    by_id = {item["id"]: item for item in dev_items}
    hot = pipeline.run(to_message(by_id["dev-24"]))
    assert hot.score.value == 100
    assert hot.routing.priority == "hot"
    assert hot.routing.sla_minutes == company["hot_lead_sla_minutes"]
    cold = pipeline.run(to_message(by_id["dev-17"]))
    assert cold.score.value < company["hot_lead_score"]
    assert cold.routing.priority == "normal"


def test_spam_is_archived_and_urgent_complaint_is_hot(company, dev_items):
    pipeline = Pipeline(RulesEngine(company), RulesEngine(company), company)
    by_id = {item["id"]: item for item in dev_items}
    spam = pipeline.run(to_message(by_id["dev-15"]))
    assert spam.routing.auto_archive and spam.score.value == 0
    complaint = pipeline.run(to_message(by_id["dev-04"]))
    assert complaint.routing.priority == "hot"
    assert complaint.routing.sla_minutes == 15


def test_unknown_message_goes_to_review(company):
    pipeline = Pipeline(RulesEngine(company), RulesEngine(company), company)
    analysis = pipeline.run(message("Передайте привет Петровичу со склада"))
    assert analysis.extraction.category.value == "other"
    assert analysis.needs_review


def test_pipeline_falls_back_to_rules_when_llm_fails(company):
    class BrokenEngine:
        name = "claude"

        def analyze(self, msg):
            raise EngineError("нет связи с API")

    pipeline = Pipeline(BrokenEngine(), RulesEngine(company), company)
    analysis = pipeline.run(message("Сколько стоит установка кондиционера? 8 900 000 00 77"))
    assert analysis.meta.engine == "rules"
    assert "нет связи с API" in analysis.meta.note
    assert analysis.extraction.category.value == "lead"


def test_force_rules_skips_llm(company):
    class ExplodingEngine:
        name = "claude"

        def analyze(self, msg):
            raise AssertionError("LLM не должна вызываться")

    pipeline = Pipeline(ExplodingEngine(), RulesEngine(company), company)
    analysis = pipeline.run(message("Сколько стоит монтаж?"), force_rules=True)
    assert isinstance(analysis.meta, EngineMeta) and analysis.meta.engine == "rules"
