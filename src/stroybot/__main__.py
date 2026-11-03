import argparse
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

import httpx

from .config import Settings
from .domain import UserError
from .gateways import Bitrix24, GoogleSheets, LocalBitrix, LocalSheets
from .networking import ai_client
from .recognition import Recognizer
from .service import Service
from .store import ProcessLock, Store


def assemble(settings: Settings, client: httpx.Client, recognition_client=None) -> Service:
    settings.validate()
    sheets = (
        GoogleSheets(settings, client)
        if settings.mode in {"setup", "live", "google"}
        else LocalSheets(state=Path(os.getenv("LOCAL_STATE_PATH", "data/local-state.json")))
    )
    bitrix = (
        Bitrix24(settings, client)
        if settings.mode == "live"
        else LocalBitrix(Path(os.getenv("LOCAL_BITRIX_PATH", "data/local-bitrix.json")))
    )
    return Service(
        settings, Store(settings.database), sheets, bitrix, Recognizer(settings, recognition_client or client)
    )


def demo(settings: Settings) -> None:
    """Explicit manual scenario, independent of credentials and paid services."""
    settings.mode = "local"
    with tempfile.TemporaryDirectory(prefix="stroybot-") as folder:
        root = Path(folder)
        service = Service(
            settings,
            Store(root / "demo.sqlite3"),
            LocalSheets(state=root / "sheets.json"),
            LocalBitrix(root / "bitrix.json"),
        )
        try:
            draft = service.manual(1, "13", "15")
            draft = service.manual(1, "14", "15")
            draft = service.manual(1, "16", "15")
            draft = service.manual(1, "17", "15")
            draft = service.manual(1, "51", "56")
            draft = service.manual(1, "40", "10")
            print(service.preview(draft))
            print("\nДемонстрационное подтверждение:")
            print(service.confirm(1, draft.id, draft.revision))
            print(service.confirm(1, draft.id, draft.revision))
            service.new(1, "set")
            correction = service.manual(1, "51", "50")
            print("\nИсправление кабеля с 56 до 50:")
            print(service.confirm(1, correction.id, correction.revision))
            print("\nЛокальный аналог записи Битрикс24:")
            print((root / "bitrix.json").read_text(encoding="utf-8"))
        finally:
            service.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Дневные строительные отчёты")
    parser.add_argument("command", choices=["demo", "check", "bot", "miniapp"], nargs="?", default="check")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    # Third-party HTTP exception text can contain credentials in request URLs.
    logging.getLogger("aiogram").addHandler(logging.NullHandler())
    logging.getLogger("aiogram").propagate = False
    logging.getLogger("httpx").disabled = True
    try:
        settings = Settings.load()
        if args.command == "demo":
            demo(settings)
            return
        settings.validate(telegram=args.command in {"bot", "miniapp"})
        lock = ProcessLock(settings.database.with_suffix(".lock"))
        try:
            with (
                httpx.Client(timeout=httpx.Timeout(90, connect=15), trust_env=False) as client,
                ai_client() as recognition_client,
            ):
                service = assemble(settings, client, recognition_client)
                try:
                    if args.command == "check":
                        snapshot = service.snapshot()
                        bitrix_status = "локальное хранилище"
                        if settings.mode == "setup":
                            bitrix_status = "не подключён; запись отключена"
                        if settings.mode == "live":
                            service.bitrix.call("crm.deal.fields", {})
                            bitrix_status = "CRM API доступен (запись не проверялась)"
                        print(
                            json.dumps(
                                {
                                    "mode": settings.mode,
                                    "positions": len(snapshot.items),
                                    "date": snapshot.day,
                                    "warnings": snapshot.warnings,
                                    "bitrix": bitrix_status,
                                    "llm_configured": bool(settings.llm_key and settings.llm_model),
                                    "telegram_configured": bool(settings.token and settings.allowed),
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                    elif args.command == "bot":
                        from .access import Access, ServiceHub
                        from .bot import run

                        hub = ServiceHub(
                            service,
                            client,
                            Access(
                                Path(
                                    os.getenv(
                                        "ADMIN_DATABASE_PATH",
                                        str(settings.database.parent / "access.sqlite3"),
                                    )
                                ),
                                settings,
                            ),
                        )
                        try:
                            asyncio.run(run(hub))
                        finally:
                            hub.close()
                    else:
                        from .access import Access, ServiceHub
                        from .miniapp import run

                        hub = ServiceHub(
                            service,
                            client,
                            Access(
                                Path(
                                    os.getenv(
                                        "ADMIN_DATABASE_PATH",
                                        str(settings.database.parent / "access.sqlite3"),
                                    )
                                ),
                                settings,
                            ),
                        )
                        try:
                            asyncio.run(run(hub))
                        finally:
                            hub.close()
                finally:
                    service.store.close()
        finally:
            lock.close()
    except UserError as error:
        print(str(error))
        raise SystemExit(1) from None
    except Exception as error:
        print(f"Не удалось запустить: {type(error).__name__}. Проверьте настройки и зависимости.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
