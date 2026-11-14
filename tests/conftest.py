import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings, load_company  # noqa: E402
from app.service import load_dataset  # noqa: E402


def make_settings(db_path: Path, **overrides) -> Settings:
    values = dict(
        engine="rules",
        engine_requested="rules",
        model="claude-opus-5-5",
        effort="low",
        db_path=db_path,
        demo_mode=True,
        seed_with_llm=False,
        manual_minutes_per_message=4.0,
        outgoing_webhook_url=None,
        bitrix24_webhook_url=None,
        telegram_bot_token=None,
        telegram_chat_id=None,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path / "test.db")


@pytest.fixture
def company() -> dict:
    return load_company()


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def dev_items() -> list[dict]:
    return load_dataset("messages_dev.json")
