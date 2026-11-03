import hashlib
import uuid
from datetime import datetime
from decimal import Decimal

from .config import Settings
from .domain import Draft, Line, Snapshot, UserError, fmt, number, targets_for
from .gateways import Bitrix, Sheets
from .recognition import Recognizer
from .store import Store


class Service:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sheets: Sheets,
        bitrix: Bitrix,
        recognizer: Recognizer | None = None,
    ):
        self.settings, self.store, self.sheets, self.bitrix = settings, store, sheets, bitrix
        self.recognizer = recognizer
        # Preparing cloud credentials must not invalidate an existing local database.
        spreadsheet = settings.spreadsheet if settings.mode in {"setup", "live", "google"} else ""
        bitrix_url = settings.bitrix_url if settings.mode == "live" else "/"
        identity = (
            f"cloud|{spreadsheet}|{settings.sheet}|{settings.project}"
            if settings.mode in {"setup", "live", "google"}
            else f"local|{spreadsheet}|{settings.sheet}|{settings.project}|{bitrix_url}"
        )
        self.store.bind(hashlib.sha256(identity.encode()).hexdigest())
        if settings.mode == "live":
            from urllib.parse import urlsplit

            self.store.bind_target("bitrix_host", urlsplit(settings.bitrix_url).netloc.lower())

    def today(self) -> str:
        return datetime.now(self.settings.timezone).date().isoformat()

    def snapshot(self) -> Snapshot:
        return self.sheets.read(self.today())

    def editable(self, owner: int) -> Draft:
        draft = self.store.active(owner)
        if draft is None:
            raise UserError("Нет черновика. Отправьте отчёт или /edit для исправления сегодняшних объёмов.")
        if draft.status != "draft":
            raise UserError("Запись уже началась. Сначала завершите её через /retry.")
        if draft.day != self.today():
            raise UserError("Наступил новый день. Отмените старый черновик командой /cancel.")
        return draft

    def new(self, owner: int, mode: str = "add") -> Draft:
        if self.store.active(owner):
            raise UserError("Уже есть черновик. Завершите или отмените его.")
        snapshot = self.snapshot()
        draft = Draft(
            uuid.uuid4().hex[:12], owner, snapshot.day, mode, [], {i.id: i.today for i in snapshot.items}
        )
        self.store.save(draft)
        return draft

    def report(self, owner: int, text: str, source: str) -> Draft:
        previous = self.store.from_source(source, owner)
        if previous:
            return previous
        snapshot = self.snapshot()
        active = self.store.active(owner)
        if active:
            self.editable(owner)
        if not self.recognizer:
            raise UserError("Распознавание не подключено.")
        lines = self.recognizer.parse(text, snapshot)
        draft = active or self.new(owner)
        if len(draft.lines) + len(lines) > 100:
            raise UserError("В черновике максимум 100 строк. Завершите этот отчёт перед новым.")
        draft.lines.extend(lines)
        draft.revision += 1
        self.store.save(draft, source)
        return draft

    def manual(self, owner: int, code: str, amount: str, source: str | None = None) -> Draft:
        if source:
            previous = self.store.from_source(source, owner)
            if previous:
                return previous
        snapshot = self.snapshot()
        matches = [i for i in snapshot.items if i.id == code or i.code == code]
        if len(matches) != 1:
            raise UserError("Нужен точный код из /find. Для строки без номера используйте её ID.")
        draft = self.editable(owner) if self.store.active(owner) else self.new(owner)
        if len(draft.lines) >= 100:
            raise UserError("В черновике максимум 100 строк.")
        item = matches[0]
        draft.lines.append(Line(item.name, str(number(amount)), item.unit, [item.id], item.id))
        draft.revision += 1
        self.store.save(draft, source)
        return draft

    def change(self, owner: int, index: int, action: str, value: str = "") -> Draft:
        draft = self.editable(owner)
        if not 1 <= index <= len(draft.lines):
            raise UserError("Неверный номер строки черновика.")
        line = draft.lines[index - 1]
        if action == "delete":
            draft.lines.pop(index - 1)
        elif action == "quantity":
            line.quantity = str(number(value))
            if line.item_id:
                item = self.snapshot().by_id().get(line.item_id)
                if item is None:
                    raise UserError("Справочник изменился. Создайте черновик заново.")
                line.unit = item.unit
                line.question = None
        elif action == "pick":
            catalog = self.snapshot().items
            matches = [i for i in catalog if i.id == value or i.code == value]
            if len(matches) != 1:
                raise UserError("Укажите точный номер или ID из /find.")
            item = matches[0]
            line.item_id, line.unit, line.candidates = item.id, item.unit, [item.id]
            line.question = None if line.quantity is not None else "Укажите объём: /set НОМЕР ОБЪЁМ"
        else:
            raise UserError("Неизвестное действие.")
        draft.revision += 1
        self.store.save(draft)
        return draft

    def cancel(self, owner: int) -> None:
        draft = self.store.active(owner)
        if draft:
            if draft.status != "draft":
                raise UserError("Синхронизация уже началась. Завершите её через /retry.")
            draft.status = "cancelled"
            self.store.save(draft)

    def preview(self, draft: Draft) -> str:
        snapshot = self.sheets.read(draft.day)
        catalog = snapshot.by_id()
        kind = "Добавить к сегодняшним объёмам" if draft.mode == "add" else "Заменить дневные объёмы"
        location = "Локально на компьютере" if self.settings.mode == "local" else "Google Sheets и Битрикс24"
        result = [f"{kind} • {draft.day}", f"Место записи: {location}"]
        for index, line in enumerate(draft.lines, 1):
            item = catalog.get(line.item_id or "")
            qty = fmt(line.quantity) if line.quantity is not None else "?"
            name = f"[{item.code or item.id}] {item.name}" if item else line.source
            result.append(f"{index}. {name}\n   {qty} {line.unit or '?'}")
            if line.question:
                result.append(f"   Уточнение: {line.question}")
            if not item:
                for key in line.candidates:
                    candidate = catalog.get(key)
                    if candidate:
                        result.append(f"   /pick {index} {candidate.code or candidate.id} — {candidate.name}")
        try:
            targets = targets_for(draft, snapshot)
            for key, target in targets.items():
                item = catalog[key]
                result.append(f"Итого за день [{item.code or item.id}]: {fmt(target)} {item.unit}")
                total = Decimal(item.completed) - Decimal(item.today) + Decimal(target)
                if total > Decimal(item.planned):
                    result.append(
                        f"ВНИМАНИЕ: превышение сметы [{item.code or item.id}] на "
                        f"{fmt(total - Decimal(item.planned))} {item.unit}."
                    )
        except UserError:
            result.append("Перед записью уточните все строки.")
        result.extend(snapshot.warnings)
        result.append("Изменить: /set НОМЕР ОБЪЁМ; удалить: /del НОМЕР; добавить: /manual КОД ОБЪЁМ.")
        return "\n".join(result)

    def confirm(self, owner: int, draft_id: str, revision: int) -> str:
        if self.settings.mode == "setup":
            raise UserError("Запись ещё не включена: нужно подключить Битрикс24. Черновик сохранён.")
        draft = self.store.load(draft_id, owner)
        existing = self.store.operation(draft_id)
        if existing:
            return self.resume(owner, draft_id)
        if draft.status != "draft" or draft.revision != revision:
            raise UserError("Кнопка устарела. Откройте актуальный черновик: /draft.")
        if draft.day != self.today():
            raise UserError("Дата изменилась. Создайте отчёт на новый день.")
        if self.store.pending():
            raise UserError("Есть незавершённая запись. Сначала выполните /retry.")
        snapshot = self.snapshot()
        snapshot.target_column()
        targets = targets_for(draft, snapshot)
        if any(Decimal(snapshot.by_id()[key].today) != Decimal(draft.base[key]) for key in targets):
            raise UserError("Сегодняшние данные изменились. Отмените черновик и создайте новый.")
        report = {"project": self.settings.project, "date": draft.day, "positions": []}
        if self.sheets.url:
            report["sheet_url"] = self.sheets.url
        for item in snapshot.items:
            amount = targets.get(item.id, item.today)
            if Decimal(amount):
                report["positions"].append(
                    {
                        "id": item.id,
                        "code": item.code,
                        "section": item.section,
                        "name": item.name,
                        "quantity": amount,
                        "unit": item.unit,
                    }
                )
        identity = (
            self.settings.spreadsheet
            if self.settings.mode in {"live", "google"}
            else (self.settings.database.name if self.settings.mode == "table" else self.settings.project)
        )
        day_key = hashlib.sha256(f"{identity}|{self.settings.sheet}|{draft.day}".encode()).hexdigest()
        self.store.prepare(
            draft,
            {
                "day": draft.day,
                "confirmed_at": datetime.now(self.settings.timezone).isoformat(timespec="seconds"),
                "mode": draft.mode,
                "changes": [
                    {
                        "id": item.id,
                        "code": item.code,
                        "section": item.section,
                        "name": item.name,
                        "unit": item.unit,
                        "before": draft.base[item.id],
                        "after": targets[item.id],
                    }
                    for item in snapshot.items
                    if item.id in targets
                ],
                "expected": {k: draft.base[k] for k in targets},
                "targets": targets,
                "report": report,
                "key": day_key,
                "title": f"{self.settings.project}: работы за {draft.day}",
            },
        )
        return self.resume(owner, draft_id)

    def resume(self, owner: int, op_id: str | None = None) -> str:
        if self.settings.mode == "setup":
            raise UserError("Для записи в обе системы сначала подключите Битрикс24.")
        if op_id is None:
            pending = [op for op in self.store.pending() if op["owner"] == owner]
            if not pending:
                return "У вас нет незавершённых записей."
            op_id = pending[0]["id"]
        op = self.store.operation(op_id)
        if not op or op["owner"] != owner:
            raise UserError("Операция не найдена.")
        if op["stage"] == "done":
            self.store.finish(self.store.load(op_id, owner))
            return "Эта запись уже выполнена. Повторного начисления нет."
        payload = op["payload"]
        try:
            if op["stage"] == "prepared":
                self.sheets.apply(payload["day"], payload["expected"], payload["targets"])
                self.store.stage(op_id, "sheets")
            self.bitrix.upsert(payload["key"], payload["title"], payload["report"])
            draft = self.store.load(op_id, owner)
            self.store.finish(draft)
            if self.settings.mode == "table":
                return "Объёмы сохранены в проекте в приложении. Отчёт поставлен в очередь Битрикс24."
            if self.settings.mode == "google":
                return "Объёмы записаны в Google Таблицу. Отчёт поставлен в очередь Битрикс24."
            return (
                "Записано в Google Sheets и Битрикс24."
                if self.settings.mode == "live"
                else ("Сохранено в двух локальных хранилищах. Реальные сервисы не подключены.")
            )
        except UserError as error:
            current = self.store.operation(op_id)
            self.store.stage(op_id, current["stage"], str(error))
            raise UserError(
                f"{error}\nОперация сохранена. Для продолжения: /retry. Не отправляйте тот же отчёт заново."
            ) from None
