from __future__ import annotations

import json
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from .config import Settings
from .domain import Snapshot, UserError, column, number, parse_table


class Sheets(Protocol):
    url: str

    def read(self, day: str) -> Snapshot: ...
    def apply(self, day: str, expected: dict[str, str], target: dict[str, str]) -> None: ...


class Bitrix(Protocol):
    def upsert(self, key: str, title: str, report: dict) -> None: ...


def check_values(snapshot: Snapshot, expected: dict[str, str], target: dict[str, str]) -> bool:
    """All target values means a previous timed-out request already succeeded."""
    catalog = snapshot.by_id()
    if any(key not in catalog for key in target):
        raise UserError("Позиции в таблице изменились. Нужна ручная сверка незавершённой операции.")
    snapshot.target_column()
    if snapshot.date_columns.get(snapshot.day) and all(
        Decimal(catalog[key].today) == Decimal(value) for key, value in target.items()
    ):
        return True
    if any(Decimal(catalog[key].today) != Decimal(expected[key]) for key in target):
        raise UserError("Объёмы изменились после подтверждения. Запись остановлена для сверки.")
    return False


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class LocalSheets:
    url = ""

    def __init__(self, seed: Path = Path(__file__).parent / "assets/catalog.json", state: Path = Path("data/local-state.json")):
        self.seed, self.state = seed, state

    def rows(self) -> list[list]:
        return json.loads((self.state if self.state.exists() else self.seed).read_text(encoding="utf-8"))

    def read(self, day: str) -> Snapshot:
        return parse_table(self.rows(), day)

    def apply(self, day: str, expected: dict[str, str], target: dict[str, str]) -> None:
        snapshot = self.read(day)
        if check_values(snapshot, expected, target):
            return
        col, _ = snapshot.target_column()
        rows = self.rows()
        for row in rows:
            row.extend([None] * max(0, col - len(row)))
        rows[2][col - 1] = day
        catalog = snapshot.by_id()
        for key, value in target.items():
            rows[catalog[key].row - 1][col - 1] = float(Decimal(value))
        atomic_json(self.state, rows)


class LocalBitrix:
    def __init__(self, path: Path = Path("data/local-bitrix.json")):
        self.path = path

    def upsert(self, key: str, title: str, report: dict) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        data[key] = {"title": title, "report": report}
        atomic_json(self.path, data)


class GoogleSheets:
    def __init__(self, settings: Settings, client: httpx.Client):
        from google.oauth2.service_account import Credentials

        self.credentials = Credentials.from_service_account_file(
            settings.credentials, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self.client, self.sheet = client, settings.sheet
        self.root = f"https://sheets.googleapis.com/v4/spreadsheets/{settings.spreadsheet}"
        self.url = f"https://docs.google.com/spreadsheets/d/{settings.spreadsheet}/edit"
        self.sheet_id = 0
        self.grid_columns = 0

    def request(self, method: str, path: str = "", **kwargs) -> dict:
        from google.auth.transport.requests import Request

        try:
            if not self.credentials.valid:
                self.credentials.refresh(Request())
            # Only safe reads are automatically retried. A write timeout is reconciled by readback.
            for attempt in range(3):
                response = self.client.request(
                    method,
                    self.root + path,
                    headers={"Authorization": f"Bearer {self.credentials.token}"},
                    **kwargs,
                )
                if method == "GET" and response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(0.3 * 2**attempt)
                    continue
                if response.status_code >= 400:
                    raise UserError(f"Google Sheets вернул ошибку {response.status_code}. Проверьте доступ.")
                return response.json()
        except UserError:
            raise
        except Exception:
            raise UserError("Нет подтверждения от Google Sheets. Повторите синхронизацию кнопкой.") from None
        raise UserError("Google Sheets временно недоступен.")

    def check_write_access(self):
        try:
            marker = "m100-permission-check-" + uuid.uuid4().hex
            # A valid write-permission check that cannot change a cell: even a
            # coincidental match would be replaced with the identical string.
            self.request(
                "POST",
                ":batchUpdate",
                json={
                    "requests": [
                        {
                            "findReplace": {
                                "find": marker,
                                "replacement": marker,
                                "sheetId": self.sheet_id,
                                "matchCase": True,
                                "matchEntireCell": True,
                                "searchByRegex": False,
                                "includeFormulas": False,
                            }
                        }
                    ]
                },
            )
        except UserError as error:
            if "403" in str(error):
                raise UserError(
                    "Таблица доступна только для чтения. Выдайте права редактора аккаунту "
                    + self.credentials.service_account_email
                    + " и повторите подключение."
                ) from None
            raise

    def read(self, day: str) -> Snapshot:
        meta = self.request("GET", params={"fields": "sheets(properties)"})
        matches = [s["properties"] for s in meta.get("sheets", []) if s["properties"]["title"] == self.sheet]
        if not matches:
            raise UserError("Лист не найден. Проверьте GOOGLE_SHEET_NAME.")
        self.sheet_id = matches[0]["sheetId"]
        self.grid_columns = matches[0]["gridProperties"]["columnCount"]
        sheet_range = "'" + self.sheet.replace("'", "''") + "'"
        data = self.request(
            "GET",
            "/values/" + quote(sheet_range, safe=""),
            params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"},
        )
        return parse_table(data.get("values", []), day)

    def apply(self, day: str, expected: dict[str, str], target: dict[str, str]) -> None:
        snapshot = self.read(day)
        if check_values(snapshot, expected, target):
            return
        col, new = snapshot.target_column()
        requests = []
        needed = max(104, col) if new else col
        if needed > self.grid_columns:
            requests.append(
                {
                    "appendDimension": {
                        "sheetId": self.sheet_id,
                        "dimension": "COLUMNS",
                        "length": needed - self.grid_columns,
                    }
                }
            )

        def write(row: int, c: int, value: dict, fields: str = "userEnteredValue") -> None:
            requests.append(
                {
                    "updateCells": {
                        "start": {"sheetId": self.sheet_id, "rowIndex": row - 1, "columnIndex": c - 1},
                        "rows": [{"values": [value]}],
                        "fields": fields,
                    }
                }
            )

        if new:
            serial = (date.fromisoformat(day) - date(1899, 12, 30)).days
            write(
                3,
                col,
                {
                    "userEnteredValue": {"numberValue": serial},
                    "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"}},
                },
                "userEnteredValue,userEnteredFormat.numberFormat",
            )
            write(4, col, {"userEnteredValue": {"stringValue": "объем"}})
            # Preserve original J:CZ coverage and extend it once the dates go beyond CZ.
            end = column(max(104, col))
            for item in snapshot.items:
                write(
                    item.row, 9, {"userEnteredValue": {"formulaValue": f"=SUM(J{item.row}:{end}{item.row})"}}
                )
        for key, value in target.items():
            write(snapshot.by_id()[key].row, col, {"userEnteredValue": {"numberValue": float(number(value))}})
        self.request("POST", ":batchUpdate", json={"requests": requests})
        verify = self.read(day).by_id()
        if any(Decimal(verify[k].today) != Decimal(v) for k, v in target.items()):
            raise UserError("Google Sheets не подтвердил значения. Нужна повторная синхронизация.")


