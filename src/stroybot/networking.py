"""Only AI and Telegram opt into a local proxy; cloud storage stays direct."""

import os
from urllib.parse import urlsplit

import httpx

from .domain import UserError


def proxy_url(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "socks5"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or not parsed.port
    ):
        raise UserError(f"{name}: допустим только локальный SOCKS5-прокси без пароля.")
    return value


def ai_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(90, connect=15), proxy=proxy_url("AI_PROXY_URL"), trust_env=False
    )


def telegram_bot(token: str):
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession

    return Bot(token, session=AiohttpSession(proxy=proxy_url("TELEGRAM_PROXY_URL"), timeout=30))
