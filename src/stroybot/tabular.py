"""Schema-independent imports, versioned records and optional work accounting."""

import csv
import hashlib
import json
import math
import re
import sqlite3
import uuid
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path

from .domain import Item, Snapshot, UserError, number, unit
from .gateways import check_values
from .reports import safe_text

MAX_ROWS = 5000
MAX_COLS = 100
MAX_CELLS = 250000
MAX_FILE = 12 * 1024 * 1024


def clean(value):
    if value is None or isinstance(value, (str, bool, int)):
        result = value
    elif isinstance(value, float):
        result = value if math.isfinite(value) else str(value)
    elif isinstance(value, (datetime, date)):
        result = value.isoformat()
    else:
        result = str(value)
    if isinstance(result, str) and len(result) > 65536:
        raise UserError("В одной ячейке больше 65 536 символов. Сократите её текст.")
    return result


def bounded(rows):
    result = []
    total = 0
    blank = 0
    for row in rows:
        values = [clean(v) for v in row]
        while values and values[-1] is None:
            values.pop()
        if not any(v not in ("", None) for v in values):
            blank += 1
            continue
        total += len(values)
        if len(result) + blank >= MAX_ROWS or len(values) > MAX_COLS or total > MAX_CELLS:
            raise UserError(
                "Для одного листа доступны 5 000 строк, 100 колонок и 250 000 ячеек. Данные не обрезаны — разделите файл."
            )
        result.extend([[] for _ in range(blank)])
        blank = 0
        result.append(values)
    return result


def read_file(filename: str, data: bytes) -> list[dict]:
    if not data or len(data) > MAX_FILE:
        raise UserError("Выберите непустой файл размером до 12 МБ.")
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in {".csv", ".tsv", ".txt"}:
            try:
                text = (
                    data.decode("utf-16")
                    if data.startswith((b"\xff\xfe", b"\xfe\xff"))
                    else data.decode("utf-8-sig")
                )
            except UnicodeDecodeError:
                text = data.decode("cp1251")
            try:
                delimiter = csv.Sniffer().sniff(text[:16000], delimiters=";,\t|").delimiter
            except csv.Error:
                delimiter = "\t" if suffix == ".tsv" else ";" if text.count(";") > text.count(",") else ","
            return [{"name": "Данные", "rows": bounded(csv.reader(StringIO(text), delimiter=delimiter))}]
        if suffix == ".json":
            raw = json.loads(data)
            if not isinstance(raw, list):
                raise UserError("JSON должен содержать массив строк или объектов.")
            if all(isinstance(r, dict) for r in raw):
                keys = list(dict.fromkeys(str(k) for row in raw for k in row))
                return [
                    {
                        "name": "Данные",
                        "rows": bounded(
                            [keys]
                            + [
                                [
                                    json.dumps(r.get(k), ensure_ascii=False)
                                    if isinstance(r.get(k), (dict, list))
                                    else r.get(k)
                                    for k in keys
                                ]
                                for r in raw
                            ]
                        ),
                    }
                ]
            if not all(isinstance(r, list) for r in raw):
                raise UserError("В JSON ожидается массив объектов или массивов.")
            return [{"name": "Данные", "rows": bounded(raw)}]
        if suffix in {".xlsx", ".xlsm", ".ods"}:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                if sum(i.file_size for i in archive.infolist()) > 48 * 1024 * 1024:
                    raise UserError("После распаковки файл слишком большой. Разделите его на части.")
            if suffix == ".ods":
                return read_ods(data)
            import openpyxl

            workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
            formulas = openpyxl.load_workbook(
                BytesIO(data), read_only=True, data_only=False, keep_links=False
            )
            try:
                if len(workbook.worksheets) > 25:
                    raise UserError("В книге больше 25 листов. Выберите меньшую книгу.")
                result = []
                for sheet, source in zip(workbook.worksheets, formulas.worksheets):
                    # Excel can store inflated formatting bounds. Stream actual cells,
                    # bounded by the importer; never allocate its declared full grid.
                    sheet.reset_dimensions()
                    source.reset_dimensions()

                    def values():
                        for cached, original in zip(
                            sheet.iter_rows(values_only=True), source.iter_rows(values_only=True)
                        ):
                            yield [
                                v
                                if v is not None
                                else (f if isinstance(f, str) and f.startswith("=") else None)
                                for v, f in zip(cached, original)
                            ]

                    result.append({"name": sheet.title, "rows": bounded(values())})
                return result
            finally:
                workbook.close()
                formulas.close()
        if suffix == ".xls":
            import xlrd

            book = xlrd.open_workbook(file_contents=data, on_demand=True)
            try:
                if book.nsheets > 25:
                    raise UserError("В книге больше 25 листов.")
                return [
                    {"name": s.name, "rows": bounded(s.row_values(r) for r in range(s.nrows))}
                    for s in book.sheets()
                ]
            finally:
                book.release_resources()
    except UserError:
        raise
    except (Exception,):
        # Never return parser details, local paths or uploaded XML to the user.
        raise UserError(
            "Файл не удалось прочитать. Проверьте, что он открывается без пароля, и попробуйте сохранить копию в XLSX или CSV."
        ) from None
    raise UserError(
        "Поддерживаются XLSX, XLS, XLSM, ODS, CSV, TSV, TXT и JSON. Файл PDF или фотографию сначала нужно преобразовать в таблицу."
    )


