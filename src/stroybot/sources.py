"""Bounded public Bitrix downloads. Never execute editor JavaScript or log signed URLs."""

import json
import re
from urllib.parse import urljoin, urlsplit

import httpx

from .domain import UserError
from .tabular import MAX_FILE, read_file

PORTAL = r"[a-z0-9][a-z0-9-]*\.bitrix24\.(?:ru|com|by|de|eu|kz)"


def bitrix_link(url):
    match = re.fullmatch(
        r"https://bitrix24public\.com/(" + PORTAL + r")/sheet/([A-Za-z0-9_-]{8,150})/?",
        str(url).strip(),
    )
    if not match:
        raise UserError("Вставьте публичную ссылку Битрикс24: bitrix24public.com/…/sheet/…")
    return match[1]


def download_path(html, portal):
    match = re.search(r"linkToDownload:\s*'([^']+)'", html)
    if not match:
        raise UserError("Битрикс24 не разрешает скачивание. Включите скачивание в доступе по ссылке.")
    path = match[1]
    # Restrict the capability to this portal's public document download endpoint.
    if not re.fullmatch(
        r"/" + re.escape(portal) + r"/docs/pub/[A-Za-z0-9_-]+/download\?token=[A-Za-z0-9_-]+", path
    ):
        raise UserError("Неизвестный формат ссылки скачивания Битрикс24. Загрузите Excel-файл.")
    return "https://bitrix24public.com" + path


def read_response(client, url, limit, allowed_host, headers=None):
    for _ in range(4):
        with client.stream("GET", url, timeout=25, follow_redirects=False, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("location", ""))
                target = urlsplit(url)
                if (
                    target.scheme != "https"
                    or target.hostname != allowed_host
                    or target.port not in (None, 443)
                    or target.username
                    or target.password
                ):
                    raise UserError(
                        "Битрикс24 перенаправил скачивание на другой адрес. Загрузите файл вручную."
                    )
                continue
            if response.status_code != 200:
                raise UserError("Таблица Битрикс24 недоступна. Проверьте ссылку и разрешение на скачивание.")
            result = bytearray()
            for part in response.iter_bytes():
                result.extend(part)
                if len(result) > limit:
                    raise UserError("Таблица превышает ограничение размера. Загрузите файл до 12 МБ.")
            return bytes(result)
    raise UserError("Слишком много перенаправлений при скачивании таблицы.")


def public_bitrix(client, url):
    portal = bitrix_link(url)
    url = str(url).strip()
    try:
        html = read_response(client, url, 2 * 1024 * 1024, "bitrix24public.com").decode("utf-8")
        match = re.search(r'"document"\s*:\s*(\{)', html)
        if not match:
            raise UserError("Откройте публичный доступ к таблице Битрикс24 или загрузите Excel-файл.")
        document = json.JSONDecoder().raw_decode(html[match.start(1) :])[0]
        if not document.get("permissions", {}).get("download"):
            raise UserError("Владелец запретил скачивание этой таблицы.")
        if document.get("fileType", "").lower() != "xlsx":
            raise UserError("Для этой ссылки нужна таблица XLSX. Другой формат загрузите файлом.")
        body = read_response(
            client, download_path(html, portal), MAX_FILE, "bitrix24public.com", headers={"Referer": url}
        )
        if not body.startswith(b"PK"):
            raise UserError("Битрикс24 вернул страницу вместо Excel-файла. Проверьте публичный доступ.")
        filename = (
            str(document.get("title") or "Таблица Битрикс24.xlsx").replace("/", "_").replace("\\", "_")[:160]
        )
        if not filename.lower().endswith(".xlsx"):
            filename += ".xlsx"
        sheets = read_file(filename, body)
        return filename, body, sheets, {"provider": "bitrix", "portal": portal, "sync": "copy"}
    except (httpx.HTTPError, UnicodeError, ValueError, TypeError, KeyError):
        raise UserError(
            "Не удалось загрузить таблицу Битрикс24. Проверьте ссылку или загрузите Excel-файл."
        ) from None
