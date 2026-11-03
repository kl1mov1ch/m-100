from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any


class UserError(Exception):
    """Safe explanation that may be shown to a Telegram user."""


def number(value: Any, *, allow_zero: bool = True) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value).replace(",", ".").strip())
        if not result.is_finite() or result < 0 or result > Decimal("1000000000"):
            raise ValueError
        if result.as_tuple().exponent < -6:
            raise ValueError
        if not allow_zero and result == 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise UserError(
            "Нужен неотрицательный объём до 1 млрд с точностью до 6 знаков после запятой."
        ) from None


def unit(value: Any) -> str:
    raw = str(value or "").strip().lower().rstrip(".")
    return {
        "метр": "м",
        "метров": "м",
        "m": "м",
        "штук": "шт",
        "штуки": "шт",
        "штука": "шт",
        "кг": "кг",
        "комплект": "компл",
        "система": "сист",
    }.get(raw, raw)


def fmt(value: Decimal | str) -> str:
    return format(Decimal(value).normalize(), "f").replace(".", ",")


def column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def date_value(value: Any, month_first: bool = False) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return date(1899, 12, 30) + timedelta(days=int(value)) if 30000 < value < 100000 else None
        except (ValueError, OverflowError):
            return None
    slash = ("%m/%d/%Y", "%d/%m/%Y") if month_first else ("%d/%m/%Y", "%m/%d/%Y")
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", *slash):
        try:
            return datetime.strptime(str(value).strip(), pattern).date()
        except ValueError:
            pass
    return None


@dataclass
class Item:
    id: str
    row: int
    section: str
    code: str
    name: str
    unit: str
    planned: str
    completed: str
    today: str

    @property
    def remaining(self) -> Decimal:
        return Decimal(self.planned) - Decimal(self.completed)


@dataclass
class Snapshot:
    day: str
    items: list[Item]
    date_columns: dict[str, list[int]]
    width: int
    warnings: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, Item]:
        return {item.id: item for item in self.items}

    def target_column(self) -> tuple[int, bool]:
        existing = self.date_columns.get(self.day, [])
        if len(existing) > 1:
            raise UserError("Сегодняшняя дата повторяется в таблице. Уберите дубликат перед записью.")
        if existing:
            return existing[0], False
        return max([9] + [c for cols in self.date_columns.values() for c in cols]) + 1, True


def parse_table(rows: list[list[Any]], day: str) -> Snapshot:
    def cell(r: int, c: int) -> Any:
        return rows[r - 1][c - 1] if r <= len(rows) and c <= len(rows[r - 1]) else None

    if "Наименование" not in str(cell(1, 2)) or unit(cell(1, 3)) != "ед изм":
        raise UserError("Структура таблицы изменилась: ожидаются наименование в B и единица в C.")
    dates: dict[str, list[int]] = {}
    # Infer the slash-date convention from the entire date header. A date such
    # as 8/19/2026 establishes month-first for ambiguous neighbouring 9/1/2026.
    slash_parts = [
        tuple(map(int, match.groups()))
        for value in (rows[2][9:] if len(rows) > 2 else [])
        if (match := re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", str(value)))
    ]
    us = any(a <= 12 < b <= 31 for a, b, _ in slash_parts)
    eu = any(b <= 12 < a <= 31 for a, b, _ in slash_parts)
    if us and eu:
        raise UserError("В строке дат смешаны форматы месяц/день и день/месяц. Используйте ДД.ММ.ГГГГ.")
    for col in range(10, len(rows[2]) + 1 if len(rows) > 2 else 10):
        parsed = date_value(cell(3, col), month_first=us)
        if parsed:
            dates.setdefault(parsed.isoformat(), []).append(col)
    warnings = [
        f"Повторяется дата {d}: {', '.join(column(c) for c in cols)}."
        for d, cols in dates.items()
        if len(cols) > 1
    ]
    items: list[Item] = []
    section = ""
    seen: set[str] = set()
    for row in range(4, len(rows) + 1):
        name = str(cell(row, 2) or "").strip()
        u = unit(cell(row, 3))
        if not name:
            continue
        if name.replace(" ", "").upper() in {"ОВ3", "ЭМ3"}:
            section = name
        if not u or cell(row, 4) in (None, ""):
            continue
        if u not in {"м", "шт", "кг", "компл", "сист"}:
            raise UserError(f"Неизвестная единица в строке {row}: {u}.")
        code = str(cell(row, 1) or "")
        key = hashlib.sha256(f"{section}|{code}|{name}".encode()).hexdigest()[:12]
        if key in seen:
            raise UserError(f"Строкам с одинаковым названием нужен разный номер: {name}.")
        seen.add(key)
        completed = Decimal(0)
        today = Decimal(0)
        dated_columns = {col for cols in dates.values() for col in cols}
        for col in range(10, len(rows[row - 1]) + 1):
            if col not in dated_columns and cell(row, col) not in (None, ""):
                raise UserError(f"В {column(col)}{row} есть объём без даты. Укажите дату столбца.")
        for d, cols in dates.items():
            for col in cols:
                value = cell(row, col)
                if value in (None, ""):
                    continue
                try:
                    amount = number(value)
                except UserError:
                    raise UserError(f"В {column(col)}{row} некорректный объём. Исправьте ячейку.") from None
                completed += amount
                if d == day:
                    today += amount
        items.append(
            Item(key, row, section, code, name, u, str(number(cell(row, 4))), str(completed), str(today))
        )
    if not items:
        raise UserError("В таблице не найдено рабочих позиций.")
    return Snapshot(day, items, dates, max(map(len, rows), default=9), warnings)


@dataclass
class Line:
    source: str
    quantity: str | None
    unit: str | None
    candidates: list[str]
    item_id: str | None = None
    question: str | None = None


@dataclass
class Draft:
    id: str
    owner: int
    day: str
    mode: str
    lines: list[Line]
    base: dict[str, str]
    revision: int = 1
    status: str = "draft"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Draft:
        return cls(**{**data, "lines": [Line(**line) for line in data["lines"]]})


def targets_for(draft: Draft, snapshot: Snapshot) -> dict[str, str]:
    catalog = snapshot.by_id()
    values: dict[str, Decimal] = {}
    if not draft.lines:
        raise UserError("Список пуст. Добавьте работы.")
    for line in draft.lines:
        if not line.item_id or line.quantity is None or line.question:
            raise UserError("Сначала уточните все позиции и объёмы.")
        item = catalog.get(line.item_id)
        if item is None:
            raise UserError("Справочник изменился. Создайте отчёт заново.")
        if item.id not in draft.base:
            raise UserError("В справочник добавлены позиции. Создайте черновик заново.")
        if unit(line.unit) != item.unit:
            raise UserError(f"Единица позиции {item.code or item.id} должна быть {item.unit}.")
        amount = number(line.quantity)
        if item.unit == "шт" and amount != amount.to_integral_value():
            raise UserError("Количество в штуках должно быть целым.")
        if draft.mode == "set" and item.id in values:
            raise UserError("В исправлении одна позиция указана дважды. Удалите повтор.")
        values[item.id] = values.get(item.id, Decimal(0)) + amount
    return {
        key: str(number(amount + (Decimal(draft.base[key]) if draft.mode == "add" else 0)))
        for key, amount in values.items()
    }