def read_ods(data):
    from defusedxml import ElementTree as ET

    ns = {
        "t": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
        "x": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
        "o": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    }
    with zipfile.ZipFile(BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("content.xml"))
    result = []
    for sheet in root.findall(".//t:table", ns):
        if len(result) >= 25:
            raise UserError("В книге больше 25 листов.")
        rows = []
        for source in sheet.findall("t:table-row", ns):
            row = []
            for cell in source:
                if cell.tag not in {f"{{{ns['t']}}}table-cell", f"{{{ns['t']}}}covered-table-cell"}:
                    continue
                value = (
                    cell.get(f"{{{ns['o']}}}value")
                    or cell.get(f"{{{ns['o']}}}date-value")
                    or cell.get(f"{{{ns['o']}}}boolean-value")
                )
                if value is None:
                    value = "\n".join("".join(p.itertext()) for p in cell.findall("x:p", ns)) or None
                repeat = int(cell.get(f"{{{ns['t']}}}number-columns-repeated", "1"))
                if repeat > MAX_COLS:
                    if value is None:
                        break
                    raise UserError("В строке больше 100 колонок.")
                row.extend([value] * repeat)
            count = int(source.get(f"{{{ns['t']}}}number-rows-repeated", "1"))
            if count > MAX_ROWS:
                if all(v is None for v in row):
                    break
                raise UserError("В листе больше 5 000 строк.")
            if len(rows) + count > MAX_ROWS:
                raise UserError("В листе больше 5 000 строк.")
            rows.extend([row.copy() for _ in range(count)])
        result.append({"name": sheet.get(f"{{{ns['t']}}}name", "Данные"), "rows": bounded(rows)})
    return result


def detect_header(rows):
    if not rows:
        return -1

    def score(pair):
        index, row = pair
        text = [str(v).strip() for v in row if isinstance(v, str) and v.strip()]
        filled = sum(v not in ("", None) for v in row)
        numeric = sum(
            isinstance(v, (int, float))
            or isinstance(v, str)
            and bool(re.fullmatch(r"-?\d+(?:[.,]\d+)?", v.strip()))
            for v in row
        )
        common = (
            "имя",
            "фио",
            "город",
            "телефон",
            "статус",
            "дата",
            "наименование",
            "название",
            "количество",
            "сумма",
            "артикул",
            "номер",
            "заметка",
            "name",
            "phone",
            "date",
            "status",
            "quantity",
            "ед изм",
        )
        labels = sum(any(word in t.lower() for word in common) for t in text)
        return len(text) * 2 + min(filled, 20) + labels * 4 - numeric * 4 - index * 0.15

    return max(enumerate(rows[:20]), key=score)[0]


def structure(rows, header):
    if isinstance(header, bool) or not isinstance(header, int) or header < -1 or header >= len(rows) and rows:
        raise UserError("Проверьте номер строки заголовков. 0 означает таблицу без заголовка.")
    width = max(map(len, rows), default=1) or 1
    labels = rows[header] if header >= 0 and rows else []
    columns = [
        {
            "id": f"c{n + 1}",
            "name": str(labels[n]).strip()
            if n < len(labels) and labels[n] not in ("", None)
            else f"Колонка {n + 1}",
        }
        for n in range(width)
    ]
    data = rows[header + 1 :] if header >= 0 else rows
    mapping = {}
    aliases = {
        "name": [
            "наименование",
            "название",
            "работа",
            "товар",
            "услуг",
            "номенклатура",
            "name",
            "item",
            "description",
        ],
        "unit": ["ед изм", "ед.", "единиц", "unit"],
        "planned": ["план", "смет", "количество", "объем", "объём", "quantity", "qty"],
        "code": ["код", "артикул", "номер", "№", "code", "sku"],
    }
    for role, words in aliases.items():
        matches = [col["id"] for col in columns if any(w in col["name"].lower() for w in words)]
        if len(matches) == 1:
            mapping[role] = matches[0]
    return columns, data, mapping


