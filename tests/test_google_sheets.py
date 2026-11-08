"""GoogleSheets с замоканным API: режимы записи, ошибки, запросы batchUpdate."""

from unittest.mock import MagicMock

import pytest

from google_sheets import (
    CredentialsFileNotFoundError,
    GoogleSheets,
    a1,
    a1_to_grid_range,
    borders_request,
    column_to_index,
    format_request,
    hex_to_color,
    index_to_column,
    quote_sheet,
    sheet_properties_request,
)

SPREADSHEET_ID = "test-spreadsheet-id"


@pytest.fixture
def service():
    return MagicMock()


@pytest.fixture
def sheets(service):
    return GoogleSheets(spreadsheet_id=SPREADSHEET_ID, service=service)


def values_api(service):
    return service.spreadsheets.return_value.values.return_value


# --- защита от формул ---


def test_write_range_uses_raw_so_plus_stays_text(sheets, service):
    rows = [["+7 999 123-45-67", "-скидка 10%", "=не формула"]]
    sheets.write_range("'Лист1'!A1", rows)

    kwargs = values_api(service).update.call_args.kwargs
    assert kwargs["valueInputOption"] == "RAW"
    assert kwargs["body"]["values"] == rows
    assert kwargs["spreadsheetId"] == SPREADSHEET_ID


def test_append_rows_and_update_cell_use_raw(sheets, service):
    sheets.append_rows("Лист1", [["+1"]])
    sheets.update_cell("'Лист1'!B2", "+2")

    assert values_api(service).append.call_args.kwargs["valueInputOption"] == "RAW"
    assert values_api(service).update.call_args.kwargs["valueInputOption"] == "RAW"


def test_write_ranges_batch_uses_raw(sheets, service):
    sheets.write_ranges([("'Л'!A1", [["+1"]]), ("'Л'!B1", [[None]])])

    body = values_api(service).batchUpdate.call_args.kwargs["body"]
    assert body["valueInputOption"] == "RAW"
    assert body["data"][1]["values"] == [[""]]


def test_write_formula_uses_user_entered(sheets, service):
    sheets.write_formula("'Лист1'!C10", "=SUM(C2:C9)")

    kwargs = values_api(service).update.call_args.kwargs
    assert kwargs["valueInputOption"] == "USER_ENTERED"
    assert kwargs["body"]["values"] == [["=SUM(C2:C9)"]]


def test_write_formula_rejects_plain_text(sheets, service):
    with pytest.raises(ValueError):
        sheets.write_formula("A1", "+7 999")
    values_api(service).update.assert_not_called()


# --- листы и чтение ---


def test_get_sheet_names_and_id(sheets, service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"title": "Лист1", "sheetId": 0}},
            {"properties": {"title": "Отчёт", "sheetId": 77}},
        ]
    }
    assert sheets.get_sheet_names() == ["Лист1", "Отчёт"]
    assert sheets.get_sheet_id("Отчёт") == 77


def test_add_sheet_returns_new_id(sheets, service):
    service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [{"addSheet": {"properties": {"sheetId": 123}}}]
    }
    assert sheets.add_sheet("Новый") == 123
    body = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    assert body["requests"][0]["addSheet"]["properties"]["title"] == "Новый"


def test_read_all_cells_quotes_sheet_and_cleans(sheets, service):
    values_api(service).get.return_value.execute.return_value = {
        "values": [["Сумма", "1\xa0500,00\xa0₽"]]
    }
    assert sheets.read_all_cells("Мой лист") == [["Сумма", "1 500,00 ₽"]]
    assert values_api(service).get.call_args.kwargs["range"] == "'Мой лист'"


def test_empty_batch_update_does_not_call_api(sheets, service):
    assert sheets.batch_update([]) == {"replies": []}
    service.spreadsheets.return_value.batchUpdate.assert_not_called()


# --- ошибки ---


def test_missing_key_file_gives_clear_error(tmp_path):
    with pytest.raises(CredentialsFileNotFoundError, match="не найден"):
        GoogleSheets(credentials_path=tmp_path / "nope.json", spreadsheet_id="x")


