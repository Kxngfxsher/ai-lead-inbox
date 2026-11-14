"""HTTP API и раздача интерфейса оператора."""

from datetime import timedelta
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import parse_iso, utcnow
from .schemas import CATEGORY_LABELS, Category, IncomingMessage
from .service import Conflict, InboxService, NotFound

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO_STALE_AFTER = timedelta(hours=3)


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class CategoryIn(BaseModel):
    category: Category


def create_app(settings: Settings | None = None, service: InboxService | None = None, reset: bool = False) -> FastAPI:
    settings = settings or get_settings()
    service = service or InboxService(settings)
    if settings.demo_mode and (reset or service.store.count() == 0):
        service.seed()

    app = FastAPI(title="Умный входящий поток", docs_url="/api/docs", redoc_url=None)
    app.state.service = service

    def guarded(action):
        """Переводит ошибки сервисного слоя в HTTP-коды."""
        try:
            return action()
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/config")
    def config() -> dict:
        seeded_at = service.seeded_at()
        stale = bool(seeded_at) and utcnow() - parse_iso(seeded_at) > DEMO_STALE_AFTER
        return {
            "company": service.company["name"],
            "engine": service.engine.name,
            "engine_requested": settings.engine_requested,
            "engine_error": service.engine_error,
            "model": settings.model if service.engine.name == "claude" else None,
            "demo_mode": settings.demo_mode,
            "demo_stale": settings.demo_mode and stale,
            "integrations": service.notifier.enabled,
            "categories": CATEGORY_LABELS,
        }

    @app.get("/api/stats")
    def stats() -> dict:
        return service.stats()

    @app.get("/api/messages")
    def list_messages(view: str = "all", q: str = "") -> list[dict]:
        return service.list(view, q)

    @app.get("/api/messages/{message_id}")
    def get_message(message_id: int) -> dict:
        return guarded(lambda: service.card(message_id))

    @app.post("/api/messages", status_code=201)
    def ingest(msg: IncomingMessage, background: BackgroundTasks) -> dict:
        """Точка приёма обращений: сюда шлют данные форма на сайте, почтовый парсер или бот."""
        card = service.ingest(msg)
        background.add_task(service.notify, card["id"])
        return card

    @app.post("/api/messages/{message_id}/reply")
    def reply(message_id: int, payload: ReplyIn) -> dict:
        return guarded(lambda: service.reply(message_id, payload.text))

    @app.post("/api/messages/{message_id}/archive")
    def archive(message_id: int) -> dict:
        return guarded(lambda: service.archive(message_id))

    @app.post("/api/messages/{message_id}/category")
    def set_category(message_id: int, payload: CategoryIn) -> dict:
        return guarded(lambda: service.set_category(message_id, payload.category))

    @app.post("/api/messages/{message_id}/reanalyze")
    def reanalyze(message_id: int) -> dict:
        return guarded(lambda: service.reanalyze(message_id))

    if settings.demo_mode:

        @app.post("/api/demo/simulate", status_code=201)
        def simulate(background: BackgroundTasks) -> dict:
            card = guarded(service.simulate_next)
            background.add_task(service.notify, card["id"])
            return card

        @app.post("/api/demo/reset")
        def reset_demo() -> dict:
            return {"seeded": service.seed()}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
