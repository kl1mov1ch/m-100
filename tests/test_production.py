from dataclasses import replace

import httpx
import pytest

from stroybot.domain import Line, UserError
from stroybot.miniapp import action, state
from stroybot.recognition import Recognizer
from stroybot.service import Service
from stroybot.store import Store


def test_setup_cannot_write_to_either_system(service):
    service.settings.mode = "setup"
    draft = service.manual(1, "51", "5")
    with pytest.raises(UserError, match="Битрикс24"):
        service.confirm(1, draft.id, draft.revision)
    assert service.store.operation(draft.id) is None
    assert not state(service, 1)["draft"]["ready"]
    assert not state(service, 1)["write_available"]


def test_setup_to_live_preserves_draft_and_binds_portal(settings, service, tmp_path):
    setup = replace(settings, mode="setup", spreadsheet="test-sheet")
    store = Store(tmp_path / "production.sqlite3")
    try:
        stage = Service(setup, store, service.sheets, service.bitrix)
        stage.today = service.today
        draft = stage.manual(1, "51", "5")
        live = Service(
            replace(setup, mode="live", bitrix_url="https://one.bitrix24.test/rest/1/key/"),
            store,
            service.sheets,
            service.bitrix,
        )
        live.today = service.today
        assert live.store.active(1).id == draft.id
        live.confirm(1, draft.id, draft.revision)
        assert live.store.operation(draft.id)["stage"] == "done"
        # Credential rotation on the same portal is allowed.
        Service(
            replace(setup, mode="live", bitrix_url="https://one.bitrix24.test/rest/1/rotated/"),
            store,
            service.sheets,
            service.bitrix,
        )
        with pytest.raises(UserError, match="портал"):
            Service(
                replace(setup, mode="live", bitrix_url="https://two.bitrix24.test/rest/1/key/"),
                store,
                service.sheets,
                service.bitrix,
            )
    finally:
        store.close()


def test_production_disallows_demo_and_preview(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(UserError, match="production"):
        settings.validate()
    key = tmp_path / "google.json"
    key.write_text("{}", encoding="utf-8")
    settings = replace(settings, mode="setup", spreadsheet="sheet", credentials=str(key))
    monkeypatch.setenv("MINIAPP_URL", "https://app.example.test")
    monkeypatch.setenv("MINIAPP_PREVIEW_TOKEN", "")
    settings.validate()
    monkeypatch.setenv("MINIAPP_PREVIEW_TOKEN", "forbidden")
    with pytest.raises(UserError, match="production"):
        settings.validate()


def test_text_inside_app_is_deduplicated_and_never_writes(service):
    item = next(i for i in service.snapshot().items if i.code == "51")
    calls = []

    class FakeAI:
        def parse(self, *args):
            calls.append(1)
            return [Line("кабель 5 м", "5", "м", [item.id], item.id)]

    service.recognizer = FakeAI()
    payload = {
        "action": "text",
        "text": "кабель 5 м",
        "mode": "add",
        "request_id": "12345678-1234-1234-1234-123456789abc",
    }
    action(service, 1, payload)
    action(service, 1, payload)
    assert calls == [1]
    assert len(service.store.active(1).lines) == 1
    assert service.snapshot().by_id()[item.id].today == "0"


def test_groq_free_limit_is_reported_without_paid_fallback(settings, rows):
    from stroybot.domain import parse_table

    calls = []

    def reply(request):
        calls.append(request.url.host)
        return httpx.Response(429, json={"error": "quota"})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        recognizer = Recognizer(
            replace(
                settings,
                llm_key="fake",
                llm_model="openai/gpt-oss-120b",
                llm_url="https://api.groq.com/openai/v1",
            ),
            client,
        )
        with pytest.raises(UserError, match="Лимит"):
            recognizer.parse("кабель 5 м", parse_table(rows, "2026-11-03"))
    assert calls == ["api.groq.com"]
