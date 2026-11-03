"""Telegram Mini App: authenticated UI and API over the existing report service."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError, web

from .access import ServiceHub
from .crm import QueuedBitrix
from .domain import UserError, fmt, number, targets_for
from .networking import telegram_bot
from .reports import changes, csv_file, filename
from .service import Service
from .tabular import MAX_FILE, TableSheets

STATIC = Path(__file__).with_name("static")
OWNER = web.RequestKey("owner", int)
log = logging.getLogger(__name__)


def authenticate(init_data: str, token: str, allowed: set[int], now: int | None = None) -> int:
    try:
        if not init_data or len(init_data) > 16000:
            raise ValueError
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError
        given = values.pop("hash")
        text = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(given, expected):
            raise ValueError
        age = (int(time.time()) if now is None else now) - int(values["auth_date"])
        if not -30 <= age <= 3600:
            raise ValueError
        owner = json.loads(values["user"])["id"]
        if not isinstance(owner, int) or isinstance(owner, bool) or owner not in allowed:
            raise ValueError
        return owner
    except (ValueError, TypeError, KeyError):
        raise UserError("Откройте приложение из своего Telegram-бота. Доступ разрешён владельцу.") from None


def label(name: str) -> str:
    lower = name.lower()
    diameter = re.search(r"[дД](\d+(?:[.,]\d+)?)", name)
    if "фреонопровод медный" in lower and diameter:
        return "Медная труба · " + diameter[1].replace(".", ",") + " мм"
    if "трубка теплоизоляционная" in lower and diameter:
        return "Изоляция трубы · " + diameter[1].replace(".", ",") + " мм"
    cable = re.search(r"\b(\d+)[хx×](\d+(?:[.,]\d+)?)", lower)
    if "кабель силовой" in lower and cable:
        return "Кабель · " + cable[1] + "×" + cable[2].replace(".", ",")
    return name if len(name) <= 85 else name[:82].rstrip() + "…"


def state(service: Service, owner: int) -> dict:
    crm = service.bitrix.status() if isinstance(service.bitrix, QueuedBitrix) else None
    table_data = service.sheets.table.page() if isinstance(service.sheets, TableSheets) else None
    if table_data is not None and not table_data["mapping"]:
        return {
            **(service.overview(owner) if isinstance(service, ServiceHub) and owner > 0 else {}),
            "project": service.settings.project,
            "day": service.today(),
            "mode": "table",
            "type": "table",
            "table": table_data,
            "items": [],
            "draft": None,
            "warnings": [],
            "ai_available": bool(service.settings.llm_key),
            "speech_available": bool(os.getenv("SPEECH_URL")),
            "write_available": True,
            "sheet_url": "",
            "crm": crm,
        }
    snapshot = service.snapshot()
    catalog = snapshot.by_id()
    items = [
        {**asdict(item), "label": label(item.name), "remaining": str(item.remaining)}
        for item in snapshot.items
    ]
    if isinstance(service.sheets, TableSheets):
        for item in items:
            if item["id"] in service.sheets.unknown_plans:
                item["planned"] = None
                item["remaining"] = None
    draft = service.store.active(owner)
    draft_data = None
    if draft:
        draft_data = draft.to_dict()
        draft_data["ready"] = False
        draft_data["warning"] = None
        draft_data["over_budget"] = []
        try:
            targets = targets_for(draft, snapshot)
            snapshot.target_column()
            if draft.day != snapshot.day:
                raise UserError("Наступил новый день. Отмените старый черновик и создайте новый.")
            draft_data["ready"] = draft.status == "draft" and service.settings.mode != "setup"
            draft_data["totals"] = targets
            for key, target in targets.items():
                if table_data is not None and key in service.sheets.unknown_plans:
                    continue
                item = catalog[key]
                excess = (
                    Decimal(item.completed) - Decimal(item.today) + Decimal(target) - Decimal(item.planned)
                )
                if excess > 0:
                    draft_data["over_budget"].append(
                        f"{label(item.name)}: превышение сметы на {fmt(excess)} {item.unit}."
                    )
        except UserError as error:
            draft_data["warning"] = str(error)
        if service.settings.mode == "setup":
            draft_data["warning"] = "Google-таблица подключена. Запись включится после подключения Битрикс24."
    return {
        **(service.overview(owner) if isinstance(service, ServiceHub) and owner > 0 else {}),
        "project": service.settings.project,
        "type": "table" if table_data is not None else "work",
        "table": table_data,
        "speech_available": bool(os.getenv("SPEECH_URL")),
        "day": snapshot.day,
        "mode": service.settings.mode,
        "sheet_url": service.sheets.url,
        "crm": crm,
        "items": items,
        "draft": draft_data,
        "warnings": snapshot.warnings,
        "ai_available": bool(service.recognizer and service.settings.llm_key and service.settings.llm_model),
        "write_available": service.settings.mode != "setup",
        "bot_url": "https://t.me/" + os.getenv("TELEGRAM_BOT_USERNAME", ""),
    }


def action(service: Service, owner: int, data: dict) -> str:
    name = data.get("action")
    if name == "text":
        text = data.get("text")
        request_id = str(data.get("request_id", ""))
        if not isinstance(text, str) or not re.fullmatch(r"[a-f0-9-]{32,36}", request_id):
            raise UserError("Проверьте текст отчёта и повторите ввод.")
        mode = data.get("mode", "add")
        if mode not in {"add", "set"}:
            raise UserError("Неизвестный режим отчёта.")
        active = service.store.active(owner)
        if active and active.mode != mode:
            raise UserError("Сначала завершите открытый черновик с другим режимом.")
        if not active and mode == "set":
            service.new(owner, "set")
        service.report(owner, text, f"mini-text:{owner}:{request_id}")
        return "Работы распознаны. Проверьте список перед записью."
    if name == "add":
        key = str(data.get("item_id", ""))
        amount = str(number(data.get("quantity")))
        item = service.snapshot().by_id().get(key)
        if item is None:
            raise UserError("Позиция не найдена. Обновите список работ.")
        if item.unit == "шт" and Decimal(amount) % 1:
            raise UserError("Укажите целое количество в штуках.")
        request_id = str(data.get("request_id", ""))
        if not re.fullmatch(r"[a-f0-9-]{32,36}", request_id):
            raise UserError("Не удалось определить запрос. Обновите приложение.")
        source = f"mini:{owner}:{request_id}"
        if service.store.from_source(source, owner):
            return "Работа уже добавлена в черновик."
        mode = data.get("mode", "add")
        if mode not in {"add", "set"}:
            raise UserError("Неизвестный режим отчёта.")
        current = service.store.active(owner)
        if current and current.mode != mode:
            raise UserError("В черновике выбран другой режим. Завершите или отмените его.")
        if not current:
            service.new(owner, mode)
        service.manual(owner, key, amount, source)
        return "Работа добавлена в черновик."
    if name == "confirm":
        return service.confirm(owner, str(data.get("draft_id", "")), int(data.get("revision", 0)))
    if name == "retry":
        return service.resume(owner)
    current = service.editable(owner)
    if current.id != data.get("draft_id") or current.revision != data.get("revision"):
        raise UserError("Черновик изменился. Проверьте обновлённый список.")
    if name == "cancel":
        service.cancel(owner)
        return "Черновик отменён."
    if name in {"quantity", "delete", "pick"}:
        service.change(
            owner,
            int(data.get("index", 0)),
            name,
            str(data.get("item_id") if name == "pick" else data.get("quantity", "")),
        )
        return "Черновик обновлён."
    raise UserError("Неизвестное действие.")


def create_app(
    service: Service,
    lock: asyncio.Lock | None = None,
    preview_token: str = "",
    local_owner: int | None = None,
    local_port: int = 8765,
) -> web.Application:
    gate = lock or asyncio.Lock()
    voice_owners = set()

    def local_identity(request, secret):
        if (
            preview_token
            and (service.settings.mode == "local" or local_owner is not None)
            and request.host in {f"127.0.0.1:{local_port}", f"localhost:{local_port}"}
            and request.remote in {"127.0.0.1", "::1"}
            and not any(
                key in request.headers
                for key in (
                    "CF-Connecting-IP",
                    "X-Forwarded-For",
                    "Forwarded",
                    "CF-Ray",
                    "X-Forwarded-Host",
                    "X-Forwarded-Proto",
                )
            )
            and isinstance(secret, str)
            and hmac.compare_digest(secret, preview_token)
        ):
            return local_owner if local_owner is not None else -1
        return None

    @web.middleware
    async def guard(request, handler):
        try:
            if request.path.startswith("/api/") and request.path != "/api/speech":
                owner = None
                owner = local_identity(request, request.headers.get("X-Preview-Token", ""))
                if owner is None:
                    owner = authenticate(
                        request.headers.get("X-Telegram-Init-Data", ""),
                        service.settings.token,
                        service.access.allowed()
                        if isinstance(service, ServiceHub)
                        else service.settings.allowed,
                    )
                request[OWNER] = owner
                if request.method == "POST" and request.path not in {"/api/import/upload", "/api/voice"}:
                    limit = 256 * 1024 if request.path == "/api/table" else 32 * 1024
                    if len(await request.read()) > limit:
                        raise UserError("Запрос слишком большой. Сохраните изменения меньшими частями.")
                async with gate:
                    with service.scope(owner) if isinstance(service, ServiceHub) else nullcontext():
                        response = await handler(request)
            else:
                response = await handler(request)
        except UserError as error:
            response = web.json_response({"error": str(error)}, status=400)
        except (ValueError, TypeError, json.JSONDecodeError):
            response = web.json_response({"error": "Проверьте введённые данные."}, status=400)
        except web.HTTPException:
            raise
        except Exception as error:
            log.error("Mini App failed: %s", type(error).__name__)
            response = web.json_response(
                {"error": "Действие не завершено. Обновите отчёт перед повтором."}, status=500
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
            f"img-src 'self' data:; connect-src 'self' wss: ws://127.0.0.1:{local_port}; worker-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org"
        )
        return response

    app = web.Application(middlewares=[guard], client_max_size=20 * 1024 * 1024)

    async def index(request):
        if local_owner is not None:
            html = (STATIC / "index.html").read_text(encoding="utf-8")
            html = html.replace('<script src="https://telegram.org/js/telegram-web-app.js"></script>', "")
            return web.Response(text=html, content_type="text/html")
        return web.FileResponse(STATIC / "index.html")

    async def asset(request):
        name = request.match_info["name"]
        if name not in {
            "app.js",
            "app.css",
            "brand.css",
            "search.js",
            "admin.js",
            "tables.js",
            "voice.js",
            "pcm-worklet.js",
            "compact.css",
            "m100-logo.png",
        }:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def read(request):
        return web.json_response(await asyncio.to_thread(state, service, request[OWNER]))

    async def mutate(request):
        if request.content_type != "application/json":
            raise UserError("Ожидается запрос приложения.")
        data = await request.json()
        if not isinstance(data, dict):
            raise UserError("Некорректный запрос приложения.")
        if isinstance(service, ServiceHub) and data.get("context_project") != service.current_key():
            raise UserError("Выбран другой объект. Обновите экран перед изменением.")
        message = await asyncio.to_thread(action, service, request[OWNER], data)
        return web.json_response(
            {"message": message, "state": await asyncio.to_thread(state, service, request[OWNER])}
        )

    async def health(request):
        return web.json_response({"status": "ok"})

    def table_required(request):
        if not isinstance(service.sheets, TableSheets):
            raise UserError("Выберите импортированный табличный проект.")
        return service.sheets.table

    async def table_read(request):
        table = table_required(request)
        if request.path.endswith(".csv"):
            return web.Response(
                body=table.history_export() if request.path.endswith("history.csv") else table.export(),
                content_type="text/csv",
                charset="utf-8",
                headers={"Content-Disposition": 'attachment; filename="M100-table.csv"'},
            )
        if request.path.endswith("/history"):
            return web.json_response({"events": table.history(), "columns": table.meta("columns")})
        return web.json_response(
            await asyncio.to_thread(
                table.page, request.query.get("q", ""), int(request.query.get("offset", 0))
            )
        )

    async def table_action(request):
        table = table_required(request)
        data = await request.json()
        if not isinstance(data, dict):
            raise UserError("Проверьте данные записи.")
        if data.get("context_project") != service.current_key():
            raise UserError("Выбран другой проект. Обновите таблицу.")
        name = data.get("action")
        if service.store.pending():
            raise UserError("Сначала завершите незавершённую запись объёмов.")
        if name == "save":
            request_id = str(data.get("request_id", ""))
            if not re.fullmatch(r"[a-f0-9-]{32,36}", request_id):
                raise UserError("Обновите форму записи перед сохранением.")
            table.save(
                request[OWNER], data.get("values"), data.get("row_id"), data.get("revision"), request_id
            )
            message = "Запись сохранена."
        elif name == "delete":
            table.delete(request[OWNER], data.get("row_id"), data.get("revision"))
            message = "Запись удалена из таблицы."
        elif name == "configure":
            service.access.require_admin(request[OWNER])
            if service.store.pending():
                raise UserError("Сначала завершите незавершённые записи объёмов.")
            table.configure(data.get("mapping"))
            message = "Назначения колонок сохранены."
        else:
            raise UserError("Неизвестное действие с таблицей.")
        return web.json_response({"message": message, "state": state(service, request[OWNER])})

    async def import_upload(request):
        if not isinstance(service, ServiceHub):
            raise UserError("Импорт пока не настроен.")
        service.access.require_admin(request[OWNER])
        reader = await request.multipart()
        part = await reader.next()
        if not part or part.name != "file" or not part.filename:
            raise UserError("Выберите файл таблицы.")
        body = bytearray()
        while chunk := await part.read_chunk():
            body.extend(chunk)
            if len(body) > MAX_FILE:
                raise UserError("Размер файла ограничен 12 МБ.")
        result = await asyncio.to_thread(service.stage_import, request[OWNER], part.filename, bytes(body))
        return web.json_response(result)

    async def import_action(request):
        if not isinstance(service, ServiceHub):
            raise UserError("Импорт пока не настроен.")
        data = await request.json()
        if not isinstance(data, dict):
            raise UserError("Проверьте параметры импорта.")
        name = data.get("action")
        if name == "preview":
            result = service.import_preview(
                request[OWNER], data.get("token"), data.get("sheet", 0), data.get("header")
            )
        elif name == "google":
            result = await asyncio.to_thread(
                service.stage_google, request[OWNER], data.get("url"), data.get("sheet")
            )
        elif name == "finish":
            key = await asyncio.to_thread(service.finish_import, request[OWNER], data)
            result = {"project_id": key, "message": "Таблица импортирована как отдельный проект."}
        else:
            raise UserError("Неизвестное действие импорта.")
        return web.json_response(result)

    async def voice_file(request):
        audio = await request.read()
        if not audio or len(audio) > 19 * 1024 * 1024:
            raise UserError("Запись должна быть непустой и меньше 19 МБ.")
        text = await asyncio.to_thread(service.recognizer.transcribe, audio)
        return web.json_response({"text": text})

    async def speech(request):
        # Browser WebSockets cannot set arbitrary auth headers. Authenticate in the
        # first frame, never in URL parameters, before contacting the private ASR.
        ws = web.WebSocketResponse(max_msg_size=65536, heartbeat=20)
        await ws.prepare(request)
        owner = None
        reserved = False
        try:
            first = await asyncio.wait_for(ws.receive_json(), 5)
            owner = (
                local_identity(request, first.get("preview_token", "")) if local_owner is not None else None
            )
            if owner is None:
                owner = authenticate(
                    first.get("init_data", ""),
                    service.settings.token,
                    service.access.allowed() if isinstance(service, ServiceHub) else service.settings.allowed,
                )
            if owner in voice_owners:
                raise UserError("На другом устройстве уже идёт запись. Сначала остановите её.")
            voice_owners.add(owner)
            reserved = True
            if not os.getenv("SPEECH_URL"):
                raise UserError("Живое распознавание ещё не подключено.")
            endpoint = os.environ["SPEECH_URL"].rstrip("/") + "/stream"
            async with ClientSession(timeout=ClientTimeout(total=205, connect=10), trust_env=False) as client:
                async with client.ws_connect(endpoint, max_msg_size=65536) as remote:

                    async def incoming():
                        async for message in ws:
                            if owner not in (
                                service.access.allowed()
                                if isinstance(service, ServiceHub)
                                else service.settings.allowed
                            ):
                                raise UserError("Доступ к голосовому вводу отозван.")
                            if message.type == WSMsgType.BINARY:
                                await remote.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                control = json.loads(message.data)
                                if control.get("eof"):
                                    await remote.send_json({"eof": True})
                                    return
                            else:
                                return

                    async def outgoing():
                        async for message in remote:
                            if message.type == WSMsgType.TEXT:
                                data = json.loads(message.data)
                                await ws.send_json(data)
                                if data.get("final") or data.get("error"):
                                    return

                    up = asyncio.create_task(incoming())
                    down = asyncio.create_task(outgoing())
                    try:
                        done, _ = await asyncio.wait(
                            {up, down}, return_when=asyncio.FIRST_COMPLETED, timeout=190
                        )
                        if up in done and not down.done():
                            await asyncio.wait_for(down, 15)
                        for task in done:
                            task.result()
                    finally:
                        up.cancel()
                        down.cancel()
                        await asyncio.gather(up, down, return_exceptions=True)
        except UserError as error:
            await ws.send_json({"error": str(error)})
        except WSServerHandshakeError as error:
            await ws.send_json(
                {
                    "error": "Распознавание занято. Повторите через несколько секунд."
                    if error.status == 429
                    else "Сервис голоса временно недоступен. Можно ввести текст вручную."
                }
            )
        except Exception:
            if not ws.closed:
                await ws.send_json(
                    {"error": "Связь с распознаванием прервана. Уже полученный текст сохранён в поле."}
                )
        finally:
            if reserved:
                voice_owners.discard(owner)
            await ws.close()
        return ws

    async def access_context(request):
        if not isinstance(service, ServiceHub) or request[OWNER] < 0:
            return web.json_response({"is_admin": False, "projects": []})
        return web.json_response(service.overview(request[OWNER]))

    async def admin_panel(request):
        if not isinstance(service, ServiceHub):
            raise UserError("Управление пользователями не настроено.")
        return web.json_response(
            {
                **service.access.panel(request[OWNER]),
                "actor_id": request[OWNER],
                "google_service_account": service.google_email(),
                "crm": service.crm.status(),
            }
        )

    async def account_action(request):
        if not isinstance(service, ServiceHub):
            raise UserError("Управление объектами не настроено.")
        data = await request.json()
        if not isinstance(data, dict):
            raise UserError("Проверьте данные формы.")
        owner = request[OWNER]
        name = data.get("action")
        if name == "select":
            service.access.select(owner, str(data.get("project", "")))
            message = "Объект выбран."
        elif name == "project":
            message = await asyncio.to_thread(service.add_project, owner, data)
        elif name == "account":
            service.access.save_account(
                owner, data.get("id"), data.get("name"), data.get("role"), data.get("project")
            )
            message = "Доступ пользователя сохранён."
        elif name == "disable":
            service.access.disable(owner, data.get("id"))
            message = "Доступ отключён."
        elif name == "crm_connect":
            service.access.require_admin(owner)
            await asyncio.to_thread(service.crm.connect, data.get("webhook"), data.get("category", 0))
            message = "Битрикс24 подключён. Подтверждённые отчёты из очереди будут отправлены автоматически."
        elif name == "crm_sync":
            service.access.require_admin(owner)
            count = await asyncio.to_thread(service.sync_crm)
            message = f"Синхронизация выполнена. Отправлено отчётов: {count}."
        else:
            raise UserError("Неизвестное действие управления.")
        return web.json_response({"message": message})

    async def report(request):
        result = await asyncio.to_thread(
            changes, service, request[OWNER], request.query.get("start"), request.query.get("end")
        )
        if request.path.endswith(".csv"):
            return web.Response(
                body=csv_file(result),
                content_type="text/csv",
                charset="utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename(result)}"'},
            )
        return web.json_response(result)

    async def send_report(request):
        from aiogram.types import BufferedInputFile

        if request[OWNER] <= 0:
            raise UserError("Получить файл в чате можно при открытии приложения из Telegram.")
        data = await request.json()
        if not isinstance(data, dict):
            raise UserError("Проверьте период отчёта.")
        result = await asyncio.to_thread(changes, service, request[OWNER], data.get("start"), data.get("end"))
        async with telegram_bot(service.settings.token) as bot:
            await bot.send_document(
                request[OWNER],
                BufferedInputFile(csv_file(result), filename=filename(result)),
                caption="M-100 · изменения за выбранный период. CSV для Excel.",
            )
        return web.json_response({"message": "Файл отправлен вам в чат с ботом."})

    app.add_routes(
        [
            web.get("/", index),
            web.get(
                "/{name:app\\.(?:js|css)|brand\\.css|compact\\.css|search\\.js|admin\\.js|tables\\.js|voice\\.js|pcm-worklet\\.js|m100-logo\\.png}",
                asset,
            ),
            web.get("/health", health),
            web.get("/api/state", read),
            web.post("/api/action", mutate),
            web.get("/api/report", report),
            web.get("/api/report.csv", report),
            web.post("/api/report/send", send_report),
            web.get("/api/access", access_context),
            web.get("/api/admin", admin_panel),
            web.post("/api/access", account_action),
            web.get("/api/table", table_read),
            web.get("/api/table.csv", table_read),
            web.get("/api/table/history", table_read),
            web.get("/api/table/history.csv", table_read),
            web.post("/api/table", table_action),
            web.post("/api/import/upload", import_upload),
            web.post("/api/import", import_action),
            web.post("/api/voice", voice_file),
            web.get("/api/speech", speech),
        ]
    )
    return app


async def run(service: Service) -> None:
    from .bot import run as run_bot

    lock = asyncio.Lock()
    app = create_app(service, lock, os.getenv("MINIAPP_PREVIEW_TOKEN", ""))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    async def maintain_bot():
        while True:
            try:
                await run_bot(service, lock)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("Telegram connection unavailable: %s; retry in 30 seconds", type(error).__name__)
            await asyncio.sleep(30)

    async def maintain_crm():
        while True:
            await asyncio.sleep(60)
            if isinstance(service, ServiceHub):
                try:
                    async with lock:
                        await asyncio.to_thread(service.sync_crm)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.error("CRM synchronization unavailable: %s", type(error).__name__)

    bot_task = None
    crm_task = None
    try:
        await web.TCPSite(runner, "127.0.0.1", 8765).start()
        print("Mini App: http://127.0.0.1:8765", flush=True)
        # Outbound Telegram failures must not take down the HTTPS application.
        bot_task = asyncio.create_task(maintain_bot())
        crm_task = asyncio.create_task(maintain_crm())
        await asyncio.Event().wait()
    finally:
        if bot_task:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        if crm_task:
            crm_task.cancel()
            await asyncio.gather(crm_task, return_exceptions=True)
        await runner.cleanup()
