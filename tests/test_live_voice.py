import asyncio

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp import signed

from stroybot.miniapp import create_app


def test_speech_auth_partial_results_and_no_write(service, monkeypatch):
    service.settings.token = "test-token"
    service.settings.allowed = {1}
    calls = []

    async def scenario():
        async def recognition(request):
            calls.append("open")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"ready": True, "sample_rate": 16000})
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    await ws.send_json({"text": "медь пятнадцать метров", "final": False})
                elif message.type == WSMsgType.TEXT:
                    await ws.send_json({"text": "медь пятнадцать метров", "final": True})
                    break
            await ws.close()
            return ws

        remote = web.Application()
        remote.router.add_get("/stream", recognition)
        async with TestClient(TestServer(remote)) as upstream:
            monkeypatch.setenv("SPEECH_URL", str(upstream.make_url("/")).rstrip("/"))
            async with TestClient(TestServer(create_app(service))) as client:
                async with client.ws_connect("/api/speech") as ws:
                    await ws.send_json({"init_data": "bad-signature"})
                    assert "error" in await ws.receive_json()
                assert not calls
                async with client.ws_connect("/api/speech") as ws:
                    await ws.send_json({"init_data": signed("test-token")})
                    assert (await ws.receive_json())["ready"]
                    await ws.send_bytes(b"\x00\x00" * 1600)
                    partial = await ws.receive_json()
                    assert not partial["final"] and "пятнадцать" in partial["text"]
                    await ws.send_json({"eof": True})
                    assert (await ws.receive_json())["final"]
                assert len(calls) == 1
                assert service.store.active(1) is None
                assert all(i.today == "0" for i in service.snapshot().items)

    asyncio.run(scenario())
