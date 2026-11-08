"""Раскладка листа и запросы оформления — без сети, с фейковым клиентом."""

import random
from datetime import date, datetime

import pytest

from data_generator import ALL_OWNERS, PAID, ReportParams, generate_rows
from report_builder import (
    FIRST_DATA_ROW,
    HEADER_ROW,
    build_report,
    build_requests,
    date_to_serial,
    plan_layout,
    sheet_title,
    unique_title,
)

NOW = datetime(2026, 11, 8, 19, 45)


def make_params(rows_count=10):
    return ReportParams(date(2026, 3, 1), date(2026, 3, 31), "Сделки", ALL_OWNERS, rows_count, "ООО «Пример»")


class FakeSheets:
    """Записывает вызовы вместо обращения к Google API."""

    def __init__(self, existing=(), fail_on=None):
        self.existing = list(existing)
        self.fail_on = fail_on
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if name == self.fail_on:
            raise RuntimeError(f"сбой в {name}")

    def get_sheet_names(self):
        return self.existing

    def add_sheet(self, title, rows=1000, cols=26):
        self._record("add_sheet", title, rows, cols)
        return 555

    def delete_sheet_by_id(self, sheet_id):
        self._record("delete_sheet_by_id", sheet_id)

    def write_ranges(self, data):
        self._record("write_ranges", data)

    def write_formula_ranges(self, data):
        self._record("write_formula_ranges", data)

    def batch_update(self, requests):
        self._record("batch_update", requests)

    def sheet_url(self, sheet_id=None):
        return f"https://example.test/#gid={sheet_id}"


def test_sheet_title_format():
    assert sheet_title(date(2026, 3, 1), date(2026, 3, 31), NOW) == "Отчёт 01.03.2026–31.03.2026 19-45"


def test_unique_title_adds_number():
    assert unique_title("Отчёт", []) == "Отчёт"
    assert unique_title("Отчёт", ["Отчёт", "Отчёт (2)"]) == "Отчёт (3)"


def test_date_to_serial_matches_sheets_epoch():
    assert date_to_serial(date(1899, 12, 31)) == 1
    assert date_to_serial(date(2026, 1, 1)) == 46023


def test_layout_rows_and_formulas_reference_data_range():
    params = make_params(rows_count=10)
    rows = generate_rows(params, random.Random(1))
    layout = plan_layout(params, rows, NOW)

    last = FIRST_DATA_ROW + 9
    assert layout.data_last_row == last
    assert layout.total_row == last + 1
    assert layout.average_row == last + 2

    formulas = dict(layout.formulas)
    assert formulas[f"F{last + 1}"] == [[f"=SUM(F{FIRST_DATA_ROW}:F{last})"]]
    assert formulas[f"F{last + 2}"] == [[f"=AVERAGE(F{FIRST_DATA_ROW}:F{last})"]]
    kpi = formulas[f"A6:G6"][0]
    assert kpi[0] == f'=SUMIF(G{FIRST_DATA_ROW}:G{last},"{PAID}",F{FIRST_DATA_ROW}:F{last})'
    assert all(cell == "" or cell.startswith("=") for cell in kpi)


def test_layout_data_values_are_typed():
    params = make_params(rows_count=3)
    rows = generate_rows(params, random.Random(2))
    data = dict(plan_layout(params, rows, NOW).values)[f"A{FIRST_DATA_ROW}:G{FIRST_DATA_ROW + 2}"]

    assert [row[0] for row in data] == [1, 2, 3]
    assert data[0][1] == date_to_serial(rows[0].date)
    assert isinstance(data[0][5], float)


def test_column_widths_fit_content():
    params = make_params(rows_count=5)
    layout = plan_layout(params, generate_rows(params, random.Random(3)), NOW)
    assert len(layout.column_widths) == 7
    assert layout.column_widths[0] < layout.column_widths[2]  # «№» уже, чем «Клиент»


def test_build_requests_contains_document_formatting():
    params = make_params(rows_count=4)
    layout = plan_layout(params, generate_rows(params, random.Random(4)), NOW)
    requests = build_requests(7, layout)
    kinds = [next(iter(r)) for r in requests]

    assert kinds.count("addBanding") == 1
    assert kinds.count("addConditionalFormatRule") == 3
    assert kinds.count("mergeCells") == 8

    freeze = requests[0]["updateSheetProperties"]["properties"]["gridProperties"]
    assert freeze == {"frozenRowCount": HEADER_ROW, "hideGridlines": True}

    title_merge = next(r for r in requests if "mergeCells" in r)["mergeCells"]["range"]
    assert title_merge == {"sheetId": 7, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 7}

    widths = [r for r in requests if r.get("updateDimensionProperties", {}).get("range", {}).get("dimension") == "COLUMNS"]
    assert len(widths) == 7

    currency = [
        r for r in requests
        if "repeatCell" in r
        and r["repeatCell"]["cell"]["userEnteredFormat"].get("numberFormat", {}).get("type") == "CURRENCY"
    ]
    assert currency, "должен быть денежный формат"


def test_build_report_calls_api_in_order():
    sheets = FakeSheets(existing=["Лист1"])
    params = make_params(rows_count=5)
    result = build_report(sheets, params, generate_rows(params, random.Random(5)), now=NOW)

    assert [call[0] for call in sheets.calls] == ["add_sheet", "write_ranges", "write_formula_ranges", "batch_update"]
    assert result.sheet_title == "Отчёт 01.03.2026–31.03.2026 19-45"
    assert result.sheet_id == 555
    assert result.url.endswith("gid=555")
    first_range = sheets.calls[1][1][0][0]
    assert first_range == "'Отчёт 01.03.2026–31.03.2026 19-45'!A1"


def test_build_report_deletes_sheet_on_failure():
    sheets = FakeSheets(fail_on="batch_update")
    params = make_params(rows_count=2)
    with pytest.raises(RuntimeError):
        build_report(sheets, params, generate_rows(params, random.Random(6)), now=NOW)
    assert sheets.calls[-1] == ("delete_sheet_by_id", 555)


def test_build_report_reports_progress():
    events = []
    params = make_params(rows_count=2)
    build_report(FakeSheets(), params, generate_rows(params, random.Random(7)), now=NOW,
                 progress=lambda p, m: events.append(p))
    assert events == sorted(events) and events[-1] == 100
