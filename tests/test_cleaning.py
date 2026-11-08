"""Очистка прочитанных значений: \xa0 из денежного формата и лишние пробелы."""

from google_sheets import clean_value, clean_values, parse_number


def test_nbsp_from_currency_format_becomes_regular_space():
    assert clean_value("12\xa0345,00\xa0₽") == "12 345,00 ₽"


def test_narrow_nbsp_and_repeated_spaces_are_collapsed():
    assert clean_value("  1 000   000 \t ") == "1 000 000"


def test_non_strings_are_kept_as_is():
    assert clean_value(42) == 42
    assert clean_value(None) is None


def test_clean_values_handles_ragged_rows():
    rows = [["Иван\xa0 ", " 1\xa0000 ₽"], ["  "], []]
    assert clean_values(rows) == [["Иван", "1 000 ₽"], [""], []]


def test_parse_number_russian_currency():
    assert parse_number("12\xa0345,50\xa0₽") == 12345.5


def test_parse_number_english_format():
    assert parse_number("12,345.50") == 12345.5


def test_parse_number_returns_none_for_text():
    assert parse_number("Оплачено") is None
    assert parse_number(True) is None
