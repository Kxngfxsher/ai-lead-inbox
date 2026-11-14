"""Сценарии работы с обращениями: приём, ответ, правка категории, статистика и демо-данные."""

import json
from datetime import datetime, timedelta

from . import textutils as tu
from .config import DATA_DIR, Settings, load_company
from .db import Store, iso, parse_iso, utcnow
from .engines import build_engine
from .engines.rules import RulesEngine
from .integrations import Notifier
from .pipeline import Pipeline
from .schemas import CATEGORY_LABELS, CHANNEL_LABELS, Analysis, Category, Contact, IncomingMessage

PRIORITY_LABELS = {"hot": "горячий", "normal": "обычный", "low": "низкий"}


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


def load_dataset(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def to_message(item: dict) -> IncomingMessage:
    return IncomingMessage(
        channel=item["channel"], sender=item["sender"], subject=item.get("subject"), body=item["body"]
    )


class InboxService:
    def __init__(self, settings: Settings, engine=None, notifier: Notifier | None = None):
        self.settings = settings
        self.company = load_company()
        self.store = Store(settings.db_path)
        self.rules = RulesEngine(self.company)
        self.engine_error: str | None = None
        if engine is None:
            engine = self.rules
            if settings.engine == "claude":
                try:
                    engine = build_engine("claude", self.company, settings.model, settings.effort)
                except Exception as exc:  # нет SDK или ключа: демо продолжает работать на правилах
                    self.engine_error = f"{type(exc).__name__}: {exc}"
        self.engine = engine
        self.pipeline = Pipeline(engine, self.rules, self.company)
        self.notifier = notifier or Notifier(settings)

    # --- чтение ------------------------------------------------------------------------------

    def card(self, message_id: int) -> dict:
        row = self.store.get(message_id)
        if row is None:
            raise NotFound(f"Обращение {message_id} не найдено")
        return self._decorate(row, with_events=True)

    def list(self, view: str = "all", query: str = "") -> list[dict]:
        rows = self.store.all()
        now = utcnow()
        filters = {
            "waiting": lambda r: r["status"] == "new",
            "hot": lambda r: r["status"] == "new" and r["priority"] == "hot",
            "review": lambda r: r["status"] == "new" and r["needs_review"],
            "overdue": lambda r: self._overdue(r, now),
            "answered": lambda r: r["status"] == "answered",
            "archived": lambda r: r["status"] == "archived",
        }
        if view in filters:
            rows = [row for row in rows if filters[view](row)]
        needle = tu.normalize(query.strip())
        if needle:
            rows = [
                row
                for row in rows
                if needle
                in tu.normalize(
                    " ".join(
                        [row["sender"], row["subject"] or "", row["body"], row["analysis"]["extraction"]["summary"]]
                    )
                )
            ]
        return [self._decorate(row) for row in rows]

    def stats(self) -> dict:
        rows = self.store.all()
        now = utcnow()
        answered = [row for row in rows if row["status"] == "answered"]
        lead_minutes = [
            (parse_iso(row["answered_at"]) - parse_iso(row["received_at"])).total_seconds() / 60
            for row in answered
            if row["category"] == "lead"
        ]
        untouched = [
            row for row in answered if row["reply_text"].strip() == row["analysis"]["extraction"]["suggested_reply"].strip()
        ]
        costs = [row["analysis"]["meta"]["cost_usd"] or 0 for row in rows]
        by_category: dict[str, int] = {}
        for row in rows:
            by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        return {
            "total": len(rows),
            "waiting": sum(row["status"] == "new" for row in rows),
            "hot_waiting": sum(row["status"] == "new" and row["priority"] == "hot" for row in rows),
            "overdue": sum(self._overdue(row, now) for row in rows),
            "needs_review": sum(row["status"] == "new" and row["needs_review"] for row in rows),
            "answered": len(answered),
            "spam_filtered": by_category.get("spam", 0),
            "avg_lead_response_minutes": round(sum(lead_minutes) / len(lead_minutes), 1) if lead_minutes else None,
            "draft_accept_rate": round(len(untouched) / len(answered), 2) if answered else None,
            "time_saved_minutes": round(len(rows) * self.settings.manual_minutes_per_message),
            "manual_minutes_per_message": self.settings.manual_minutes_per_message,
            "llm_cost_usd": round(sum(costs), 4),
            "by_category": by_category,
        }

    # --- действия ----------------------------------------------------------------------------

    def ingest(self, msg: IncomingMessage, received_at: datetime | None = None, force_rules: bool = False) -> dict:
        """Принимает обращение: анализ, маршрут, запись в базу и журнал."""
        received_at = received_at or utcnow()
        analysis = self.pipeline.run(msg, force_rules=force_rules)
        status = "archived" if analysis.routing.auto_archive else "new"
        message_id = self.store.insert(msg, analysis, received_at, status)
        self.store.add_event(
            message_id, "received", f"Получено по каналу: {CHANNEL_LABELS.get(msg.channel, msg.channel)}", received_at
        )
        self._log_analysis(message_id, analysis, received_at)
        return self.card(message_id)

    def notify(self, message_id: int) -> None:
        """Передаёт карточку во внешние системы (вызывается в фоне после ответа клиенту API)."""
        if not self.notifier.enabled:
            return
        for kind, detail in self.notifier.dispatch(self.card(message_id)):
            self.store.add_event(message_id, kind, detail)

    def reply(self, message_id: int, text: str) -> dict:
        row = self._require(message_id)
        if row["status"] == "answered":
            raise Conflict("На это обращение уже ответили")
        text = text.strip()
        if not text:
            raise ValueError("Ответ не может быть пустым")
        draft = row["analysis"]["extraction"]["suggested_reply"].strip()
        self.store.update(message_id, status="answered", reply_text=text, answered_at=iso(utcnow()))
        detail = "черновик одобрен без правок" if text == draft else "оператор отредактировал черновик"
        self.store.add_event(message_id, "reply_sent", f"Ответ отправлен: {detail}")
        return self.card(message_id)

    def archive(self, message_id: int) -> dict:
        self._require(message_id)
        self.store.update(message_id, status="archived")
        self.store.add_event(message_id, "archived", "Оператор отправил обращение в архив")
        return self.card(message_id)

    def set_category(self, message_id: int, category: Category) -> dict:
        """Оператор исправляет категорию: маршрут и оценка пересчитываются, правка попадает в журнал."""
        row = self._require(message_id)
        analysis = Analysis.model_validate(row["analysis"])
        old = analysis.extraction.category
        if old == category:
            return self.card(message_id)
        msg = IncomingMessage(channel=row["channel"], sender=row["sender"], subject=row["subject"], body=row["body"])
        reply = analysis.extraction.suggested_reply
        # Шаблонный черновик зависит от категории, поэтому пересобирается. Ответ, написанный LLM,
        # остаётся как есть: оператор поправит его сам.
        if category != Category.spam and (analysis.meta.engine == "rules" or not reply.strip()):
            normalized = tu.normalize(f"{msg.subject or ''}\n{msg.body}")
            reply = self.rules.reply_template(
                category.value, Contact(**analysis.extraction.contact.model_dump()), analysis.extraction.interest, normalized
            )
        extraction = analysis.extraction.model_copy(
            update={"category": category, "confidence": 1.0, "suggested_reply": reply}
        )
        updated = self.pipeline.finalize(extraction, msg, analysis.meta, checks=analysis.checks)
        updated = updated.model_copy(update={"needs_review": False})
        status = row["status"]
        if updated.routing.auto_archive:
            status = "archived"
        elif status == "archived" and old == Category.spam:
            status = "new"
        self.store.update_analysis(
            message_id,
            updated,
            parse_iso(row["received_at"]),
            status=status,
            corrected_from=row["corrected_from"] or old.value,
        )
        self.store.add_event(
            message_id,
            "corrected",
            f"Оператор исправил категорию: {CATEGORY_LABELS[old.value]} → {CATEGORY_LABELS[category.value]}",
        )
        self._log_route(message_id, updated)
        return self.card(message_id)

    def reanalyze(self, message_id: int) -> dict:
        row = self._require(message_id)
        msg = IncomingMessage(channel=row["channel"], sender=row["sender"], subject=row["subject"], body=row["body"])
        analysis = self.pipeline.run(msg)
        status = row["status"]
        if status != "answered":
            status = "archived" if analysis.routing.auto_archive else "new"
        self.store.update_analysis(message_id, analysis, parse_iso(row["received_at"]), status=status)
        self.store.add_event(message_id, "reanalyzed", "Запущен повторный разбор")
        self._log_analysis(message_id, analysis)
        return self.card(message_id)

    # --- демо --------------------------------------------------------------------------------

    def seed(self) -> int:
        """Заполняет базу демо-обращениями со свежими метками времени."""
        self.store.clear()
        now = utcnow()
        items = load_dataset("messages_dev.json")
        for item in sorted(items, key=lambda entry: -entry["minutes_ago"]):
            received_at = now - timedelta(minutes=item["minutes_ago"])
            card = self.ingest(to_message(item), received_at, force_rules=not self.settings.seed_with_llm)
            if item.get("seed_status") == "answered" and card["status"] == "new":
                answered_at = received_at + timedelta(minutes=item["response_minutes"])
                draft = card["analysis"]["extraction"]["suggested_reply"]
                note = item.get("reply_note")
                self.store.update(
                    card["id"],
                    status="answered",
                    reply_text=f"{draft}\n\n{note}" if note else draft,
                    answered_at=iso(answered_at),
                )
                detail = "оператор отредактировал черновик" if note else "черновик одобрен без правок"
                self.store.add_event(card["id"], "reply_sent", f"Ответ отправлен: {detail}", answered_at)
        self.store.set_meta("seeded_at", iso(now))
        self.store.set_meta("simulated", "[]")
        return len(items)

    def simulate_next(self) -> dict:
        """Имитирует новое входящее: берёт следующее обращение из отложенного набора."""
        used = json.loads(self.store.get_meta("simulated") or "[]")
        for item in load_dataset("messages_holdout.json"):
            if item["id"] not in used:
                card = self.ingest(to_message(item))
                self.store.set_meta("simulated", json.dumps(used + [item["id"]]))
                return card
        raise Conflict("Все демо-обращения уже загружены. Сбросьте демо, чтобы начать заново.")

    def seeded_at(self) -> str | None:
        return self.store.get_meta("seeded_at")

    # --- внутреннее --------------------------------------------------------------------------

    def _require(self, message_id: int) -> dict:
        row = self.store.get(message_id)
        if row is None:
            raise NotFound(f"Обращение {message_id} не найдено")
        return row

    def _decorate(self, row: dict, with_events: bool = False) -> dict:
        row["category_label"] = CATEGORY_LABELS[row["category"]]
        row["channel_label"] = CHANNEL_LABELS.get(row["channel"], row["channel"])
        if with_events:
            row["events"] = self.store.events(row["id"])
        return row

    @staticmethod
    def _overdue(row: dict, now: datetime) -> bool:
        return row["status"] == "new" and bool(row["sla_due_at"]) and parse_iso(row["sla_due_at"]) < now

    def _log_analysis(self, message_id: int, analysis: Analysis, ts: datetime | None = None) -> None:
        extraction, meta = analysis.extraction, analysis.meta
        engine = "правила" if meta.engine == "rules" else (meta.model or "Claude")
        self.store.add_event(
            message_id,
            "analyzed",
            f"Категория: {CATEGORY_LABELS[extraction.category.value]} · уверенность "
            f"{round(extraction.confidence * 100)}% · движок: {engine} · {meta.latency_ms} мс",
            ts,
        )
        if meta.note:
            self.store.add_event(message_id, "note", meta.note, ts)
        for check in analysis.checks:
            self.store.add_event(message_id, "check", f"Сверка: {check}", ts)
        if analysis.needs_review:
            self.store.add_event(message_id, "review", "Низкая уверенность: помечено на проверку человеком", ts)
        self._log_route(message_id, analysis, ts)

    def _log_route(self, message_id: int, analysis: Analysis, ts: datetime | None = None) -> None:
        routing = analysis.routing
        if routing.auto_archive:
            self.store.add_event(message_id, "auto_archived", "Спам: автоматически отправлено в архив", ts)
            return
        who = f"{routing.queue} → {routing.assignee}" if routing.assignee else routing.queue
        self.store.add_event(
            message_id,
            "routed",
            f"Маршрут: {who} · приоритет {PRIORITY_LABELS[routing.priority]} · ответить за {routing.sla_minutes} мин",
            ts,
        )