class Bitrix24:
    """One standard CRM deal per project/day. No paid smart-process dependency."""

    def __init__(self, settings: Settings, client: httpx.Client):
        self.root, self.category, self.client = settings.bitrix_url, settings.category, client

    def call(self, method: str, payload: dict) -> dict:
        try:
            response = self.client.post(self.root + method + ".json", json=payload)
            if response.status_code >= 400:
                raise UserError(f"Битрикс24 вернул HTTP {response.status_code}.")
            result = response.json()
            if result.get("error"):
                # Do not expose webhook URLs, response bodies, or credentials in errors/logs.
                raise UserError("Битрикс24 отклонил запрос. Проверьте CRM-права вебхука и воронку.")
            return result
        except UserError:
            raise
        except Exception:
            raise UserError("Нет подтверждения от Битрикс24. Повторите синхронизацию.") from None

    def upsert(self, key: str, title: str, report: dict) -> None:
        found = self.call(
            "crm.deal.list",
            {"filter": {"=ORIGINATOR_ID": "stroybot", "=ORIGIN_ID": key}, "select": ["ID", "COMMENTS"]},
        ).get("result", [])
        if len(found) > 1:
            raise UserError("В Битрикс24 несколько записей на этот день. Нужна ручная сверка.")
        if "positions" in report:
            comments = "\n".join(
                [
                    "Ежедневный отчёт M-100",
                    "Объект: " + str(report.get("project", "")),
                    "Дата: " + str(report.get("date", "")),
                    *(["Таблица: " + report["sheet_url"]] if report.get("sheet_url") else []),
                    "",
                    *[
                        f"{item.get('code', '')}. {item['name']} — {item['quantity']} {item['unit']}"
                        for item in report["positions"]
                    ],
                    *(["За этот день выполненные объёмы равны нулю."] if not report["positions"] else []),
                ]
            )
        else:
            comments = json.dumps(report, ensure_ascii=False, indent=2)
        fields = {"TITLE": title, "COMMENTS": comments, "ORIGINATOR_ID": "stroybot", "ORIGIN_ID": key}
        if found:
            deal_id = found[0]["ID"]
            self.call("crm.deal.update", {"id": deal_id, "fields": fields})
        else:
            created = self.call("crm.deal.add", {"fields": {**fields, "CATEGORY_ID": self.category}})
            deal_id = created.get("result")
            if not deal_id:
                raise UserError("Битрикс24 не вернул ID созданной записи.")
        saved = self.call("crm.deal.get", {"id": deal_id}).get("result", {})
        if saved.get("COMMENTS") != comments:
            raise UserError("Битрикс24 не подтвердил состав отчёта. Повторите синхронизацию.")
