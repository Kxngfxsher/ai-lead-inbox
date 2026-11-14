"""Маршрутизация: кто отвечает и за какое время. Таблица очередей лежит в data/company.json."""

from .schemas import Extraction, Routing, Score


def route(extraction: Extraction, score: Score, company: dict) -> Routing:
    category = extraction.category.value
    rule = company["queues"].get(category) or company["queues"]["other"]
    if rule.get("auto_archive"):
        return Routing(queue=rule["queue"], assignee=None, sla_minutes=None, priority="low", auto_archive=True)

    sla = rule["sla_minutes"]
    priority = "normal"
    if category == "lead" and score.value >= company["hot_lead_score"]:
        priority = "hot"
        sla = min(sla, company["hot_lead_sla_minutes"])
    elif category == "complaint" and extraction.urgency == "high":
        priority = "hot"
        sla = max(5, sla // 2)
    elif extraction.urgency == "high":
        sla = max(5, sla // 2)
    elif extraction.urgency == "low":
        priority = "low"
    return Routing(queue=rule["queue"], assignee=rule.get("assignee"), sla_minutes=sla, priority=priority)
