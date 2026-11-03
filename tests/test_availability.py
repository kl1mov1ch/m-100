import asyncio

import pytest

import stroybot.bot as bot_module
import stroybot.miniapp as miniapp


def test_telegram_failure_does_not_stop_web_server(service, monkeypatch):
    async def scenario():
        failed = asyncio.Event()

        class Runner:
            def __init__(self, *args, **kwargs):
                pass

            async def setup(self):
                pass

            async def cleanup(self):
                pass

        class Site:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                pass

        async def unavailable(*args):
            failed.set()
            raise OSError("network unreachable")

        monkeypatch.setattr(miniapp.web, "AppRunner", Runner)
        monkeypatch.setattr(miniapp.web, "TCPSite", Site)
        monkeypatch.setattr(bot_module, "run", unavailable)
        task = asyncio.create_task(miniapp.run(service))
        await asyncio.wait_for(failed.wait(), 1)
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
