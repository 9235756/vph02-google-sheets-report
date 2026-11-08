"""Валидация полей и генерация данных."""

import random
from datetime import date

import pytest

from data_generator import (
    ALL_OWNERS,
    DEPARTMENTS,
    PAID,
    REPORT_TYPES,
    STATUSES,
    ReportParams,
    ReportRow,
    ValidationError,
    format_money,
    generate_rows,
    managers_for,
    owner_choices,
    parse_date,
    summarize,
    validate_period,
    validate_rows_count,
)


def make_params(**overrides):
    values = dict(
        date_from=date(2026, 3, 1),
        date_to=date(2026, 3, 31),
        report_type="Продажи",
        owner=ALL_OWNERS,
        rows_count=40,
        company="ООО «Пример»",
    )
    values.update(overrides)
    return ReportParams(**values)


# --- даты ---


def test_parse_date_valid():
    assert parse_date(" 05.03.2026 ") == date(2026, 3, 5)


@pytest.mark.parametrize("text", ["2026-03-05", "5.3.2026", "05/03/2026", "", "05.03.26"])
def test_parse_date_rejects_wrong_format(text):
    with pytest.raises(ValidationError, match="ДД.ММ.ГГГГ"):
        parse_date(text)


def test_parse_date_rejects_impossible_date():
    with pytest.raises(ValidationError, match="не существует"):
        parse_date("31.02.2026")


def test_validate_period_ok_and_same_day():
    assert validate_period("01.03.2026", "31.03.2026") == (date(2026, 3, 1), date(2026, 3, 31))
    assert validate_period("10.03.2026", "10.03.2026")[0] == date(2026, 3, 10)


def test_validate_period_start_after_end():
    with pytest.raises(ValidationError, match="не позже"):
        validate_period("01.04.2026", "31.03.2026")


@pytest.mark.parametrize("value", ["0", "1001", "abc", "", "2.5"])
def test_validate_rows_count_rejects(value):
    with pytest.raises(ValidationError):
        validate_rows_count(value)


def test_validate_rows_count_ok():
    assert validate_rows_count(" 25 ") == 25


def test_params_validate_company_and_type():
    with pytest.raises(ValidationError):
        make_params(company="  ")
    with pytest.raises(ValidationError):
        make_params(report_type="Склад")


# --- менеджеры и отделы ---


def test_owner_choices_start_with_all():
    choices = owner_choices()
    assert choices[0] == ALL_OWNERS
    assert set(DEPARTMENTS) <= set(choices)


def test_managers_for_department_and_single_manager():
    department, managers = next(iter(DEPARTMENTS.items()))
    assert managers_for(department) == managers
    assert managers_for(managers[0]) == (managers[0],)
    with pytest.raises(ValidationError):
        managers_for("Неизвестный")


# --- генерация ---


@pytest.mark.parametrize("report_type", REPORT_TYPES)
def test_generate_rows_shape_and_bounds(report_type):
    params = make_params(report_type=report_type)
    rows = generate_rows(params, random.Random(1))

    assert len(rows) == params.rows_count
    assert [r.date for r in rows] == sorted(r.date for r in rows)
    for row in rows:
        assert params.date_from <= row.date <= params.date_to
        assert row.status in STATUSES
        assert row.amount > 0 and row.amount % 100 == 0


def test_generate_rows_respects_department_filter():
    department, managers = next(iter(DEPARTMENTS.items()))
    rows = generate_rows(make_params(owner=department), random.Random(2))
    assert {row.manager for row in rows} <= set(managers)


def test_generate_rows_is_reproducible_with_seed():
    params = make_params()
    assert generate_rows(params, random.Random(7)) == generate_rows(params, random.Random(7))


def test_generate_rows_single_day_period():
    day = date(2026, 3, 10)
    rows = generate_rows(make_params(date_from=day, date_to=day, rows_count=5), random.Random(3))
    assert {row.date for row in rows} == {day}


# --- итоги ---


def _row(amount, status):
    return ReportRow(date(2026, 3, 1), "ООО «Тест»", "Орлова А. В.", "Сделка", amount, status)


def test_summarize_counts_only_paid_revenue():
    rows = [_row(1000, PAID), _row(3000, PAID), _row(5000, "В работе"), _row(700, "Отменено")]
    summary = summarize(rows)
    assert summary.revenue == 4000
    assert summary.deals_count == 4
    assert summary.paid_count == 2
    assert summary.average_check == 2000
    assert summary.conversion == 0.5


def test_summarize_without_paid_deals():
    summary = summarize([_row(1000, "Отменено")])
    assert summary.revenue == 0 and summary.average_check == 0 and summary.conversion == 0


def test_format_money():
    assert format_money(1234567.5) == "1 234 567,50 ₽"
