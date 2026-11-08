"""Проверка настроек из .env.local."""

import pytest

from config import ConfigError, load_settings, normalize_spreadsheet_id

VARS = ("GOOGLE_CREDENTIALS_PATH", "SPREADSHEET_ID", "REPORT_COMPANY_NAME")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in VARS:
        monkeypatch.delenv(name, raising=False)


def test_missing_variables_are_listed(tmp_path):
    with pytest.raises(ConfigError) as error:
        load_settings(tmp_path / ".env.local")
    message = str(error.value)
    assert "GOOGLE_CREDENTIALS_PATH" in message
    assert "SPREADSHEET_ID" in message
    assert ".env.local.example" in message


def test_missing_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH", str(tmp_path / "service_account.json"))
    monkeypatch.setenv("SPREADSHEET_ID", "abc")
    with pytest.raises(ConfigError, match="файл ключа не найден"):
        load_settings(tmp_path / ".env.local")


def test_settings_loaded_from_env_file(tmp_path):
    key = tmp_path / "key.json"
    key.write_text("{}")
    env = tmp_path / ".env.local"
    env.write_text(f"GOOGLE_CREDENTIALS_PATH={key}\nSPREADSHEET_ID=abc123\n", encoding="utf-8")

    settings = load_settings(env)
    assert settings.credentials_path == key
    assert settings.spreadsheet_id == "abc123"
    assert settings.company_name == "ООО «Пример»"


def test_full_url_is_reduced_to_id():
    url = "https://docs.google.com/spreadsheets/d/1AbC-d_E/edit#gid=0"
    assert normalize_spreadsheet_id(url) == "1AbC-d_E"
    assert normalize_spreadsheet_id(" 1AbC ") == "1AbC"