def _http_error(status):
    httplib2 = pytest.importorskip("httplib2")
    errors = pytest.importorskip("googleapiclient.errors")
    response = httplib2.Response({"status": status, "content-type": "application/json"})
    return errors.HttpError(response, b'{"error": {"message": "boom"}}')


def test_403_means_service_account_has_no_access(sheets, service):
    from google_sheets import PermissionDeniedError

    values_api(service).get.return_value.execute.side_effect = _http_error(403)
    with pytest.raises(PermissionDeniedError, match="редактора"):
        sheets.read_range("A1")


def test_404_means_wrong_spreadsheet_id(sheets, service):
    from google_sheets import SpreadsheetNotFoundError

    values_api(service).get.return_value.execute.side_effect = _http_error(404)
    with pytest.raises(SpreadsheetNotFoundError, match=SPREADSHEET_ID):
        sheets.read_range("A1")


# --- A1 и сборщики запросов ---


def test_column_letters_roundtrip():
    for letters, index in (("A", 0), ("Z", 25), ("AA", 26), ("AZ", 51)):
        assert column_to_index(letters) == index
        assert index_to_column(index) == letters


def test_quote_sheet_escapes_apostrophe():
    assert quote_sheet("Отчёт Д'Артаньяна") == "'Отчёт Д''Артаньяна'"
    assert a1("Лист 1", "A1:B2") == "'Лист 1'!A1:B2"


def test_a1_to_grid_range():
    assert a1_to_grid_range(5, "B2:D10") == {
        "sheetId": 5,
        "startRowIndex": 1,
        "endRowIndex": 10,
        "startColumnIndex": 1,
        "endColumnIndex": 4,
    }
    assert a1_to_grid_range(0, "C3") == {
        "sheetId": 0,
        "startRowIndex": 2,
        "endRowIndex": 3,
        "startColumnIndex": 2,
        "endColumnIndex": 3,
    }
    assert a1_to_grid_range(0, "A:C") == {"sheetId": 0, "startColumnIndex": 0, "endColumnIndex": 3}


@pytest.mark.parametrize("bad", ["", "A0", "1A", "A1:B2:C3"])
def test_a1_to_grid_range_rejects_garbage(bad):
    with pytest.raises(ValueError):
        a1_to_grid_range(0, bad)


def test_hex_to_color():
    assert hex_to_color("#FF0000") == {"red": 1.0, "green": 0.0, "blue": 0.0}
    with pytest.raises(ValueError):
        hex_to_color("red")


def test_format_request_lists_only_given_fields():
    request = format_request({"sheetId": 1}, bold=True, bg_color="#FFFFFF", number_format="currency")
    body = request["repeatCell"]
    assert set(body["fields"].split(",")) == {
        "userEnteredFormat.backgroundColor",
        "userEnteredFormat.textFormat.bold",
        "userEnteredFormat.numberFormat",
    }
    assert body["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "CURRENCY"


def test_format_request_without_options_is_an_error():
    with pytest.raises(ValueError):
        format_request({"sheetId": 1})


def test_borders_request_sides():
    request = borders_request({"sheetId": 1}, sides=("top",), inner=False)["updateBorders"]
    assert "top" in request and "bottom" not in request and "innerHorizontal" not in request


def test_freeze_rows_builds_sheet_properties(sheets, service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Лист1", "sheetId": 9}}]
    }
    sheets.freeze_rows("Лист1", 3)

    body = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]
    assert body["requests"] == [sheet_properties_request(9, frozen_rows=3)]
    assert body["requests"][0]["updateSheetProperties"]["fields"] == "gridProperties.frozenRowCount"


def test_set_column_width_range(sheets, service):
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Лист1", "sheetId": 0}}]
    }
    sheets.set_column_width("Лист1", "B:D", 150)

    request = service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
    dims = request["updateDimensionProperties"]
    assert dims["range"]["startIndex"] == 1 and dims["range"]["endIndex"] == 4
    assert dims["properties"]["pixelSize"] == 150
