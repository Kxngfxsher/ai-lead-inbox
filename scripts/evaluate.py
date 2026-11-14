"""Оценка качества разбора на размеченных обращениях.

    python scripts/evaluate.py                    # движок правил, оба набора
    python scripts/evaluate.py --engine claude    # Claude: нужен ANTHROPIC_API_KEY, запросы платные

Наборы:
  dev      26 обращений, на которых настраивались правила (они же демо-данные);
  holdout  16 обращений, которые при настройке не использовались. Это и есть честная проверка.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import textutils as tu  # noqa: E402
from app.config import get_settings, load_company  # noqa: E402
from app.engines import build_engine  # noqa: E402
from app.engines.rules import RulesEngine  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.schemas import CATEGORY_LABELS  # noqa: E402
from app.service import load_dataset, to_message  # noqa: E402

FIELDS = ["category", "urgency", "name", "phone", "email", "company", "budget_rub"]
FIELD_LABELS = {
    "category": "Категория",
    "urgency": "Срочность",
    "name": "Имя",
    "phone": "Телефон",
    "email": "E-mail",
    "company": "Компания",
    "budget_rub": "Бюджет",
}


def canon(value: str) -> str:
    return " ".join(re.sub(r"[«»\"“”'.,]", " ", tu.normalize(value)).split())


def same(field: str, expected, actual) -> bool:
    if field in ("name", "company"):
        if not expected or not actual:
            return not expected and not actual
        a, b = canon(expected), canon(actual)
        return a in b or b in a
    return expected == actual


def predicted(analysis) -> dict:
    extraction = analysis.extraction
    return {
        "category": extraction.category.value,
        "urgency": extraction.urgency,
        "name": extraction.contact.name,
        "phone": extraction.contact.phone,
        "email": extraction.contact.email,
        "company": extraction.contact.company,
        "budget_rub": extraction.budget_rub,
    }


def evaluate(pipeline: Pipeline, items: list[dict]) -> dict:
    hits = {field: 0 for field in FIELDS}
    errors: list[dict] = []
    latency_ms = cost = 0.0
    fallbacks = 0
    for item in items:
        analysis = pipeline.run(to_message(item))
        got = predicted(analysis)
        latency_ms += analysis.meta.latency_ms
        cost += analysis.meta.cost_usd or 0
        fallbacks += bool(analysis.meta.note and "правилами" in analysis.meta.note)
        for field in FIELDS:
            if same(field, item["expected"][field], got[field]):
                hits[field] += 1
            else:
                errors.append(
                    {"id": item["id"], "field": field, "expected": item["expected"][field], "got": got[field]}
                )
    total = len(items)
    return {
        "total": total,
        "accuracy": {field: round(hits[field] / total, 3) for field in FIELDS},
        "errors": errors,
        "avg_latency_ms": round(latency_ms / total, 1),
        "cost_usd": round(cost, 4),
        "fell_back_to_rules": fallbacks,
    }


def print_report(name: str, report: dict) -> None:
    print(f"\n=== {name}: {report['total']} обращений ===")
    for field in FIELDS:
        share = report["accuracy"][field]
        print(f"  {FIELD_LABELS[field]:<10} {share * 100:5.1f}%  ({round(share * report['total'])}/{report['total']})")
    print(f"  Среднее время разбора: {report['avg_latency_ms']} мс · стоимость: ${report['cost_usd']}")
    if report["fell_back_to_rules"]:
        print(f"  Внимание: {report['fell_back_to_rules']} обращений разобраны правилами из-за ошибок LLM")
    category_errors = [error for error in report["errors"] if error["field"] == "category"]
    if category_errors:
        print("  Ошибки в категории:")
        for error in category_errors:
            expected = CATEGORY_LABELS[error["expected"]]
            got = CATEGORY_LABELS[error["got"]]
            print(f"    {error['id']}: ожидалось «{expected}», получено «{got}»")


def main() -> None:
    parser = argparse.ArgumentParser(description="Оценка качества разбора обращений")
    parser.add_argument("--engine", choices=["rules", "claude"], default="rules")
    parser.add_argument("--set", choices=["dev", "holdout", "all"], default="all", dest="dataset")
    parser.add_argument("--limit", type=int, default=0, help="взять только первые N обращений каждого набора")
    parser.add_argument("--json", type=Path, help="сохранить отчёт в JSON")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение платных запросов")
    args = parser.parse_args()

    settings = get_settings()
    company = load_company()
    rules = RulesEngine(company)
    engine = rules if args.engine == "rules" else build_engine("claude", company, settings.model, settings.effort)
    pipeline = Pipeline(engine, rules, company)

    names = ["dev", "holdout"] if args.dataset == "all" else [args.dataset]
    datasets = {name: load_dataset(f"messages_{name}.json") for name in names}
    if args.limit:
        datasets = {name: items[: args.limit] for name, items in datasets.items()}

    if args.engine == "claude" and not args.yes:
        count = sum(len(items) for items in datasets.values())
        answer = input(f"Будет отправлено {count} платных запросов к {settings.model}. Продолжить? [y/N] ")
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено.")
            return

    label = "правила" if args.engine == "rules" else f"Claude ({settings.model}, effort={settings.effort})"
    print(f"Движок: {label}")
    started = time.perf_counter()
    reports = {name: evaluate(pipeline, items) for name, items in datasets.items()}
    for name, report in reports.items():
        print_report(name, report)
    print(f"\nГотово за {time.perf_counter() - started:.1f} с")

    if args.json:
        args.json.write_text(json.dumps({"engine": label, "reports": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт сохранён: {args.json}")


if __name__ == "__main__":
    main()
