NEW_LEAD = {
    "channel": "site_form",
    "sender": "Форма «Заказать замер»",
    "subject": None,
    "body": "Здравствуйте! Нужен кондиционер в офис 40 м², бюджет до 90 тыс., монтаж на этой неделе. Перезвоните: Мария, 8 900 000 00 70",
}


def test_demo_inbox_is_seeded(client):
    config = client.get("/api/config").json()
    assert config["engine"] == "rules" and config["company"] == "Прохлада-Сервис"

    cards = client.get("/api/messages").json()
    assert len(cards) == 26
    stats = client.get("/api/stats").json()
    assert stats["total"] == 26
    assert stats["spam_filtered"] == 3
    assert stats["waiting"] == len(client.get("/api/messages?view=waiting").json()) == 9
    assert stats["answered"] == 14
    assert stats["hot_waiting"] >= 3
    assert 0 < stats["draft_accept_rate"] < 1


def test_ingest_reply_flow(client):
    created = client.post("/api/messages", json=NEW_LEAD)
    assert created.status_code == 201
    card = created.json()
    assert card["category"] == "lead" and card["status"] == "new"
    assert card["analysis"]["extraction"]["contact"] == {
        "name": "Мария",
        "phone": "+79000000070",
        "email": None,
        "company": None,
    }
    assert card["analysis"]["extraction"]["budget_rub"] == 90_000
    assert card["priority"] == "hot" and card["sla_due_at"]
    assert [event["kind"] for event in card["events"]] == ["received", "analyzed", "routed"]

    draft = card["analysis"]["extraction"]["suggested_reply"]
    answered = client.post(f"/api/messages/{card['id']}/reply", json={"text": draft}).json()
    assert answered["status"] == "answered" and answered["reply_text"] == draft
    assert "без правок" in answered["events"][-1]["detail"]

    again = client.post(f"/api/messages/{card['id']}/reply", json={"text": "ещё раз"})
    assert again.status_code == 409


def test_operator_corrects_category(client):
    card = client.post("/api/messages", json={**NEW_LEAD, "body": "Передайте привет Петровичу со склада"}).json()
    assert card["category"] == "other" and card["needs_review"]

    fixed = client.post(f"/api/messages/{card['id']}/category", json={"category": "complaint"}).json()
    assert fixed["category"] == "complaint"
    assert fixed["corrected_from"] == "other"
    assert fixed["needs_review"] is False
    assert fixed["queue"] == "Руководитель сервиса"
    assert any(event["kind"] == "corrected" for event in fixed["events"])

    spam = client.post(f"/api/messages/{card['id']}/category", json={"category": "spam"}).json()
    assert spam["status"] == "archived"
    restored = client.post(f"/api/messages/{card['id']}/category", json={"category": "support"}).json()
    assert restored["status"] == "new"
    assert restored["analysis"]["extraction"]["suggested_reply"]


def test_search_and_views(client):
    found = client.get("/api/messages", params={"q": "тепловой насос"}).json()
    assert [card["category"] for card in found] == ["lead"]
    archived = client.get("/api/messages", params={"view": "archived"}).json()
    assert {card["category"] for card in archived} == {"spam"}


def test_simulate_until_exhausted_then_reset(client):
    statuses = [client.post("/api/demo/simulate").status_code for _ in range(17)]
    assert statuses == [201] * 16 + [409]
    assert client.get("/api/stats").json()["total"] == 42
    assert client.post("/api/demo/reset").json() == {"seeded": 26}
    assert client.get("/api/stats").json()["total"] == 26


def test_errors(client):
    assert client.get("/api/messages/9999").status_code == 404
    assert client.post("/api/messages/9999/archive").status_code == 404
    assert client.post("/api/messages", json={"channel": "fax", "sender": "x", "body": "y"}).status_code == 422
    assert client.post("/api/messages/1/category", json={"category": "vip"}).status_code == 422
    assert client.get("/").status_code == 200
