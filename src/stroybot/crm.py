"""Private CRM configuration and a durable latest-report outbox per project/day."""

import hashlib
import json
import os
import re
from dataclasses import replace
from urllib.parse import urlsplit

from .domain import UserError
from .gateways import Bitrix24, atomic_json


class CRMConfig:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.path = settings.database.parent / "integrations.json"

    def load(self):
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        return replace(
            self.settings,
            bitrix_url=data.get("webhook", self.settings.bitrix_url),
            category=data.get("category", self.settings.category),
        )

    def status(self):
        settings = self.load()
        return {
            "configured": settings.bitrix_url != "/",
            "portal": "https://" + urlsplit(settings.bitrix_url).netloc
            if settings.bitrix_url != "/"
            else ("https://" + os.environ["BITRIX_PORTAL_HOST"] if os.getenv("BITRIX_PORTAL_HOST") else ""),
            "category": settings.category,
            "disk_checked": bool(json.loads(self.path.read_text()).get("disk_checked"))
            if self.path.exists()
            else False,
        }

    def connect(self, webhook, category=0, disk=False):
        host = os.getenv("BITRIX_PORTAL_HOST") or urlsplit(self.load().bitrix_url).hostname
        if not host and isinstance(webhook, str):
            host = urlsplit(webhook.strip()).hostname
        if not host or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.bitrix24\.(ru|com|by|de|eu|kz)", host):
            raise UserError("Укажите входящий вебхук вашего портала Битрикс24.")
        if not isinstance(webhook, str) or not re.fullmatch(
            r"https://" + re.escape(host) + r"/rest/[1-9][0-9]*/[A-Za-z0-9_-]{8,150}/?", webhook.strip()
        ):
            raise UserError("Нужен полный адрес входящего вебхука вашего портала " + host + ", с правом CRM.")
        if isinstance(category, bool) or not str(category).isdigit() or int(category) > 1000000:
            raise UserError("ID воронки должен быть целым неотрицательным числом.")
        settings = replace(
            self.settings, bitrix_url=webhook.strip().rstrip("/") + "/", category=int(category)
        )
        gateway = Bitrix24(settings, self.client)
        fields = gateway.call("crm.deal.fields", {}).get("result", {})
        if not all(key in fields for key in ("TITLE", "COMMENTS", "ORIGIN_ID", "ORIGINATOR_ID")):
            raise UserError("Вебхук не даёт доступа к нужным полям сделок CRM.")
        if settings.category:
            categories = gateway.call("crm.dealcategory.list", {}).get("result", [])
            if not any(str(item.get("ID")) == str(settings.category) for item in categories):
                raise UserError("Выбранная воронка не найдена или недоступна вебхуку.")
        if disk:
            fields = gateway.call("disk.file.getFields", {}).get("result", {})
            if not isinstance(fields, dict) or "ID" not in fields:
                raise UserError("Не удалось проверить Диск. Добавьте право «Диск» в настройках вебхука.")
        # The destination is pinned to the owner's known portal. Never return the secret.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(
            self.path, {"webhook": settings.bitrix_url, "category": settings.category, "disk_checked": disk}
        )
        self.path.chmod(0o600)
        return self.status()


class QueuedBitrix:
    def __init__(self, store, config: CRMConfig):
        self.store, self.config = store, config
        store.db.execute("""CREATE TABLE IF NOT EXISTS crm_outbox (
            key TEXT PRIMARY KEY,title TEXT NOT NULL,report TEXT NOT NULL,
            fingerprint TEXT NOT NULL,status TEXT NOT NULL,error TEXT,
            updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        store.db.commit()

    def upsert(self, key, title, report):
        raw = json.dumps(report, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.db:
            self.store.db.execute(
                """INSERT INTO crm_outbox(key,title,report,fingerprint,status)
                VALUES (?,?,?,?,'pending') ON CONFLICT(key) DO UPDATE SET
                title=excluded.title,report=excluded.report,fingerprint=excluded.fingerprint,
                status='pending',error=NULL,updated=CURRENT_TIMESTAMP
                WHERE crm_outbox.fingerprint != excluded.fingerprint""",
                (key, title, raw, fingerprint),
            )

    def status(self):
        counts = dict(self.store.db.execute("SELECT status,count(*) FROM crm_outbox GROUP BY status"))
        last = self.store.db.execute(
            "SELECT error FROM crm_outbox WHERE error IS NOT NULL ORDER BY updated DESC LIMIT 1"
        ).fetchone()
        return {
            **self.config.status(),
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
            "last_error": last[0] if last else None,
        }

    def flush(self, limit=5):
        settings = self.config.load()
        if settings.bitrix_url == "/":
            return 0
        self.store.bind_target(
            "crm_destination", urlsplit(settings.bitrix_url).netloc.lower() + ":" + str(settings.category)
        )
        gateway = Bitrix24(settings, self.config.client)
        sent = 0
        for row in self.store.db.execute(
            "SELECT * FROM crm_outbox WHERE status='pending' ORDER BY updated LIMIT ?", (limit,)
        ).fetchall():
            try:
                gateway.upsert(row["key"], row["title"], json.loads(row["report"]))
            except UserError as error:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE crm_outbox SET error=? WHERE key=? AND fingerprint=?",
                        (str(error), row["key"], row["fingerprint"]),
                    )
                break
            else:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE crm_outbox SET status='sent',error=NULL WHERE key=? AND fingerprint=?",
                        (row["key"], row["fingerprint"]),
                    )
                sent += 1
        return sent
