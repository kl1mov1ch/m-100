import asyncio

from aiohttp.test_utils import TestClient, TestServer

from stroybot.miniapp import create_app

pytest_plugins = ("test_access",)


def test_local_owner_access_requires_private_token_and_loopback_context(hub):
    async def scenario():
        async with TestClient(
            TestServer(create_app(hub, preview_token="local-test-only", local_owner=1))
        ) as client:
            headers = {"Host": "127.0.0.1:8765", "X-Preview-Token": "local-test-only"}
            assert (await client.get("/api/admin", headers=headers)).status == 200
            for extra in (
                {"X-Forwarded-Proto": "https"},
                {"X-Forwarded-Host": "example.com"},
                {"Host": "public.example"},
                {"X-Preview-Token": "incorrect"},
            ):
                assert (await client.get("/api/admin", headers={**headers, **extra})).status == 400
            html = await (await client.get("/")).text()
            assert "telegram.org/js" not in html

    asyncio.run(scenario())


def test_web_entrypoint_does_not_enable_local_owner_by_default(hub):
    hub.base.settings.mode = "google"

    async def scenario():
        async with TestClient(TestServer(create_app(hub, preview_token="same-secret"))) as client:
            response = await client.get(
                "/api/admin", headers={"Host": "127.0.0.1:8765", "X-Preview-Token": "same-secret"}
            )
            assert response.status == 400

    asyncio.run(scenario())


def test_duplicate_local_profile_has_friendly_error_and_can_reopen(tmp_path):
    import pytest
    from stroybot.store import ProcessLock
    from stroybot.domain import UserError

    path = tmp_path / "profile.lock"
    with ProcessLock(path):
        with pytest.raises(UserError, match="Другой экземпляр"):
            ProcessLock(path)
    with ProcessLock(path):
        pass
