import asyncio
import logging
import os
from io import BytesIO

from aiogram import BaseMiddleware, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from .access import ServiceHub
from .domain import Draft, UserError, fmt
from .networking import telegram_bot
from .reports import changes, csv_file, filename
from .service import Service

HELP = """Отправьте дневной отчёт текстом или голосом. Я покажу список перед записью.
Следующее сообщение добавляет строки в открытый черновик.

/draft — текущий черновик
/today — объёмы за сегодня
/report [НАЧАЛО КОНЕЦ] — скачать изменения (даты ГГГГ-ММ-ДД)
/edit — исправить итоговые объёмы за сегодня
/find кабель — найти позиции и их коды
/manual КОД ОБЪЁМ — добавить позицию вручную
/set НОМЕР ОБЪЁМ — изменить строку черновика
/pick НОМЕР КОД — выбрать позицию для строки
/del НОМЕР — удалить строку черновика
/remain кабель 3х2,5 — узнать остаток
/retry — продолжить незавершённую запись
/cancel — отменить черновик

Обычный отчёт добавляет объёмы к сегодняшним. В режиме /edit вводите новый итог дня.
Дату беру по часовому поясу объекта. До подтверждения ничего не записывается."""


def chunks(text: str, limit: int = 3500) -> list[str]:
    result = []
    while len(text) > limit:
        split = text.rfind("\n", 0, limit)
        if split < limit // 2:
            split = limit
        result.append(text[:split])
        text = text[split:].lstrip("\n")
    return result + [text]


