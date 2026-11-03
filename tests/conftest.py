import json
from dataclasses import replace
from pathlib import Path

import pytest

from stroybot.config import Settings
from stroybot.gateways import LocalBitrix, LocalSheets
from stroybot.service import Service
from stroybot.store import Store


@pytest.fixture
def rows():
    return json.loads(Path("src/stroybot/assets/catalog.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    return replace(
        Settings.load(),
        database=tmp_path / "db.sqlite3",
        token="test-token",
        llm_key="",
        allowed={1, 2},
        mode="local",
        spreadsheet="",
        project="Тест",
        sheet="Лист1",
        bitrix_url="/",
    )


@pytest.fixture
def service(settings, tmp_path):
    store = Store(settings.database)
    result = Service(
        settings, store, LocalSheets(state=tmp_path / "sheet.json"), LocalBitrix(tmp_path / "bitrix.json")
    )
    result.today = lambda: "2026-11-03"
    yield result
    store.close()
