"""Настройки проекта: читаются из .env.local, ключи и ID в коде не хранятся."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env.local"
DEFAULT_COMPANY = "ООО «Пример»"

# Если в SPREADSHEET_ID вставили весь URL таблицы, достаём из него ID.
_ID_FROM_URL = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


class ConfigError(Exception):
    """Не хватает настроек или файла ключа."""


@dataclass(frozen=True)
class Settings:
    credentials_path: Path
    spreadsheet_id: str
    company_name: str


def normalize_spreadsheet_id(value: str) -> str:
    value = value.strip()
    match = _ID_FROM_URL.search(value)
    return match.group(1) if match else value


def load_settings(env_file: Path = ENV_FILE, *, require_credentials: bool = True) -> Settings:
    """Читает .env.local и проверяет, что всё нужное на месте.

    При ошибке поднимает ConfigError со списком того, чего не хватает.
    """
    load_dotenv(env_file)

    credentials_raw = os.getenv("GOOGLE_CREDENTIALS_PATH", "").strip()
    spreadsheet_id = normalize_spreadsheet_id(os.getenv("SPREADSHEET_ID", ""))
    company = os.getenv("REPORT_COMPANY_NAME", "").strip() or DEFAULT_COMPANY

    credentials_path = Path(credentials_raw or "service_account.json")
    if not credentials_path.is_absolute():
        credentials_path = BASE_DIR / credentials_path

    problems = []
    if not credentials_raw:
        problems.append("GOOGLE_CREDENTIALS_PATH — путь к JSON-ключу сервисного аккаунта")
    elif require_credentials and not credentials_path.is_file():
        problems.append(
            f"файл ключа не найден: {credentials_path} — скачайте JSON-ключ "
            "(README, шаг 4) и положите его в корень проекта"
        )
    if not spreadsheet_id:
        problems.append("SPREADSHEET_ID — ID таблицы (часть URL между /d/ и /edit)")

    if problems:
        message = "Не хватает настроек:\n" + "\n".join(f"  • {p}" for p in problems)
        if not env_file.is_file():
            message += (
                f"\nФайл {env_file.name} не найден: скопируйте .env.local.example "
                "в .env.local и заполните его."
            )
        raise ConfigError(message)

    return Settings(credentials_path, spreadsheet_id, company)
