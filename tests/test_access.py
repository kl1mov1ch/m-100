import asyncio
from dataclasses import replace

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp import signed

import stroybot.access as access_module
from stroybot.access import Access, ServiceHub
from stroybot.domain import UserError
from stroybot.gateways import LocalSheets
from stroybot.miniapp import create_app
from stroybot.networking import proxy_url


@pytest.fixture
def hub(service, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "1")
    service.settings.allowed = {1, 2}
    service.settings.token = "test-token"

    class FakeGoogle(LocalSheets):
        def check_write_access(self):
            pass

    monkeypatch.setattr(
        access_module,
        "GoogleSheets",
        lambda settings, client: FakeGoogle(
            state=settings.database.with_name(settings.database.stem + "-sheet.json")
        ),
    )
    access = Access(tmp_path / "access.sqlite3", service.settings)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        hub = ServiceHub(service, client, access)
        yield hub
        hub.close()


def project(hub):
    return hub.access.add_project(1, "Второй объект", "a" * 30, "Лист1")


def test_roles_owner_protection_and_disable(hub):
    assert hub.access.admin(1) and not hub.access.admin(2)
    with pytest.raises(UserError):
        hub.access.save_account(2, 3, "Другой", "admin", "default")
    with pytest.raises(UserError):
        hub.access.disable(1, 1)
    hub.access.save_account(1, 3, "Администратор", "admin", "default")
    assert 3 in hub.access.allowed() and hub.access.admin(3)
    hub.access.disable(1, 3)
    assert 3 not in hub.access.allowed()
    with pytest.raises(UserError):
        hub.access.panel(3)


def test_worker_can_only_select_assigned_project(hub):
    key = project(hub)
    hub.access.save_account(1, 2, "Мастер", "worker", key)
    assert [p["id"] for p in hub.access.projects(2)] == [key]
    with pytest.raises(UserError):
        hub.access.select(2, "default")
    hub.access.select(1, key)
    with hub.scope(1):
        assert hub.settings.project == "Второй объект"
        assert hub.settings.mode == "setup"
    assert hub.settings.project == hub.base.settings.project


def test_project_validation_read_and_duplicate(hub):
    data = {
        "name": "Офис",
        "url": "https://docs.google.com/spreadsheets/d/" + "b" * 30 + "/edit",
        "sheet": "Лист1",
    }
    assert "62" in hub.add_project(1, data)
    with pytest.raises(UserError):
        hub.add_project(1, data)
    with pytest.raises(UserError):
        hub.add_project(2, {**data, "url": "https://docs.google.com/spreadsheets/d/" + "c" * 30})
    for url in [
        "http://127.0.0.1/",
        "https://evil.test/spreadsheets/d/" + "b" * 30,
        "https://docs.google.com.evil.test/spreadsheets/d/" + "b" * 30,
    ]:
        with pytest.raises(UserError):
            hub.add_project(1, {**data, "url": url})


def test_project_drafts_and_context_survive_thread_and_restart(hub):
    first = hub.base.manual(1, "51", "5")
    key = project(hub)
    hub.access.select(1, key)
    with hub.scope(1):
        assert hub.store.active(1) is None
        second = hub.manual(1, "13", "15")
        assert second.id != first.id

        async def check():
            assert await asyncio.to_thread(hub.current_key) == key

        asyncio.run(check())
        with pytest.raises(UserError):
            hub.confirm(1, second.id, second.revision)
    hub.access.select(1, "default")
    with hub.scope(1):
        assert hub.store.active(1).id == first.id
    reloaded = Access(
        hub.base.settings.database.parent / "access.sqlite3", replace(hub.base.settings, allowed={1, 2})
    )
    try:
        assert len(reloaded.projects(1)) == 2
    finally:
        reloaded.db.close()


def test_api_admin_auth_revocation_and_stale_object(hub):
    key = project(hub)

    async def scenario():
        async with TestClient(TestServer(create_app(hub))) as client:
            admin = {"X-Telegram-Init-Data": signed("test-token", 1)}
            worker = {"X-Telegram-Init-Data": signed("test-token", 2)}
            assert (await client.get("/api/admin")).status == 400
            assert (await client.get("/api/admin", headers=worker)).status == 400
            assert (await client.get("/api/admin", headers=admin)).status == 200
            response = await client.post(
                "/api/access", headers=worker, json={"action": "select", "project": key}
            )
            assert response.status == 400
            response = await client.post(
                "/api/access", headers=admin, json={"action": "select", "project": key}
            )
            assert response.status == 200
            response = await client.post(
                "/api/action", headers=admin, json={"action": "add", "context_project": "default"}
            )
            assert response.status == 400 and "другой объект" in (await response.json())["error"]
            hub.access.disable(1, 2)
            assert (await client.get("/api/access", headers=worker)).status == 400

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value", ["http://127.0.0.1:40000", "socks5://example.com:40000", "socks5://user:pass@127.0.0.1:40000"]
)
def test_only_local_socks_proxy(value, monkeypatch):
    monkeypatch.setenv("AI_PROXY_URL", value)
    with pytest.raises(UserError):
        proxy_url("AI_PROXY_URL")


def test_local_proxy_selected_explicitly(monkeypatch):
    monkeypatch.setenv("AI_PROXY_URL", "socks5://127.0.0.1:40000")
    monkeypatch.delenv("TELEGRAM_PROXY_URL", raising=False)
    assert proxy_url("AI_PROXY_URL") == "socks5://127.0.0.1:40000"
    assert proxy_url("TELEGRAM_PROXY_URL") is None
