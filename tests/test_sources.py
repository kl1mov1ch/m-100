import io
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook

from stroybot.domain import UserError
from stroybot.sources import bitrix_link, public_bitrix

pytest_plugins = ("test_access",)
LINK = "https://bitrix24public.com/demo.bitrix24.ru/sheet/abcdefgh1234"


def workbook():
    book = Workbook()
    book.active.append(["Материал", "Количество"])
    book.active.append(["Кабель", 7])
    target = io.BytesIO()
    book.save(target)
    return target.getvalue()


def transport(*, download=True, redirect=None, broken=False):
    document = {
        "fileType": "xlsx",
        "title": "Материалы.xlsx",
        "permissions": {"download": download, "edit": False},
    }
    html = (
        'editorJson: {"document":'
        + json.dumps(document)
        + "}; linkToDownload: '/demo.bitrix24.ru/docs/pub/abcdef/download?token=testtoken'"
    )

    def handle(request):
        if "/sheet/" in request.url.path:
            return httpx.Response(200, text=html)
        if redirect:
            return httpx.Response(302, headers={"location": redirect})
        return httpx.Response(200, content=b"<html>login</html>" if broken else workbook())

    return httpx.MockTransport(handle)


def test_public_read_only_link_imports_data_without_claiming_remote_write(hub):
    with httpx.Client(transport=transport()) as client:
        hub.client = client
        preview = hub.stage_bitrix(1, LINK)
        assert preview["source"]["sync"] == "copy"
        assert preview["preview"][0] == ["Кабель", 7]
        key = hub.finish_import(1, {"token": preview["token"], "name": "Материалы", "header": 0})
        hub.access.save_account(1, 2, "Мастер", "worker", key)
        with hub.scope(2):
            table = hub.sheets.table
            assert table.page()["source"]["provider"] == "bitrix"
        with pytest.raises(UserError):
            hub.stage_bitrix(2, LINK)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "https://bitrix24public.com.evil.test/demo.bitrix24.ru/sheet/abcdefgh",
        "https://bitrix24public.com/evil.test/sheet/abcdefgh",
        LINK + "?redirect=http://localhost",
    ],
)
def test_rejects_untrusted_links(url):
    with pytest.raises(UserError):
        bitrix_link(url)


@pytest.mark.parametrize(
    "options",
    [
        {"download": False},
        {"redirect": "http://127.0.0.1/private"},
        {"redirect": "https://evil.test/file"},
        {"broken": True},
    ],
)
def test_denied_download_and_redirects_are_safe_user_errors(options):
    with httpx.Client(transport=transport(**options)) as client, pytest.raises(UserError):
        public_bitrix(client, LINK)


def test_windows_bootstrap_output_handles_non_cyrillic_console():
    root = Path(__file__).resolve().parents[1]
    code = "import runpy; runpy.run_path('start.py',run_name='test_bootstrap'); print('Создаём окружение')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
    )
    assert result.returncode == 0
    assert "Создаём" in result.stdout.decode("utf-8")
