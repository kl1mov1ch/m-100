import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .domain import UserError


@dataclass
class Settings:
    mode: str
    token: str
    allowed: set[int]
    timezone: ZoneInfo
    project: str
    database: Path
    llm_url: str
    llm_key: str
    llm_model: str
    stt_model: str
    spreadsheet: str
    sheet: str
    credentials: str
    bitrix_url: str
    category: int
    max_output_tokens: int = 2048

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(os.getenv("ENV_FILE", ".env"))
        env = os.environ
        return cls(
            env.get("STORAGE_MODE", "local"),
            env.get("TELEGRAM_BOT_TOKEN", ""),
            {int(v.strip()) for v in env.get("ALLOWED_USER_IDS", "").split(",") if v.strip()},
            ZoneInfo(env.get("TIMEZONE", "Europe/Moscow")),
            env.get("PROJECT_NAME", "M-100"),
            Path(env.get("DATABASE_PATH", "data/stroybot.sqlite3")),
            env.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            env.get("LLM_API_KEY", ""),
            env.get("LLM_MODEL", ""),
            env.get("STT_MODEL", "whisper-1"),
            env.get("GOOGLE_SPREADSHEET_ID", ""),
            env.get("GOOGLE_SHEET_NAME", "Лист1"),
            env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/google-service-account.json"),
            env.get("BITRIX_WEBHOOK_URL", "").rstrip("/") + "/",
            int(env.get("BITRIX_CATEGORY_ID", "0")),
            int(env.get("LLM_MAX_OUTPUT_TOKENS", "2048")),
        )

    def validate(self, telegram: bool = False) -> None:
        if self.mode not in {"local", "setup", "live", "google"}:
            raise UserError("STORAGE_MODE: допустимы local, setup, live и google.")
        if telegram and (not self.token or not self.allowed):
            raise UserError("Заполните TELEGRAM_BOT_TOKEN и ALLOWED_USER_IDS в .env.")
        if self.mode in {"setup", "live", "google"}:
            if not self.spreadsheet or not Path(self.credentials).is_file():
                raise UserError("Для облачного режима нужны Google-таблица и файл сервисного аккаунта.")
        if self.mode == "live":
            if self.bitrix_url == "/":
                raise UserError("Для live обязателен вебхук Битрикс24. До подключения используйте setup.")
            if not self.bitrix_url.startswith("https://"):
                raise UserError("Вебхук Битрикс24 должен использовать HTTPS.")
        if not 256 <= self.max_output_tokens <= 8192:
            raise UserError("LLM_MAX_OUTPUT_TOKENS должен быть от 256 до 8192.")
        if os.getenv("APP_ENV") == "production":
            if self.mode == "local" or os.getenv("MINIAPP_PREVIEW_TOKEN"):
                raise UserError("В production запрещены локальные данные и проверочный вход.")
            if not os.getenv("MINIAPP_URL", "").startswith("https://"):
                raise UserError("Для production нужен HTTPS-адрес MINIAPP_URL.")