def keyboard(draft: Draft, catalog: dict | None = None) -> InlineKeyboardMarkup:
    prefix = f"{draft.id}:{draft.revision}"
    rows = []
    if draft.status == "draft":
        rows.append([InlineKeyboardButton(text="Подтвердить запись", callback_data=f"ok:{prefix}")])
        for index, line in enumerate(draft.lines[:10], 1):
            for candidate in line.candidates[:5] if not line.item_id else []:
                item = (catalog or {}).get(candidate)
                label = f"[{item.code or item.id}] {item.name[:55]}" if item else candidate
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"{index}: {label}", callback_data=f"pick:{prefix}:{index}:{candidate}"
                        )
                    ]
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Изменить объём {index}", callback_data=f"qty:{prefix}:{index}"
                    ),
                    InlineKeyboardButton(text=f"Удалить {index}", callback_data=f"del:{prefix}:{index}"),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text="Добавить работы", callback_data=f"add:{prefix}"),
                InlineKeyboardButton(text="Отменить", callback_data=f"cancel:{prefix}"),
            ]
        )
    elif draft.status == "pending":
        rows.append([InlineKeyboardButton(text="Повторить синхронизацию", callback_data=f"retry:{prefix}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def normalized(text: str) -> str:
    return " ".join(text.lower().replace("x", "х").replace("×", "х").replace(",", ".").split())


def build_dispatcher(service: Service, lock: asyncio.Lock | None = None) -> Dispatcher:
    dp = Dispatcher()
    if lock or isinstance(service, ServiceHub):

        class Serialize(BaseMiddleware):
            async def __call__(self, handler, event, data):
                async with lock or asyncio.Lock():
                    if isinstance(service, ServiceHub):
                        user = getattr(event.event, "from_user", None)
                        with service.scope(user.id if user else 0):
                            return await handler(event, data)
                    return await handler(event, data)

        dp.update.outer_middleware(Serialize())

    def permitted(message: Message) -> bool:
        return bool(
            message.from_user
            and message.from_user.id in service.settings.allowed
            and message.chat.type == "private"
        )

    async def send(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        parts = chunks(text)
        for index, part in enumerate(parts):
            await message.answer(part, reply_markup=markup if index == len(parts) - 1 else None)

    async def show(message: Message, draft: Draft) -> None:
        preview = await asyncio.to_thread(service.preview, draft)
        snapshot = await asyncio.to_thread(service.sheets.read, draft.day)
        await send(message, preview, keyboard(draft, snapshot.by_id()))

    async def guarded(message: Message, action) -> None:
        if not permitted(message):
            await message.answer("Доступ разрешён только указанным владельцем пользователям в личном чате.")
            return
        try:
            await action()
        except UserError as error:
            await send(message, str(error))
        except Exception as error:
            logging.error("Handler failed: %s", type(error).__name__)
            await message.answer(
                "Не удалось завершить действие. Проверьте /draft и /retry перед повторным вводом."
            )

    @dp.message(Command("id"))
    async def identity(message: Message):
        if message.chat.type == "private" and message.from_user:
            await message.answer(f"Ваш Telegram ID: {message.from_user.id}")

    @dp.message(Command("start", "help"))
    async def start(message: Message):
        async def action():
            url = os.getenv("MINIAPP_URL", "")
            if url.startswith("https://"):
                markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Открыть отчёт", web_app=WebAppInfo(url=url))]
                    ]
                )
                await send(
                    message,
                    "Откройте отчёт: выберите работы, укажите объёмы и проверьте список. "
                    "Также можно отправить отчёт текстом или голосом в этот чат.",
                    markup,
                )
                return
            banner = (
                "ЛОКАЛЬНЫЙ РЕЖИМ: записи сохраняются только на этом компьютере.\n\n"
                if (service.settings.mode == "local")
                else ""
            )
            await send(message, banner + HELP)

        await guarded(message, action)

    @dp.message(Command("report"))
    async def export_report(message: Message):
        async def action():
            args = (message.text or "").split()[1:]
            if len(args) not in {0, 2}:
                raise UserError("Пример: /report 2026-11-01 2026-11-30. Без дат — последние 31 день.")
            result = await asyncio.to_thread(changes, service, message.from_user.id, *(args or [None, None]))
            await message.answer_document(
                BufferedInputFile(csv_file(result), filename=filename(result)),
                caption="Изменения: было, разница, стало. CSV для Excel. "
                + ("Тестовые данные." if service.settings.mode == "local" else "Рабочие данные."),
            )

        await guarded(message, action)

    @dp.message(Command("draft"))
    async def draft(message: Message):
        async def action():
            current = service.store.active(message.from_user.id)
            if not current:
                raise UserError("Открытого черновика нет. Отправьте отчёт.")
            await show(message, current)

        await guarded(message, action)

    @dp.message(Command("today"))
    async def today(message: Message):
        async def action():
            snapshot = await asyncio.to_thread(service.snapshot)
            rows = [f"За {snapshot.day}:"]
            rows.extend(
                f"[{i.code or i.id}] {i.name}: {fmt(i.today)} {i.unit}"
                for i in snapshot.items
                if float(i.today) != 0
            )
            await send(message, "\n".join(rows) if len(rows) > 1 else "Сегодня объёмы ещё не внесены.")

        await guarded(message, action)

    @dp.message(Command("edit"))
    async def edit(message: Message):
        async def action():
            await asyncio.to_thread(service.new, message.from_user.id, "set")
            await message.answer(
                "Режим исправления: отправьте названия работ и НОВЫЕ ИТОГОВЫЕ объёмы "
                "за сегодня. Для обнуления используйте /manual КОД 0. "
                "Остальные позиции останутся как есть."
            )

        await guarded(message, action)

    @dp.message(Command("find"))
    async def find(message: Message):
        async def action():
            query = (message.text or "").partition(" ")[2]
            words = normalized(query).split()
            snapshot = await asyncio.to_thread(service.snapshot)
            matches = [i for i in snapshot.items if all(w in normalized(i.name) for w in words)]
            if not matches:
                raise UserError("Позиции не найдены. Попробуйте часть названия или размер.")
            rows = [f"[{i.code or i.id}] {i.section}: {i.name} ({i.unit})" for i in matches[:20]]
            if len(matches) > 20:
                rows.append("Показаны первые 20. Уточните поиск.")
            await send(message, "\n".join(rows))

        await guarded(message, action)

    @dp.message(Command("manual", "set", "pick", "del"))
    async def modify(message: Message):
        async def action():
            args = (message.text or "").split()
            command = args[0].split("@")[0][1:]
            try:
                if command == "manual" and len(args) == 3:
                    current = await asyncio.to_thread(
                        service.manual,
                        message.from_user.id,
                        args[1],
                        args[2],
                        f"{message.chat.id}:{message.message_id}",
                    )
                elif command in {"set", "pick"} and len(args) == 3:
                    current = await asyncio.to_thread(
                        service.change,
                        message.from_user.id,
                        int(args[1]),
                        "quantity" if command == "set" else "pick",
                        args[2],
                    )
                elif command == "del" and len(args) == 2:
                    current = await asyncio.to_thread(
                        service.change, message.from_user.id, int(args[1]), "delete"
                    )
                else:
                    raise ValueError
            except ValueError:
                raise UserError("Проверьте формат команды в /help.") from None
            service.store.input(message.from_user.id, clear=True)
            await show(message, current)

        await guarded(message, action)

    @dp.message(Command("cancel"))
    async def cancel(message: Message):
        async def action():
            service.cancel(message.from_user.id)
            service.store.input(message.from_user.id, clear=True)
            await message.answer("Черновик отменён.")

        await guarded(message, action)

    async def result(message: Message, text: str):
        rows = (
            [[InlineKeyboardButton(text="Открыть Google-таблицу", url=service.sheets.url)]]
            if (service.sheets.url)
            else []
        )
        await send(message, text, InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    @dp.message(Command("retry"))
    async def retry(message: Message):
        async def action():
            text = await asyncio.to_thread(service.resume, message.from_user.id)
            await result(message, text)

        await guarded(message, action)

    async def remaining(message: Message, query: str):
        if not service.recognizer:
            raise UserError("AI не подключён.")
        snapshot = await asyncio.to_thread(service.snapshot)
        lines = await asyncio.to_thread(service.recognizer.parse, query, snapshot, True)
        output = []
        for line in lines:
            if not line.item_id or line.question:
                output.append(line.question or "Уточните позицию.")
                for key in line.candidates:
                    item = snapshot.by_id()[key]
                    output.append(
                        f"[{item.code or item.id}] {item.name}: остаток {fmt(item.remaining)} {item.unit}"
                    )
                if not line.candidates:
                    output.append("Для поиска используйте /find ЧАСТЬ_НАЗВАНИЯ.")
            else:
                item = snapshot.by_id()[line.item_id]
                output.append(
                    f"[{item.code or item.id}] {item.name}\n"
                    f"По смете: {fmt(item.planned)}; выполнено: {fmt(item.completed)}; "
                    f"остаток: {fmt(item.remaining)} {item.unit}."
                )
        output.extend(snapshot.warnings)
        await send(message, "\n".join(output))

    @dp.message(Command("remain"))
    async def remain(message: Message):
        async def action():
            query = (message.text or "").partition(" ")[2]
            if not query:
                raise UserError("Пример: /remain кабель 3х2,5")
            await remaining(message, query)

        await guarded(message, action)

    @dp.callback_query()
    async def callback(query: CallbackQuery):
        if (
            not query.from_user
            or query.from_user.id not in service.settings.allowed
            or not isinstance(query.message, Message)
            or query.message.chat.type != "private"
        ):
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()

        async def action():
            fields = (query.data or "").split(":")
            if len(fields) < 3:
                raise UserError("Неизвестная кнопка.")
            action, draft_id, revision = fields[:3]
            owner = query.from_user.id
            draft = service.store.load(draft_id, owner)
            if action == "ok":
                text = await asyncio.to_thread(service.confirm, owner, draft_id, int(revision))
                await result(query.message, text)
                return
            if action == "retry":
                text = await asyncio.to_thread(service.resume, owner, draft_id)
                await result(query.message, text)
                return
            if draft.status != "draft" or draft.revision != int(revision):
                raise UserError("Кнопка устарела. Откройте /draft.")
            if action == "cancel":
                service.cancel(owner)
                service.store.input(owner, clear=True)
                await query.message.answer("Черновик отменён.")
            elif action == "add":
                service.store.input(owner, clear=True)
                await query.message.answer("Отправьте дополнительные работы текстом или голосом.")
            elif action == "qty":
                service.store.input(
                    owner, {"draft": draft_id, "revision": draft.revision, "index": int(fields[3])}
                )
                await query.message.answer(f"Введите новый объём для строки {fields[3]} одним числом.")
            elif action in {"pick", "del"}:
                current = await asyncio.to_thread(
                    service.change,
                    owner,
                    int(fields[3]),
                    "pick" if action == "pick" else "delete",
                    fields[4] if action == "pick" else "",
                )
                await show(query.message, current)
            else:
                raise UserError("Неизвестная кнопка.")

        # Callback messages are sent by the bot; authorization was checked against query.from_user above.
        try:
            await action()
        except UserError as error:
            await send(query.message, str(error))
        except Exception as error:
            logging.error("Callback failed: %s", type(error).__name__)
            await query.message.answer("Действие не завершено. Проверьте /draft и /retry.")

    @dp.message(F.text | F.voice)
    async def report(message: Message):
        async def action():
            owner = message.from_user.id
            text = message.text or ""
            pending_input = service.store.input(owner)
            if pending_input and message.text:
                current = service.editable(owner)
                if current.id != pending_input["draft"] or current.revision != pending_input["revision"]:
                    service.store.input(owner, clear=True)
                    raise UserError("Черновик изменился. Нажмите кнопку изменения заново.")
                current = await asyncio.to_thread(
                    service.change, owner, pending_input["index"], "quantity", text
                )
                service.store.input(owner, clear=True)
                await show(message, current)
                return
            if message.voice:
                if pending_input:
                    raise UserError("Введите объём числом или /cancel для отмены черновика.")
                if message.voice.file_size and message.voice.file_size > 19 * 1024 * 1024:
                    raise UserError("Голосовое сообщение слишком большое. Максимум 19 МБ.")
                audio = BytesIO()
                await message.bot.download(message.voice, destination=audio)
                text = await asyncio.to_thread(service.recognizer.transcribe, audio.getvalue())
                await send(message, "Распознано из голоса:\n" + text)
            if text.startswith("/"):
                raise UserError("Неизвестная команда. Список: /help.")
            if "сколько осталось" in text.lower():
                await remaining(message, text)
                return
            await message.answer("Разбираю сообщение…")
            current = await asyncio.to_thread(
                service.report, owner, text, f"{message.chat.id}:{message.message_id}"
            )
            if current.status == "done":
                await message.answer("Это сообщение уже записано.")
            else:
                await show(message, current)

        await guarded(message, action)

    return dp


async def run(service: Service, lock: asyncio.Lock | None = None) -> None:
    service.settings.validate(telegram=True)
    bot = telegram_bot(service.settings.token)
    dp = build_dispatcher(service, lock)
    try:
        url = os.getenv("MINIAPP_URL", "")
        if url.startswith("https://"):
            for owner in service.settings.allowed - {0}:
                await bot.set_chat_menu_button(
                    chat_id=owner,
                    menu_button=MenuButtonWebApp(text="Открыть отчёт", web_app=WebAppInfo(url=url)),
                )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Помощь"),
                BotCommand(command="draft", description="Черновик"),
                BotCommand(command="today", description="Сегодняшние объёмы"),
                BotCommand(command="report", description="Скачать отчёт по изменениям"),
                BotCommand(command="edit", description="Исправить сегодня"),
                BotCommand(command="find", description="Найти позицию"),
                BotCommand(command="remain", description="Остаток"),
                BotCommand(command="retry", description="Повторить синхронизацию"),
                BotCommand(command="cancel", description="Отменить черновик"),
            ]
        )
        # Serialize updates across users. Together with ProcessLock this prevents parallel writers.
        await dp.start_polling(bot, handle_as_tasks=False, close_bot_session=False)
    finally:
        await bot.session.close()
