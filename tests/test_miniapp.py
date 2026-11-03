import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from stroybot.domain import UserError
from stroybot.miniapp import authenticate, create_app


def signed(token, owner=1, age=0):
    values = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": owner}), "query_id": "test"}
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(
        secret, "\n".join(f"{k}={v}" for k, v in sorted(values.items())).encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(values)


def test_telegram_auth_valid_signature():
    assert authenticate(signed("test-token"), "test-token", {1}) == 1


@pytest.mark.parametrize("case", ["expired", "future", "other", "tampered", "duplicate", "empty"])
def test_telegram_auth_rejects_invalid_inputs(case):
    raw = signed(
        "test-token",
        owner=2 if case == "other" else 1,
        age=4000 if case == "expired" else -100 if case == "future" else 0,
    )
    if case == "tampered":
        raw = raw.replace("auth_date=", "auth_date=9")
    if case == "duplicate":
        raw += "&auth_date=1"
    if case == "empty":
        raw = ""
    with pytest.raises(UserError):
        authenticate(raw, "test-token", {1})


def test_web_manual_flow_confirmation_and_no_duplicates(service):
    service.settings.token = "test-token"
    service.settings.allowed = {1}

    async def scenario():
        async with TestClient(TestServer(create_app(service))) as client:
            headers = {"X-Telegram-Init-Data": signed("test-token")}
            denied = await client.get("/api/state")
            assert denied.status == 400
            response = await client.get("/api/state", headers=headers)
            data = await response.json()
            assert len(data["items"]) == 62
            assert "token" not in data and "settings" not in data
            item = next(i for i in data["items"] if i["code"] == "51")
            payload = {
                "action": "add",
                "item_id": item["id"],
                "quantity": "5",
                "mode": "add",
                "request_id": "12345678-1234-1234-1234-123456789abc",
            }
            first = await client.post("/api/action", json=payload, headers=headers)
            assert first.status == 200
            data = (await first.json())["state"]
            assert data["draft"]["ready"]
            assert next(i for i in data["items"] if i["id"] == item["id"])["today"] == "0"
            repeated = await client.post("/api/action", json=payload, headers=headers)
            assert len((await repeated.json())["state"]["draft"]["lines"]) == 1
            draft = data["draft"]
            update = {
                "action": "quantity",
                "draft_id": draft["id"],
                "revision": draft["revision"],
                "index": 1,
                "quantity": "7",
            }
            changed = await client.post("/api/action", json=update, headers=headers)
            new_draft = (await changed.json())["state"]["draft"]
            stale = await client.post("/api/action", json=update, headers=headers)
            assert stale.status == 400
            confirmation = {
                "action": "confirm",
                "draft_id": new_draft["id"],
                "revision": new_draft["revision"],
            }
            confirmed = await client.post("/api/action", json=confirmation, headers=headers)
            result = await confirmed.json()
            assert result["state"]["draft"] is None
            assert float(next(i for i in result["state"]["items"] if i["id"] == item["id"])["today"]) == 7
            confirmed = await client.post("/api/action", json=confirmation, headers=headers)
            assert (
                float(
                    next(i for i in (await confirmed.json())["state"]["items"] if i["id"] == item["id"])[
                        "today"
                    ]
                )
                == 7
            )
            static = await client.get("/")
            assert static.status == 200
            assert "Отчёт мастера" in await static.text()
            assert "default-src 'self'" in static.headers["Content-Security-Policy"]

    asyncio.run(scenario())


def test_preview_token_cannot_be_used_through_public_tunnel(service):
    service.settings.allowed = {1}

    async def scenario():
        async with TestClient(TestServer(create_app(service, preview_token="local-secret"))) as client:
            headers = {"Host": "127.0.0.1:8765", "X-Preview-Token": "local-secret"}
            good = await client.get("/api/state", headers=headers)
            assert good.status == 200
            external = await client.get("/api/state", headers={**headers, "CF-Connecting-IP": "1.2.3.4"})
            assert external.status == 400
            external = await client.get("/api/state", headers={**headers, "Host": "public.example"})
            assert external.status == 400

    asyncio.run(scenario())
