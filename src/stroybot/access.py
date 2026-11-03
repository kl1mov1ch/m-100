"""Persistent accounts and isolated project stores for the Telegram application."""

import contextvars
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .crm import CRMConfig, QueuedBitrix
from .domain import UserError
from .gateways import Bitrix24, GoogleSheets, LocalBitrix
from .recognition import Recognizer
from .service import Service
from .store import Store
from .tabular import DataTable, TableSheets, bounded, detect_header, read_file, structure


class Access:
    def __init__(self, path: Path, settings):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                role TEXT NOT NULL, project TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                spreadsheet TEXT NOT NULL, sheet TEXT NOT NULL, kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS selections (owner INTEGER PRIMARY KEY, project TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_events (at TEXT NOT NULL, actor INTEGER NOT NULL,
                action TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY,owner INTEGER NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL,project TEXT);
        """)
        roots = os.getenv("ADMIN_USER_IDS", "")
        if not roots and len(settings.allowed) != 1:
            raise UserError("Укажите ADMIN_USER_IDS явно, если разрешено несколько пользователей.")
        self.roots = {int(i) for i in roots.split(",") if i.strip()} if roots else {min(settings.allowed)}
        if not self.roots <= settings.allowed:
            raise UserError("ADMIN_USER_IDS должны входить в ALLOWED_USER_IDS при первом запуске.")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO projects VALUES (?,?,?,?,?)",
                ("default", settings.project, settings.spreadsheet, settings.sheet, "default"),
            )
            for owner in settings.allowed | self.roots:
                self.db.execute(
                    "INSERT OR IGNORE INTO accounts(id,name,role,project) VALUES (?,?,?,?)",
                    (owner, str(owner), "admin" if owner in self.roots else "worker", "default"),
                )

    def account(self, owner: int):
        row = self.db.execute("SELECT * FROM accounts WHERE id=? AND enabled=1", (owner,)).fetchone()
        return dict(row) if row else None

    def allowed(self) -> set[int]:
        return {row[0] for row in self.db.execute("SELECT id FROM accounts WHERE enabled=1")} | self.roots

    def admin(self, owner: int) -> bool:
        return owner in self.roots or bool((self.account(owner) or {}).get("role") == "admin")

    def require_admin(self, owner: int):
        if not self.admin(owner):
            raise UserError("Это действие доступно только администратору.")

    def project(self, key: str):
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (key,)).fetchone()
        if not row:
            raise UserError("Объект не найден.")
        return dict(row)

    def projects(self, owner: int) -> list[dict]:
        account = self.account(owner)
        if not account:
            raise UserError("Доступ к приложению не выдан.")
        if self.admin(owner):
            return [dict(row) for row in self.db.execute("SELECT * FROM projects ORDER BY rowid")]
        return [self.project(account["project"])]

    def selected(self, owner: int) -> str:
        account = self.account(owner)
        if not account:
            raise UserError("Доступ к приложению не выдан.")
        row = self.db.execute("SELECT project FROM selections WHERE owner=?", (owner,)).fetchone()
        selected = row[0] if row else account["project"]
        return selected if any(p["id"] == selected for p in self.projects(owner)) else account["project"]

    def select(self, owner: int, key: str):
        self.require_admin(owner)
        if key not in {p["id"] for p in self.projects(owner)}:
            raise UserError("Нет доступа к этому объекту.")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO selections VALUES (?,?)", (owner, key))

    def event(self, actor: int, action: str, data: dict):
        self.db.execute(
            "INSERT INTO admin_events VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), actor, action, json.dumps(data, ensure_ascii=False)),
        )

    def add_project(self, actor: int, name: str, spreadsheet: str, sheet: str, kind="google") -> str:
        self.require_admin(actor)
        existing = self.db.execute(
            "SELECT id FROM projects WHERE spreadsheet=? AND sheet=?", (spreadsheet, sheet)
        ).fetchone()
        if existing:
            raise UserError("Эта таблица и лист уже подключены.")
        key = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (key, name, spreadsheet, sheet, kind))
            self.event(
                actor, "project.add", {"id": key, "name": name, "spreadsheet": spreadsheet, "sheet": sheet}
            )
        return key

    def save_account(self, actor: int, owner, name, role, project):
        self.require_admin(actor)
        if isinstance(owner, bool) or not str(owner).isdigit() or not 0 < int(owner) < 2**52:
            raise UserError("Укажите числовой Telegram ID пользователя.")
        owner = int(owner)
        name = str(name or "").strip()
        if not 1 <= len(name) <= 80 or role not in {"admin", "worker"}:
            raise UserError("Укажите имя и роль пользователя.")
        self.project(str(project))
        if owner in self.roots or owner == actor:
            raise UserError("Учётную запись владельца и собственные права нельзя менять здесь.")
        with self.db:
            self.db.execute(
                "INSERT INTO accounts VALUES (?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role,project=excluded.project,enabled=1",
                (owner, name, role, project),
            )
            self.db.execute("DELETE FROM selections WHERE owner=?", (owner,))
            self.event(actor, "account.save", {"id": owner, "name": name, "role": role, "project": project})

    def disable(self, actor: int, owner):
        self.require_admin(actor)
        if not str(owner).isdigit():
            raise UserError("Некорректный Telegram ID.")
        owner = int(owner)
        if owner in self.roots or owner == actor:
            raise UserError("Нельзя отключить владельца или самого себя.")
        with self.db:
            self.db.execute("UPDATE accounts SET enabled=0 WHERE id=?", (owner,))
            self.event(actor, "account.disable", {"id": owner})

    def panel(self, owner: int) -> dict:
        self.require_admin(owner)
        return {
            "accounts": [dict(row) for row in self.db.execute("SELECT * FROM accounts ORDER BY id")],
            "projects": self.projects(owner),
            "owner_ids": sorted(self.roots),
        }


class ServiceHub:
    def __init__(self, base: Service, client, access: Access):
        self.base, self.client, self.access = base, client, access
        self.context = contextvars.ContextVar("project-owner", default=None)
        self.services = {}
        self.crm = CRMConfig(base.settings, client)
        if base.settings.mode == "google":
            base.bitrix = QueuedBitrix(base.store, self.crm)

    def current_key(self):
        owner = self.context.get()
        return self.access.selected(owner) if owner and self.access.account(owner) else "default"

    def current(self):
        return self.for_project(self.current_key())

    def for_project(self, key):
        if key == "default":
            return self.base
        if key not in self.services:
            project = self.access.project(key)
            if project["kind"] == "dataset":
                settings = replace(
                    self.base.settings,
                    project=project["name"],
                    spreadsheet="",
                    sheet=project["sheet"],
                    database=self.base.settings.database.parent / f"project-{key}.sqlite3",
                    mode="table",
                )
                table = DataTable(settings.database)
                store = Store(settings.database)
                recognition_client = self.base.recognizer.client if self.base.recognizer else self.client
                self.services[key] = Service(
                    settings,
                    store,
                    TableSheets(table),
                    QueuedBitrix(store, self.crm),
                    Recognizer(settings, recognition_client),
                )
                return self.services[key]
            settings = replace(
                self.base.settings,
                project=project["name"],
                spreadsheet=project["spreadsheet"],
                sheet=project["sheet"],
                database=self.base.settings.database.parent / f"project-{key}.sqlite3",
                mode="google"
                if project["kind"] == "google_work"
                else "live"
                if self.base.settings.mode == "live"
                else "setup",
            )
            recognition_client = self.base.recognizer.client if self.base.recognizer else self.client
            store = Store(settings.database)
            self.services[key] = Service(
                settings,
                store,
                GoogleSheets(settings, self.client),
                (
                    QueuedBitrix(store, self.crm)
                    if settings.mode == "google"
                    else Bitrix24(settings, self.client)
                    if settings.mode == "live"
                    else LocalBitrix(settings.database.with_suffix(".json"))
                ),
                Recognizer(settings, recognition_client),
            )
        return self.services[key]

    def __getattr__(self, name):
        return getattr(self.current(), name)

    @contextmanager
    def scope(self, owner: int):
        token = self.context.set(owner)
        allowed = self.access.allowed()
        self.base.settings.allowed = allowed
        for service in self.services.values():
            service.settings.allowed = allowed
        try:
            yield
        finally:
            self.context.reset(token)

    def overview(self, owner):
        return {
            "is_admin": self.access.admin(owner),
            "project_id": self.current_key(),
            "projects": [{"id": p["id"], "name": p["name"]} for p in self.access.projects(owner)],
            "actor_id": owner,
        }

    def add_project(self, actor: int, data: dict):
        self.access.require_admin(actor)
        name, sheet, url = [str(data.get(k, "")).strip() for k in ("name", "sheet", "url")]
        match = re.fullmatch(
            r"https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]{20,150})(?:/[^\s]*)?", url
        )
        if not match or not 1 <= len(name) <= 80 or not 1 <= len(sheet) <= 100:
            raise UserError("Укажите название объекта, ссылку Google-таблицы и имя листа.")
        settings = replace(self.base.settings, spreadsheet=match[1], sheet=sheet)
        # A successful read validates both permissions and the catalogue structure.
        gateway = GoogleSheets(settings, self.client)
        snapshot = gateway.read(self.base.today())
        if not snapshot.items:
            raise UserError("В таблице не найдены работы. Нужна структура сметы из шаблона.")
        snapshot.target_column()
        gateway.check_write_access()
        key = self.access.add_project(actor, name, match[1], sheet, kind="google_work")
        self.access.select(actor, key)
        return f"Google-таблица подключена для записи отчётов: {len(snapshot.items)} позиций."

    def google_email(self):
        try:
            return json.loads(Path(self.base.settings.credentials).read_text(encoding="utf-8")).get("client_email", "")
        except (OSError, ValueError):
            return ""

    def sync_crm(self):
        sent = 0
        if isinstance(self.base.bitrix, QueuedBitrix):
            sent += self.base.bitrix.flush()
        for project in self.access.db.execute(
            "SELECT id FROM projects WHERE kind IN ('google_work','dataset')"
        ).fetchall():
            gateway = self.for_project(project["id"]).bitrix
            if isinstance(gateway, QueuedBitrix):
                sent += gateway.flush()
        return sent

    def close(self):
        for service in self.services.values():
            service.store.close()
            if isinstance(service.sheets, TableSheets):
                service.sheets.table.db.close()
        self.access.db.close()

    def stage_import(self, owner, filename, data=None, sheets=None):
        self.access.require_admin(owner)
        if sheets is None:
            sheets = read_file(filename, data)
        if not sheets:
            raise UserError("В файле не найдены листы с таблицами.")
        if len(json.dumps(sheets, ensure_ascii=False).encode()) > 24 * 1024 * 1024:
            raise UserError("Содержимое таблицы слишком большое. Разделите книгу на части.")
        token = uuid.uuid4().hex
        if data:
            folder = self.base.settings.database.parent / "imports"
            folder.mkdir(mode=0o700, exist_ok=True)
            suffix = Path(filename).suffix.lower()
            path = folder / (token + suffix)
            with path.open("xb") as stream:
                stream.write(data)
            path.chmod(0o600)
        payload = {"filename": Path(filename).name, "sheets": sheets}
        with self.access.db:
            self.access.db.execute(
                "INSERT INTO imports VALUES (?,?,?,?,NULL)",
                (token, owner, time.time(), json.dumps(payload, ensure_ascii=False)),
            )
        return self.import_preview(owner, token, 0, None)

    def import_payload(self, owner, token):
        self.access.require_admin(owner)
        row = self.access.db.execute(
            "SELECT * FROM imports WHERE id=? AND owner=?", (str(token), owner)
        ).fetchone()
        if not row or time.time() - row["created"] > 3600:
            raise UserError("Предпросмотр импорта истёк. Загрузите файл ещё раз.")
        return dict(row), json.loads(row["payload"])

    def import_preview(self, owner, token, sheet, header):
        _, payload = self.import_payload(owner, token)
        if isinstance(sheet, bool) or not isinstance(sheet, int) or not 0 <= sheet < len(payload["sheets"]):
            raise UserError("Выберите лист из файла.")
        rows = payload["sheets"][sheet]["rows"]
        if header is None:
            header = detect_header(rows)
        columns, data, mapping = structure(rows, header)
        return {
            "token": token,
            "filename": payload["filename"],
            "sheets": [{"name": s["name"], "rows": len(s["rows"])} for s in payload["sheets"]],
            "sheet": sheet,
            "header": header,
            "columns": columns,
            "preview": data[:6],
            "total_rows": sum(any(v not in ("", None) for v in r) for r in data),
            "suggested_mapping": mapping,
        }

    def finish_import(self, owner, data):
        token = data.get("token")
        row, payload = self.import_payload(owner, token)
        if row["project"]:
            return row["project"]
        name = str(data.get("name", "")).strip()
        if not 1 <= len(name) <= 80:
            raise UserError("Укажите название проекта до 80 символов.")
        preview = self.import_preview(owner, token, data.get("sheet", 0), data.get("header"))
        sheet = payload["sheets"][preview["sheet"]]
        key = uuid.uuid4().hex
        table = DataTable(self.base.settings.database.parent / f"project-{key}.sqlite3")
        try:
            table.initialize(
                sheet["rows"], preview["header"], {"filename": payload["filename"], "sheet": sheet["name"]}
            )
        finally:
            table.db.close()
        with self.access.db:
            self.access.db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?)", (key, name, "", sheet["name"], "dataset")
            )
            self.access.db.execute("UPDATE imports SET project=? WHERE id=?", (key, token))
            self.access.event(
                owner,
                "project.import",
                {"id": key, "name": name, "file": payload["filename"], "sheet": sheet["name"]},
            )
            self.access.select(owner, key)
        return key

    def stage_google(self, owner, url, sheet):
        self.access.require_admin(owner)
        match = re.fullmatch(
            r"https://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]{20,150})(?:/[^\s]*)?", str(url).strip()
        )
        if not match or not str(sheet).strip():
            raise UserError("Укажите ссылку Google-таблицы и имя листа.")
        settings = replace(self.base.settings, spreadsheet=match[1], sheet=str(sheet).strip())
        gateway = GoogleSheets(settings, self.client)
        region = "'" + settings.sheet.replace("'", "''") + "'"
        raw = gateway.request(
            "GET",
            "/values/" + quote(region, safe=""),
            params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"},
        )
        return self.stage_import(
            owner,
            "Google Sheets — " + settings.sheet,
            sheets=[{"name": settings.sheet, "rows": bounded(raw.get("values", []))}],
        )
