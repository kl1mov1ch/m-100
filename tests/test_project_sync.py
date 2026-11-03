import asyncio
import json
import os
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp import signed

from stroybot.crm import CRMConfig, QueuedBitrix
from stroybot.domain import UserError, parse_table
from stroybot.miniapp import create_app
from stroybot.reports import changes
from stroybot.store import Store

pytest_plugins = ("test_access",)


def test_us_dates_are_inferred_across_entire_header():
    rows = [
        ["х", "Наименование", "ед изм", "План"],
        [],
        [None] * 9 + ["8/19/2026", "9/1/2026"],
        [1, "Кабель", "м", 100, None, None, None, None, None, 3, 4],
    ]
    snapshot = parse_table(rows, "2026-09-01")
    assert snapshot.date_columns == {"2026-08-19": [10], "2026-09-01": [11]}
    assert snapshot.items[0].today == "4" and snapshot.items[0].completed == "7"
    rows[2][9:] = ["19/8/2026", "1/9/2026"]
    assert parse_table(rows, "2026-09-01").items[0].today == "4"
    rows[2][9:] = ["8/19/2026", "19/8/2026"]
    with pytest.raises(UserError, match="смешаны"):
        parse_table(rows, "2026-09-01")


def test_google_work_saves_with_crm_disconnected_and_keeps_durable_queue(hub):
    hub.add_project(
        1,
        {
            "name": "Рабочая смета",
            "url": "https://docs.google.com/spreadsheets/d/" + "w" * 30 + "/edit",
            "sheet": "Лист1",
        },
    )
    key = hub.access.selected(1)
    assert hub.access.project(key)["kind"] == "google_work"
    with hub.scope(1):
        assert hub.settings.mode == "google"
        for qty in ["2", "3"]:
            draft = hub.manual(1, "51", qty)
            assert "Google" in hub.confirm(1, draft.id, draft.revision)
        assert not hub.store.pending()
        assert (
            Decimal(hub.snapshot().by_id()[next(i.id for i in hub.snapshot().items if i.code == "51")].today)
            == 5
        )
        assert hub.bitrix.status()["pending"] == 1
        report = json.loads(hub.store.db.execute("SELECT report FROM crm_outbox").fetchone()[0])
        assert Decimal(next(p["quantity"] for p in report["positions"] if p["code"] == "51")) == 5
        assert all("CRM ожидает" in row["status"] for row in changes(hub, 1)["rows"])
    assert hub.sync_crm() == 0
    assert hub.access.selected(1) == key


def test_only_admin_can_reassign_and_employee_cannot_select_even_own_project(hub):
    key = hub.access.add_project(1, "Объект Б", "b" * 30, "Лист1")
    hub.access.save_account(1, 2, "Иван", "worker", key)
    assert hub.access.selected(2) == key
    assert len(hub.access.projects(2)) == 1
    with pytest.raises(UserError, match="администратору"):
        hub.access.select(2, key)
    with pytest.raises(UserError):
        hub.access.save_account(2, 2, "Иван", "admin", "default")
    hub.access.save_account(1, 2, "Иван", "worker", "default")
    assert hub.access.selected(2) == "default"


def test_read_only_google_sheet_is_not_added_as_writable_project(hub, monkeypatch):
    from stroybot import access
    from stroybot.gateways import LocalSheets

    class ReadOnly(LocalSheets):
        def check_write_access(self):
            raise UserError("Нужны права редактора")

    monkeypatch.setattr(access, "GoogleSheets", lambda settings, client: ReadOnly())
    before = len(hub.access.projects(1))
    with pytest.raises(UserError, match="редактора"):
        hub.add_project(
            1,
            {
                "name": "Без доступа",
                "url": "https://docs.google.com/spreadsheets/d/" + "r" * 30 + "/edit",
                "sheet": "Лист1",
            },
        )
    assert len(hub.access.projects(1)) == before


def test_admin_sees_team_audit_while_worker_only_sees_own_changes(hub):
    hub.add_project(
        1,
        {
            "name": "Команда",
            "url": "https://docs.google.com/spreadsheets/d/" + "t" * 30 + "/edit",
            "sheet": "Лист1",
        },
    )
    key = hub.access.selected(1)
    hub.access.save_account(1, 2, "Иван", "worker", key)
    for owner in (1, 2):
        with hub.scope(owner):
            draft = hub.manual(owner, "51", "1")
            hub.confirm(owner, draft.id, draft.revision)
    with hub.scope(1):
        assert {r["author"] for r in changes(hub, 1)["rows"]} == {"1", "2"}
    with hub.scope(2):
        assert {r["author"] for r in changes(hub, 2)["rows"]} == {"2"}


def test_crm_config_checks_portal_and_permissions_without_creating_deal(settings, tmp_path):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(
            200, json={"result": {k: {} for k in ("TITLE", "COMMENTS", "ORIGIN_ID", "ORIGINATOR_ID")}}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        config = CRMConfig(settings, client)
        with pytest.raises(UserError):
            config.connect("https://evil.test/rest/1/abcdefgh/")
        config.connect("https://example.bitrix24.ru/rest/1/abcdefgh/", 0)
        assert len(calls) == 1 and calls[0].endswith("crm.deal.fields.json")
        assert config.status()["configured"] and "abcdefgh" not in json.dumps(config.status())
        if os.name == "posix":
            assert config.path.stat().st_mode & 0o777 == 0o600


def test_crm_queue_survives_restart_and_retry_does_not_duplicate(settings, tmp_path, monkeypatch):
    from stroybot import crm

    config = CRMConfig(replace(settings, bitrix_url="https://example.test/rest/1/secret/"), None)
    records = {}
    calls = []

    class Gateway:
        def __init__(self, *args):
            pass

        def upsert(self, key, title, report):
            records[key] = report
            calls.append(key)
            if len(calls) == 1:
                raise UserError("Нет подтверждения CRM")

    monkeypatch.setattr(crm, "Bitrix24", Gateway)
    path = tmp_path / "queue.sqlite3"
    store = Store(path)
    queue = QueuedBitrix(store, config)
    queue.upsert("project/day", "Объект", {"quantity": "2"})
    assert queue.flush() == 0 and queue.status()["pending"] == 1
    store.close()
    store = Store(path)
    queue = QueuedBitrix(store, config)
    assert queue.flush() == 1 and len(records) == 1
    queue.upsert("project/day", "Объект", {"quantity": "2"})
    assert queue.flush() == 0
    queue.upsert("project/day", "Объект", {"quantity": "5"})
    assert queue.flush() == 1 and len(records) == 1 and records["project/day"]["quantity"] == "5"
    store.close()


def test_crm_configuration_api_is_admin_only_and_never_returns_secret(hub, monkeypatch):
    seen = []

    def connect(webhook, category, disk=False):
        seen.append((webhook, category))
        return {"configured": True}

    monkeypatch.setattr(hub.crm, "connect", connect)

    async def scenario():
        async with TestClient(TestServer(create_app(hub))) as client:
            body = {"action": "crm_connect", "webhook": "sensitive-test-value", "category": 0}
            response = await client.post(
                "/api/access", headers={"X-Telegram-Init-Data": signed("test-token", 2)}, json=body
            )
            assert response.status == 400 and not seen
            response = await client.post(
                "/api/access", headers={"X-Telegram-Init-Data": signed("test-token", 1)}, json=body
            )
            assert response.status == 200 and "sensitive" not in await response.text()
            response = await client.get(
                "/api/admin", headers={"X-Telegram-Init-Data": signed("test-token", 1)}
            )
            assert "sensitive" not in await response.text()

    asyncio.run(scenario())