class DataTable:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS table_meta(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,payload TEXT NOT NULL,revision INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS record_events(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,owner INTEGER NOT NULL,row_id TEXT NOT NULL,action TEXT NOT NULL,before TEXT,after TEXT);
        CREATE TABLE IF NOT EXISTS table_daily(day TEXT NOT NULL,item_id TEXT NOT NULL,quantity TEXT NOT NULL,PRIMARY KEY(day,item_id));""")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS table_requests(id TEXT PRIMARY KEY,owner INTEGER NOT NULL,digest TEXT NOT NULL,row_id TEXT NOT NULL)"
        )

    def meta(self, key, value=None):
        if value is not None:
            self.db.execute(
                "INSERT OR REPLACE INTO table_meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False))
            )
        row = self.db.execute("SELECT payload FROM table_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def initialize(self, rows, header, source):
        columns, data, mapping = structure(rows, header)
        with self.db:
            if self.meta("columns") is not None:
                return
            self.meta("columns", columns)
            self.meta("source", source)
            self.meta("suggested_mapping", mapping)
            self.meta("work", {})
            self.meta("source_rows", rows)
            for row in data:
                if not any(v not in ("", None) for v in row):
                    continue
                values = {c["id"]: row[n] if n < len(row) else None for n, c in enumerate(columns)}
                self.db.execute(
                    "INSERT INTO records VALUES (?,?,1,1)",
                    (uuid.uuid4().hex, json.dumps(values, ensure_ascii=False)),
                )

    def all(self):
        return [
            {"id": r["id"], "values": json.loads(r["payload"]), "revision": r["revision"]}
            for r in self.db.execute("SELECT * FROM records WHERE active=1 ORDER BY rowid")
        ]

    def page(self, query="", offset=0):
        rows = self.all()
        if query:
            q = str(query).casefold()[:200]
            rows = [
                r for r in rows if any(q in str(v).casefold() for v in r["values"].values() if v is not None)
            ]
        offset = max(0, int(offset))
        return {
            "columns": self.meta("columns"),
            "rows": rows[offset : offset + 30],
            "total": len(rows),
            "offset": offset,
            "limit": 30,
            "mapping": self.meta("work") or {},
            "suggested_mapping": self.meta("suggested_mapping") or {},
            "source": self.meta("source"),
        }

    def save(self, owner, values, row_id=None, revision=None, request_id=None):
        columns = {c["id"] for c in self.meta("columns")}
        if not isinstance(values, dict) or not values or not set(values) <= columns:
            raise UserError("Проверьте поля записи: структура проекта изменилась.")
        if any(isinstance(v, (dict, list)) for v in values.values()):
            raise UserError("В ячейке ожидается текст, число или пустое значение.")
        values = {k: clean(v) for k, v in values.items()}
        digest = hashlib.sha256(
            json.dumps([values, row_id, revision], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        with self.db:
            if request_id:
                prior = self.db.execute("SELECT * FROM table_requests WHERE id=?", (request_id,)).fetchone()
                if prior:
                    if prior["owner"] != owner or prior["digest"] != digest:
                        raise UserError("Этот запрос уже использован. Откройте запись заново.")
                    return prior["row_id"]
            before = None
            if row_id:
                row = self.db.execute(
                    "SELECT * FROM records WHERE id=? AND active=1", (str(row_id),)
                ).fetchone()
                if not row or row["revision"] != revision:
                    raise UserError("Запись уже изменена. Обновите таблицу и повторите правку.")
                before = json.loads(row["payload"])
                after = {**before, **values}
                self.db.execute(
                    "UPDATE records SET payload=?,revision=revision+1 WHERE id=?",
                    (json.dumps(after, ensure_ascii=False), row_id),
                )
            else:
                if self.db.execute("SELECT COUNT(*) FROM records WHERE active=1").fetchone()[0] >= MAX_ROWS:
                    raise UserError("Достигнут предел 5 000 записей в проекте.")
                row_id = uuid.uuid4().hex
                after = {c: values.get(c) for c in columns}
                self.db.execute(
                    "INSERT INTO records VALUES (?,?,1,1)", (row_id, json.dumps(after, ensure_ascii=False))
                )
            self.db.execute(
                "INSERT INTO record_events(at,owner,row_id,action,before,after) VALUES (?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    owner,
                    row_id,
                    "update" if before is not None else "add",
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                ),
            )
            if request_id:
                self.db.execute(
                    "INSERT INTO table_requests VALUES (?,?,?,?)", (request_id, owner, digest, row_id)
                )
        return row_id

    def delete(self, owner, row_id, revision):
        with self.db:
            row = self.db.execute("SELECT * FROM records WHERE id=? AND active=1", (str(row_id),)).fetchone()
            if not row or row["revision"] != revision:
                raise UserError("Запись изменилась. Обновите таблицу.")
            self.db.execute("UPDATE records SET active=0,revision=revision+1 WHERE id=?", (row_id,))
            self.db.execute(
                "INSERT INTO record_events(at,owner,row_id,action,before,after) VALUES (?,?,?,?,?,NULL)",
                (datetime.now(timezone.utc).isoformat(), owner, row_id, "delete", row["payload"]),
            )

    def configure(self, mapping):
        columns = {c["id"] for c in self.meta("columns")}
        if not isinstance(mapping, dict):
            raise UserError("Проверьте настройку колонок.")
        allowed = {"name", "unit", "planned", "code", "default_unit"}
        if not set(mapping) <= allowed:
            raise UserError("Неизвестное назначение колонки.")
        selected = [v for k, v in mapping.items() if k != "default_unit" and v]
        if any(v not in columns for v in selected) or len(selected) != len(set(selected)):
            raise UserError("Выберите разные существующие колонки для каждого назначения.")
        if mapping and (
            not mapping.get("name")
            or not (mapping.get("unit") or str(mapping.get("default_unit", "")).strip())
        ):
            raise UserError("Для учёта объёмов выберите название работы и единицу или её общее значение.")
        with self.db:
            self.meta("work", mapping)

    def export(self):
        stream = StringIO(newline="")
        writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
        cols = self.meta("columns")
        writer.writerow([safe_text(c["name"]) for c in cols])
        for row in self.all():
            writer.writerow(
                [
                    safe_text(row["values"].get(c["id"], ""))
                    if isinstance(row["values"].get(c["id"]), str)
                    else row["values"].get(c["id"])
                    for c in cols
                ]
            )
        return stream.getvalue().encode("utf-8-sig")

    def history_export(self):
        stream = StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["Дата", "Пользователь", "Действие", "Запись", "Поле", "Было", "Стало"])
        names = {c["id"]: c["name"] for c in self.meta("columns")}
        for event in self.db.execute("SELECT * FROM record_events ORDER BY id"):
            before, after = (
                json.loads(event["before"] or "{}") or {},
                json.loads(event["after"] or "{}") or {},
            )
            for key in dict.fromkeys([*before, *after]):
                if before.get(key) != after.get(key):
                    writer.writerow(
                        [
                            safe_text(str(value))
                            for value in (
                                event["at"],
                                event["owner"],
                                event["action"],
                                event["row_id"],
                                names.get(key, key),
                                before.get(key, ""),
                                after.get(key, ""),
                            )
                        ]
                    )
        return stream.getvalue().encode("utf-8-sig")

    def history(self):
        return [
            {
                **dict(r),
                "before": json.loads(r["before"]) if r["before"] else None,
                "after": json.loads(r["after"]) if r["after"] else None,
            }
            for r in self.db.execute("SELECT * FROM record_events ORDER BY id DESC LIMIT 200")
        ]


class TableSheets:
    url = ""

    def __init__(self, table):
        self.table = table

    def read(self, day):
        mapping = self.table.meta("work") or {}
        if not mapping.get("name"):
            raise UserError("Для этого проекта откройте «Данные» или настройте колонки для учёта объёмов.")
        daily = {}
        dates = {}
        for row in self.table.db.execute("SELECT * FROM table_daily"):
            daily.setdefault(row["item_id"], {})[row["day"]] = Decimal(row["quantity"])
            dates.setdefault(row["day"], [10 + len(dates)])
        items = []
        warnings = []
        self.keys = {}
        self.unknown_plans = set()
        for index, row in enumerate(self.table.all(), 1):
            values = row["values"]
            name = str(values.get(mapping["name"]) or "").strip()
            u = unit(values.get(mapping.get("unit")) or mapping.get("default_unit", ""))
            if not name or not u:
                continue
            try:
                planned = str(number(values.get(mapping.get("planned")) or 0))
            except UserError:
                warnings.append(
                    f"Запись {index}: план не является числом. Её можно исправить в таблице данных."
                )
                continue
            amounts = daily.get(row["id"], {})
            code = str(values.get(mapping.get("code")) or "R" + row["id"][:6])
            key = hashlib.sha256(f"{row['id']}|{name}|{u}|{code}".encode()).hexdigest()[:24]
            self.keys[key] = row["id"]
            if not mapping.get("planned") or values.get(mapping["planned"]) in ("", None):
                self.unknown_plans.add(key)
            items.append(
                Item(
                    key,
                    index,
                    "Проект",
                    code,
                    name,
                    u,
                    planned,
                    str(sum(amounts.values(), Decimal(0))),
                    str(amounts.get(day, Decimal(0))),
                )
            )
        return Snapshot(day, items, dates, 10 + len(dates), warnings)

    def apply(self, day, expected, target):
        snapshot = self.read(day)
        if check_values(snapshot, expected, target):
            return
        with self.table.db:
            for key, value in target.items():
                self.table.db.execute(
                    "INSERT INTO table_daily VALUES (?,?,?) ON CONFLICT(day,item_id) DO UPDATE SET quantity=excluded.quantity",
                    (day, self.keys[key], value),
                )
