"""Portable local application; Telegram polling is a separate explicit mode."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

import httpx
from aiohttp import web
from dotenv import load_dotenv

from .access import Access, ServiceHub
from .config import Settings
from .domain import UserError
from .gateways import Bitrix24, GoogleSheets, LocalBitrix, LocalSheets
from .miniapp import create_app
from .networking import ai_client
from .recognition import Recognizer
from .service import Service
from .store import ProcessLock, Store

ROOT = Path(__file__).resolve().parents[2]


def account_settings():
    path = ROOT / ".local/account.env"
    if not path.is_file():
        raise UserError(
            "Сначала создайте .local/account.env по образцу .env.example и заполните подключения."
        )
    load_dotenv(path, override=True)
    # Resolve private data consistently even when started from a different directory.
    settings = Settings.load()
    settings.database = (ROOT / ".local/account/data.sqlite3").resolve()
    if not Path(settings.credentials).is_absolute():
        settings.credentials = str(ROOT / settings.credentials)
    settings.validate()
    if not settings.allowed:
        raise UserError("Укажите свой Telegram ID в ALLOWED_USER_IDS и ADMIN_USER_IDS.")
    return settings


def demo_settings():
    for key in ("TELEGRAM_PROXY_URL", "AI_PROXY_URL", "ADMIN_USER_IDS", "APP_ENV"):
        os.environ.pop(key, None)
    os.environ["ADMIN_USER_IDS"] = "1"
    return replace(
        Settings.load(),
        mode="local",
        token="local-demo-token",
        allowed={1},
        project="M-100 · демонстрация",
        database=ROOT / ".local/demo/data.sqlite3",
        spreadsheet="",
        credentials="",
        bitrix_url="/",
        llm_key="",
        llm_model="",
    )


def assemble(settings, client, recognition_client):
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    sheets = (
        GoogleSheets(settings, client)
        if settings.mode in {"google", "setup", "live"}
        else LocalSheets(state=settings.database.parent / "sheet.json")
    )
    service = Service(
        settings,
        Store(settings.database),
        sheets,
        Bitrix24(settings, client)
        if settings.mode == "live"
        else LocalBitrix(settings.database.parent / "local-crm.json"),
        Recognizer(settings, recognition_client),
    )
    # ServiceHub supplies the durable real CRM queue for google mode.
    return ServiceHub(service, client, Access(settings.database.parent / "access.sqlite3", settings))


def seed_demo(hub, owner):
    if any(p["kind"] == "dataset" for p in hub.access.projects(owner)):
        return
    data = "Название;Единица;План;Примечание\nМонтаж кабеля;м;150;Демонстрационные данные\nУстановка светильников;шт;24;Первый этаж\nМаркировка линий;шт;40;После проверки"
    preview = hub.stage_import(owner, "Пример проекта.csv", data.encode())
    hub.finish_import(owner, {"token": preview["token"], "name": "Офис · пример", "sheet": 0, "header": 0})
    with hub.scope(owner):
        hub.sheets.table.configure({"name": "c1", "unit": "c2", "planned": "c3"})


async def run_ui(hub, owner, args):
    token = secrets.token_urlsafe(32)
    gate = asyncio.Lock()
    app = create_app(hub, gate, preview_token=token, local_owner=owner, local_port=args.port)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    tasks = []

    async def sync_crm():
        while True:
            await asyncio.sleep(60)
            async with gate:
                try:
                    await asyncio.to_thread(hub.sync_crm)
                except UserError:
                    pass

    try:
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        url = f"http://127.0.0.1:{args.port}/#preview={token}"
        # Keep the local-session capability out of terminal history and logs.
        session_file = hub.base.settings.database.parent / "session.json"
        session_file.write_text(json.dumps({"url": url}), encoding="utf-8")
        session_file.chmod(0o600)
        print(f"M-100 открыт: http://127.0.0.1:{args.port}/", flush=True)
        print(
            "Режим: ваш аккаунт." if args.account else "Режим: демонстрация на вымышленных данных.",
            flush=True,
        )
        print("Для остановки нажмите Ctrl+C.", flush=True)
        if not args.no_browser:
            await asyncio.to_thread(webbrowser.open, url)
        tasks.append(asyncio.create_task(sync_crm()))
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        session_file = hub.base.settings.database.parent / "session.json"
        session_file.unlink(missing_ok=True)


def start_voice():
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8766))
        except OSError:
            raise UserError(
                "Голосовой режим уже используется. Закройте предыдущую голосовую копию."
            ) from None
    process = subprocess.Popen([sys.executable, "-m", "stroybot.voice_service"], cwd=ROOT)
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            for _ in range(40):
                if process.poll() is not None:
                    raise UserError("Не удалось включить голос. Повторите запуск с --voice.")
                try:
                    if client.get("http://127.0.0.1:8766/health").json().get("engine") == "vosk":
                        return process
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.25)
        raise UserError("Голос не успел подготовиться. Попробуйте запустить приложение ещё раз.")
    except BaseException:
        stop_voice(process)
        raise


def stop_voice(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # The Windows venv launcher has a child interpreter; stop our own tree.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    parser = argparse.ArgumentParser(description="M-100 · проекты и ежедневная отчётность")
    parser.add_argument("--account", action="store_true", help="Локально открыть ваш настроенный аккаунт")
    parser.add_argument("--telegram", action="store_true", help="Включить получение сообщений Telegram")
    parser.add_argument("--voice", action="store_true", help="Локальное живое распознавание русского голоса")
    parser.add_argument("--check", action="store_true", help="Проверить конфигурацию без изменения данных")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Порт должен быть от 1024 до 65535.")
    os.chdir(ROOT)
    voice_process = None
    try:
        settings = account_settings() if args.account or args.telegram else demo_settings()
        if not args.voice:
            os.environ.pop("SPEECH_URL", None)
        else:
            os.environ["STT_PROVIDER"] = "vosk"
            os.environ["SPEECH_URL"] = "http://127.0.0.1:8766"
            os.environ["VOSK_MODEL_PATH"] = str(ROOT / ".local/models/vosk-model-small-ru-0.22")
        with (
            ProcessLock(settings.database.with_suffix(".lock")),
            httpx.Client(timeout=httpx.Timeout(90, connect=15), trust_env=False) as client,
            ai_client() as recognition_client,
        ):
            if args.voice:
                voice_process = start_voice()
            hub = assemble(settings, client, recognition_client)
            try:
                owner = min(hub.access.roots)
                if not args.account and not args.telegram:
                    seed_demo(hub, owner)
                if args.check:
                    with hub.scope(owner):
                        snapshot = hub.snapshot()
                        if isinstance(hub.sheets, GoogleSheets):
                            hub.sheets.check_write_access()
                        print(
                            json.dumps(
                                {
                                    "status": "ok",
                                    "positions": len(snapshot.items),
                                    "mode": hub.settings.mode,
                                    "ai_configured": bool(settings.llm_key),
                                    "crm_configured": hub.crm.status()["configured"],
                                },
                                ensure_ascii=False,
                            )
                        )
                elif args.telegram:
                    from .miniapp import run

                    asyncio.run(run(hub))
                else:
                    asyncio.run(run_ui(hub, owner, args))
            finally:
                hub.close()
                hub.base.store.close()
    except KeyboardInterrupt:
        print("\nM-100 остановлен.")
    except UserError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except OSError as error:
        if getattr(error, "errno", None) in {98, 48, 10048}:
            print("Порт занят. Закройте предыдущую копию или укажите --port 8767.", file=sys.stderr)
        else:
            print("Не удалось открыть приложение. Проверьте доступ к папке и подключения.", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        stop_voice(voice_process)


if __name__ == "__main__":
    main()
