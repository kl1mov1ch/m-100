import asyncio
import time

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Message, Update

from stroybot.bot import build_dispatcher
from stroybot.domain import Line


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.sent),
                date=int(time.time()),
                chat={"id": method.chat_id, "type": "private"},
                from_user={"id": bot.id, "is_bot": True, "first_name": "Bot"},
                text=method.text,
            )
        return True

    async def stream_content(self, *args, **kwargs):
        yield b"fake-audio"


def test_telegram_confirm_edit_and_authorization(service):
    service.settings.allowed = {1}
    item = next(i for i in service.snapshot().items if i.code == "51")

    class FakeAI:
        def parse(self, *args):
            return [Line("кабель 56 м", "56", "м", [item.id], item.id)]

    service.recognizer = FakeAI()
    session = TelegramSession()
    bot = Bot("123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", session=session)
    dp = build_dispatcher(service)
    counter = 0

    async def message(text, owner=1):
        nonlocal counter
        counter += 1
        update = Update.model_validate(
            {
                "update_id": counter,
                "message": {
                    "message_id": counter,
                    "date": int(time.time()),
                    "chat": {"id": owner, "type": "private"},
                    "from": {"id": owner, "is_bot": False, "first_name": "Мастер"},
                    "text": text,
                },
            }
        )
        await dp.feed_update(bot, update)

    async def click(data):
        nonlocal counter
        counter += 1
        update = Update.model_validate(
            {
                "update_id": counter,
                "callback_query": {
                    "id": str(counter),
                    "chat_instance": "fake",
                    "data": data,
                    "from": {"id": 1, "is_bot": False, "first_name": "Мастер"},
                    "message": {
                        "message_id": counter,
                        "date": int(time.time()),
                        "chat": {"id": 1, "type": "private"},
                        "from": {"id": bot.id, "is_bot": True, "first_name": "Bot"},
                        "text": "Черновик",
                    },
                },
            }
        )
        await dp.feed_update(bot, update)

    async def scenario():
        await message("/id", owner=2)
        assert session.sent[-1].text == "Ваш Telegram ID: 2"
        await message("/start", owner=2)
        assert "Доступ разрешён" in session.sent[-1].text
        assert service.store.active(2) is None
        await message("кабель 56 м")
        draft = service.store.active(1)
        assert draft.lines[0].quantity == "56"
        assert service.snapshot().by_id()[item.id].today == "0"
        old = f"ok:{draft.id}:{draft.revision}"
        await click(f"qty:{draft.id}:{draft.revision}:1")
        await message("50")
        assert service.store.active(1).lines[0].quantity == "50"
        await click(old)
        assert "устарела" in session.sent[-1].text
        assert service.snapshot().by_id()[item.id].today == "0"
        draft = service.store.active(1)
        await click(f"ok:{draft.id}:{draft.revision}")
        assert service.snapshot().by_id()[item.id].today == "50.0"
        assert service.store.active(1) is None
        await message("/today")
        assert "50 м" in session.sent[-1].text
        await message("/edit")
        await message("/manual 51 40")
        draft = service.store.active(1)
        assert draft.mode == "set"
        await click(f"ok:{draft.id}:{draft.revision}")
        assert service.snapshot().by_id()[item.id].today == "40.0"
        await bot.session.close()

    asyncio.run(scenario())


def test_manual_message_retry_is_not_appended(service):
    first = service.manual(1, "51", "56", source="1:42")
    second = service.manual(1, "51", "56", source="1:42")
    assert first.id == second.id
    assert len(second.lines) == 1
