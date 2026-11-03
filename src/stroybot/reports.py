"""Owner-scoped change journal; CSV opens in Russian Excel without conversion."""

import csv
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from .crm import QueuedBitrix
from .domain import UserError, fmt


def period(today: str, start: str | None = None, end: str | None = None) -> tuple[str, str]:
    try:
        last = date.fromisoformat(end or today)
        first = date.fromisoformat(start) if start else last - timedelta(days=30)
        if first > last or (last - first).days > 365:
            raise ValueError
    except (ValueError, TypeError):
        raise UserError("Выберите период до 366 дней: начало должно быть не позже окончания.") from None
    return first.isoformat(), last.isoformat()


def changes(service, owner: int, start: str | None = None, end: str | None = None) -> dict:
    start, end = period(service.today(), start, end)
    rows = []
    stages = {
        "done": "Завершено",
        "sheets": "Таблица записана, Битрикс24 ожидает",
        "prepared": "Запись не подтверждена",
    }
    history_owner = None if hasattr(service, "access") and service.access.admin(owner) else owner
    for op in service.store.history(history_owner, start, end):
        payload = op["payload"]
        # New operations preserve the names and units at the time of confirmation.
        positions = payload.get("changes")
        if positions is None:
            positions = [
                dict(item, before=payload["expected"][item["id"]], after=payload["targets"][item["id"]])
                for item in payload["report"]["positions"]
                if item["id"] in payload["targets"]
            ]
            known = {item["id"] for item in positions}
            # Older zero corrections may lack a name; preserve their numeric evidence.
            positions += [
                dict(
                    id=key,
                    code="",
                    section="",
                    name="Позиция " + key,
                    unit="",
                    before=payload["expected"][key],
                    after=value,
                )
                for key, value in payload["targets"].items()
                if key not in known
            ]
        for item in positions:
            before, after = Decimal(item["before"]), Decimal(item["after"])
            rows.append(
                {
                    "date": payload["day"],
                    "author": str(op["owner"]),
                    "at": payload.get("confirmed_at", ""),
                    "operation": op["id"],
                    "status": (
                        "Таблица сохранена, CRM ожидает"
                        if op["stage"] == "done"
                        and isinstance(service.bitrix, QueuedBitrix)
                        and (
                            service.store.db.execute(
                                "SELECT status FROM crm_outbox WHERE key=?", (payload["key"],)
                            ).fetchone()
                            or ["pending"]
                        )[0]
                        != "sent"
                        else stages[op["stage"]]
                    ),
                    "stage": op["stage"],
                    "mode": "Исправление" if payload.get("mode") == "set" else "Добавление",
                    "code": item["code"],
                    "section": item["section"],
                    "name": item["name"],
                    "unit": item["unit"],
                    "before": format(before.normalize(), "f"),
                    "after": format(after.normalize(), "f"),
                    "delta": format((after - before).normalize(), "f"),
                }
            )
            if len(rows) > 10000:
                raise UserError("В периоде больше 10 000 изменений. Выберите меньший период.")
    return {
        "project": service.settings.project,
        "mode": service.settings.mode,
        "start": start,
        "end": end,
        "rows": rows,
    }


def safe_text(value) -> str:
    value = str(value)
    return (
        "'" + value
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
        else value
    )


def csv_file(report: dict) -> bytes:
    out = StringIO(newline="")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Объект",
            "Telegram ID сотрудника",
            "Режим",
            "Дата работ",
            "Время подтверждения",
            "Операция",
            "Статус",
            "Действие",
            "Код",
            "Раздел",
            "Работа",
            "Ед.",
            "Было за день",
            "Изменение",
            "Стало за день",
        ]
    )
    for row in report["rows"]:
        writer.writerow(
            [
                safe_text(report["project"]),
                row["author"],
                "Тестовый" if report["mode"] == "local" else "Рабочий",
                *[
                    safe_text(row[key])
                    for key in (
                        "date",
                        "at",
                        "operation",
                        "status",
                        "mode",
                        "code",
                        "section",
                        "name",
                        "unit",
                    )
                ],
                fmt(row["before"]),
                fmt(row["delta"]),
                fmt(row["after"]),
            ]
        )
    return out.getvalue().encode("utf-8-sig")


def filename(report: dict) -> str:
    return f"M100-changes-{report['start']}-{report['end']}.csv"
