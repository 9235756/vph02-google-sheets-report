"""Работа с Google Таблицей через Google Sheets API v4.

Авторизация через сервисный аккаунт (JSON-ключ). Модуль переиспользуемый:
класс GoogleSheets даёт CRUD-операции и форматирование, а функции *_request
собирают запросы batchUpdate без обращения к сети — их удобно тестировать
и складывать в один пакет.

Запуск напрямую выводит список листов и содержимое первого листа:
    python google_sheets.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import ConfigError, load_settings

try:
    import httplib2
    from google.auth.exceptions import RefreshError, TransportError
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    _NETWORK_ERRORS: tuple[type[BaseException], ...] = (
        OSError,
        TransportError,
        httplib2.HttpLib2Error,
    )
    _MISSING_DEPENDENCY: ImportError | None = None
except ImportError as _exc:
    # Без библиотек Google модуль всё равно импортируется: чистые функции
    # и тесты логики работают, а подключение к API объяснит, что поставить.
    _MISSING_DEPENDENCY = _exc
    _NETWORK_ERRORS = (OSError,)

    class HttpError(Exception):  # type: ignore[no-redef]
        resp = None

    class RefreshError(Exception):  # type: ignore[no-redef]
        pass


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

NUMBER_FORMATS = {
    "number": {"type": "NUMBER", "pattern": "#,##0"},
    "decimal": {"type": "NUMBER", "pattern": "#,##0.00"},
    "currency": {"type": "CURRENCY", "pattern": '#,##0.00 "₽"'},
    "percent": {"type": "PERCENT", "pattern": "0.0%"},
    "date": {"type": "DATE", "pattern": "dd.mm.yyyy"},
    "text": {"type": "TEXT"},
}


# --- Исключения -------------------------------------------------------------


class GoogleSheetsError(Exception):
    """Базовая ошибка модуля."""


class CredentialsFileNotFoundError(GoogleSheetsError):
    """Нет файла JSON-ключа."""


class InvalidCredentialsError(GoogleSheetsError):
    """Файл ключа повреждён, пустой или ключ отозван."""


class PermissionDeniedError(GoogleSheetsError):
    """Сервисный аккаунт не добавлен редактором или API не включён."""


class SpreadsheetNotFoundError(GoogleSheetsError):
    """Неверный ID таблицы."""


class SheetNotFoundError(GoogleSheetsError):
    """Нет листа или диапазона с таким именем."""


class SheetsRequestError(GoogleSheetsError):
    """Прочие ошибки API."""


# --- Очистка прочитанных данных --------------------------------------------

# \xa0 — неразрывный пробел, которым Sheets разделяет разряды в денежном
# формате;   и   — его узкие родственники.
_NBSP_CHARS = ("\xa0", " ", " ")
_SPACES = re.compile(r"[ \t]+")


def clean_value(value: Any) -> Any:
    """Неразрывные пробелы → обычные, повторы схлопнуты, края обрезаны."""
    if not isinstance(value, str):
        return value
    for char in _NBSP_CHARS:
        value = value.replace(char, " ")
    return _SPACES.sub(" ", value).strip()


def clean_values(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    return [[clean_value(cell) for cell in row] for row in rows]


def parse_number(value: Any) -> float | None:
    """«12 345,50 ₽» → 12345.5. Проценты не делятся на 100: «12,5%» → 12.5."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = re.sub(r"[^\d,.\-]", "", clean_value(value))
    if "," in text and "." in text:
        # Разделитель дробной части — тот, что стоит последним.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


# --- A1-нотация -------------------------------------------------------------

_CELL_RE = re.compile(r"^([A-Za-z]*)(\d*)$")


def column_to_index(letters: str) -> int:
    """«A» → 0, «Z» → 25, «AA» → 26."""
    if not letters or not letters.isalpha():
        raise ValueError(f"Некорректная буква колонки: {letters!r}")
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def index_to_column(index: int) -> str:
    if index < 0:
        raise ValueError("Индекс колонки не может быть отрицательным")
    number, letters = index + 1, ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def quote_sheet(title: str) -> str:
    """Имя листа для A1-нотации: в кавычках, апостроф удваивается."""
    return "'" + title.replace("'", "''") + "'"


def a1(sheet: str, cell_range: str) -> str:
    return f"{quote_sheet(sheet)}!{cell_range}"


def _parse_cell(ref: str) -> tuple[int | None, int | None]:
    match = _CELL_RE.match(ref.strip())
    if not match or not (match.group(1) or match.group(2)):
        raise ValueError(f"Некорректная ссылка на ячейку: {ref!r}")
    letters, digits = match.groups()
    column = column_to_index(letters) if letters else None
    row = int(digits) - 1 if digits else None
    if row is not None and row < 0:
        raise ValueError(f"Строки нумеруются с 1: {ref!r}")
    return column, row


def a1_to_grid_range(sheet_id: int, cell_range: str) -> dict:
    """«B2:D10» → GridRange с индексами от нуля и открытым концом."""
    parts = cell_range.split(":")
    if len(parts) > 2:
        raise ValueError(f"Некорректный диапазон: {cell_range!r}")
    start_col, start_row = _parse_cell(parts[0])
    end_col, end_row = _parse_cell(parts[-1])
    grid: dict[str, int] = {"sheetId": sheet_id}
    if start_row is not None:
        grid["startRowIndex"] = start_row
    if end_row is not None:
        grid["endRowIndex"] = end_row + 1
    if start_col is not None:
        grid["startColumnIndex"] = start_col
    if end_col is not None:
        grid["endColumnIndex"] = end_col + 1
    return grid


def _span(spec: str, kind: str) -> tuple[int, int]:
    """«A:C» → (0, 3) для колонок, «2:4» → (1, 4) для строк."""
    parts = spec.split(":")
    if kind == "COLUMNS":
        indexes = [column_to_index(p.strip()) for p in parts]
    else:
        indexes = [int(p) - 1 for p in parts]
    return indexes[0], indexes[-1] + 1


def hex_to_color(value: str) -> dict:
    """«#1F3864» → {"red": …, "green": …, "blue": …} в долях единицы."""
    text = value.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        raise ValueError(f"Цвет задаётся как #RRGGBB, получено {value!r}")
    red, green, blue = (int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": round(red, 4), "green": round(green, 4), "blue": round(blue, 4)}


def resolve_number_format(value: str | dict) -> dict:
    if isinstance(value, dict):
        return value
    if value in NUMBER_FORMATS:
        return dict(NUMBER_FORMATS[value])
    return {"type": "NUMBER", "pattern": value}


# --- Сборщики запросов batchUpdate -----------------------------------------


def format_request(
    grid: dict,
    *,
    bg_color: str | None = None,
    font_color: str | None = None,
    font_size: int | None = None,
    font_family: str | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    h_align: str | None = None,
    v_align: str | None = None,
    wrap: bool | None = None,
    number_format: str | dict | None = None,
) -> dict:
    """repeatCell, который меняет только переданные свойства формата."""
    fmt: dict[str, Any] = {}
    text_format: dict[str, Any] = {}
    fields = []

    if bg_color is not None:
        fmt["backgroundColor"] = hex_to_color(bg_color)
        fields.append("userEnteredFormat.backgroundColor")
    for key, value in (
        ("foregroundColor", hex_to_color(font_color) if font_color else None),
        ("fontSize", font_size),
        ("fontFamily", font_family),
        ("bold", bold),
        ("italic", italic),
    ):
        if value is not None:
            text_format[key] = value
            fields.append(f"userEnteredFormat.textFormat.{key}")
    if text_format:
        fmt["textFormat"] = text_format
    if h_align is not None:
        fmt["horizontalAlignment"] = h_align.upper()
        fields.append("userEnteredFormat.horizontalAlignment")
    if v_align is not None:
        fmt["verticalAlignment"] = v_align.upper()
        fields.append("userEnteredFormat.verticalAlignment")
    if wrap is not None:
        fmt["wrapStrategy"] = "WRAP" if wrap else "OVERFLOW_CELL"
        fields.append("userEnteredFormat.wrapStrategy")
    if number_format is not None:
        fmt["numberFormat"] = resolve_number_format(number_format)
        fields.append("userEnteredFormat.numberFormat")

    if not fields:
        raise ValueError("Не задано ни одного параметра форматирования")
    return {
        "repeatCell": {
            "range": grid,
            "cell": {"userEnteredFormat": fmt},
            "fields": ",".join(fields),
        }
    }


def merge_request(grid: dict, merge_type: str = "MERGE_ALL") -> dict:
    return {"mergeCells": {"range": grid, "mergeType": merge_type}}


def borders_request(
    grid: dict,
    *,
    style: str = "SOLID",
    color: str = "#BFBFBF",
    sides: Sequence[str] = ("top", "bottom", "left", "right"),
    inner: bool = True,
) -> dict:
    border = {"style": style, "color": hex_to_color(color)}
    body: dict[str, Any] = {"range": grid}
    for side in sides:
        body[side] = border
    if inner:
        body["innerHorizontal"] = border
        body["innerVertical"] = border
    return {"updateBorders": body}


def dimension_size_request(
    sheet_id: int, dimension: str, start: int, end: int, pixels: int
) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": dimension,
                "startIndex": start,
                "endIndex": end,
            },
            "properties": {"pixelSize": pixels},
            "fields": "pixelSize",
        }
    }


def auto_resize_request(sheet_id: int, start: int, end: int) -> dict:
    return {
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start,
                "endIndex": end,
            }
        }
    }


def sheet_properties_request(
    sheet_id: int,
    *,
    frozen_rows: int | None = None,
    hide_gridlines: bool | None = None,
) -> dict:
    grid: dict[str, Any] = {}
    if frozen_rows is not None:
        grid["frozenRowCount"] = frozen_rows
    if hide_gridlines is not None:
        grid["hideGridlines"] = hide_gridlines
    if not grid:
        raise ValueError("Не задано ни одного свойства листа")
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": grid},
            "fields": ",".join(f"gridProperties.{key}" for key in grid),
        }
    }


def banding_request(grid: dict, first_color: str, second_color: str) -> dict:
    """Чередование цветов строк («зебра»)."""
    return {
        "addBanding": {
            "bandedRange": {
                "range": grid,
                "rowProperties": {
                    "firstBandColor": hex_to_color(first_color),
                    "secondBandColor": hex_to_color(second_color),
                },
            }
        }
    }


def text_rule_request(
    grid: dict,
    text: str,
    *,
    font_color: str | None = None,
    bg_color: str | None = None,
    bold: bool | None = None,
    index: int = 0,
) -> dict:
    """Условное форматирование: ячейка равна тексту → свой цвет."""
    fmt: dict[str, Any] = {}
    text_format: dict[str, Any] = {}
    if font_color:
        text_format["foregroundColor"] = hex_to_color(font_color)
    if bold is not None:
        text_format["bold"] = bold
    if text_format:
        fmt["textFormat"] = text_format
    if bg_color:
        fmt["backgroundColor"] = hex_to_color(bg_color)
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [grid],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": text}],
                    },
                    "format": fmt,
                },
            },
            "index": index,
        }
    }


# --- Значения для записи ----------------------------------------------------


def _as_rows(values: Iterable[Iterable[Any]]) -> list[list[Any]]:
    return [["" if cell is None else cell for cell in row] for row in values]


def _check_formulas(values: Iterable[Iterable[Any]]) -> None:
    for row in values:
        for cell in row:
            if isinstance(cell, str) and cell and not cell.startswith("="):
                raise ValueError(
                    f"Ожидалась формула, начинающаяся с «=», получено {cell!r}. "
                    "Обычный текст пишите через write_range."
                )


# --- Клиент -----------------------------------------------------------------


class GoogleSheets:
    """CRUD и форматирование одной Google Таблицы.

    Без аргументов путь к ключу и ID таблицы берутся из .env.local.
    Параметр service нужен тестам: туда подставляется mock вместо API.
    """

    def __init__(
        self,
        credentials_path: str | Path | None = None,
        spreadsheet_id: str | None = None,
        *,
        service: Any = None,
    ) -> None:
        if not spreadsheet_id or (service is None and credentials_path is None):
            settings = load_settings(
                require_credentials=service is None and credentials_path is None
            )
            spreadsheet_id = spreadsheet_id or settings.spreadsheet_id
            if credentials_path is None:
                credentials_path = settings.credentials_path

        self.spreadsheet_id = spreadsheet_id
        self.service_account_email: str | None = None
        self._service = service if service is not None else self._build_service(Path(credentials_path))

    def _build_service(self, credentials_path: Path) -> Any:
        if not credentials_path.is_file():
            raise CredentialsFileNotFoundError(
                f"Файл ключа сервисного аккаунта не найден: {credentials_path}. "
                "Скачайте JSON-ключ в Google Cloud Console (Keys → Add key → JSON) "
                "и укажите путь в GOOGLE_CREDENTIALS_PATH."
            )
        if _MISSING_DEPENDENCY is not None:
            raise GoogleSheetsError(
                "Не установлены библиотеки Google API: "
                f"{_MISSING_DEPENDENCY}. Выполните pip install -r requirements.txt"
            )
        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_path), scopes=SCOPES
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidCredentialsError(
                f"Файл {credentials_path.name} не похож на JSON-ключ сервисного "
                f"аккаунта или повреждён ({exc}). Скачайте ключ заново."
            ) from exc
        self.service_account_email = credentials.service_account_email
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    # --- выполнение запросов и перевод ошибок ---

    def _execute(self, request: Any) -> dict:
        try:
            return request.execute()
        except HttpError as exc:
            raise self._translate_http_error(exc) from exc
        except RefreshError as exc:
            raise InvalidCredentialsError(
                f"Google отклонил ключ сервисного аккаунта: {exc}. Ключ мог быть "
                "удалён или отозван — создайте новый JSON-ключ."
            ) from exc
        except _NETWORK_ERRORS as exc:
            raise GoogleSheetsError(f"Нет связи с Google API: {exc}") from exc

    def _translate_http_error(self, exc: HttpError) -> GoogleSheetsError:
        status = int(getattr(exc.resp, "status", 0) or 0)
        reason = str(getattr(exc, "reason", "") or exc)

        if status == 403:
            if "has not been used" in reason or "disabled" in reason.lower():
                return PermissionDeniedError(
                    "Google Sheets API не включён в проекте Google Cloud: "
                    "APIs & Services → Library → Google Sheets API → Enable."
                )
            who = self.service_account_email or "email сервисного аккаунта (client_email в JSON-ключе)"
            return PermissionDeniedError(
                "Нет доступа к таблице. Откройте её → «Настройки доступа» и "
                f"добавьте {who} с правами редактора."
            )
        if status == 404:
            return SpreadsheetNotFoundError(
                f"Таблица с ID «{self.spreadsheet_id}» не найдена. Проверьте "
                "SPREADSHEET_ID — это часть URL между /d/ и /edit."
            )
        if status == 400 and "Unable to parse range" in reason:
            return SheetNotFoundError(f"Лист или диапазон не найден: {reason}")
        if status == 429:
            return SheetsRequestError(
                "Превышена квота запросов Google Sheets API. Подождите минуту и повторите."
            )
        return SheetsRequestError(f"Google Sheets API вернул ошибку {status}: {reason}")

    def _spreadsheets(self) -> Any:
        return self._service.spreadsheets()

    def _values(self) -> Any:
        return self._service.spreadsheets().values()

    def sheet_url(self, sheet_id: int | None = None) -> str:
        url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"
        return url if sheet_id is None else f"{url}#gid={sheet_id}"

    # --- листы ---

    def get_sheets(self) -> list[dict]:
        response = self._execute(
            self._spreadsheets().get(
                spreadsheetId=self.spreadsheet_id, fields="sheets.properties"
            )
        )
        return [sheet["properties"] for sheet in response.get("sheets", [])]

    def get_sheet_names(self) -> list[str]:
        return [props["title"] for props in self.get_sheets()]

    def get_sheet_id(self, title: str) -> int:
        sheets = self.get_sheets()
        for props in sheets:
            if props["title"] == title:
                return props["sheetId"]
        names = ", ".join(f"«{p['title']}»" for p in sheets) or "нет листов"
        raise SheetNotFoundError(f"Лист «{title}» не найден. Есть: {names}")

    def add_sheet(self, title: str, rows: int = 1000, cols: int = 26) -> int:
        """Создаёт лист и возвращает его sheetId."""
        response = self.batch_update(
            [
                {
                    "addSheet": {
                        "properties": {
                            "title": title,
                            "gridProperties": {"rowCount": rows, "columnCount": cols},
                        }
                    }
                }
            ]
        )
        return response["replies"][0]["addSheet"]["properties"]["sheetId"]

    def delete_sheet(self, title: str) -> None:
        self.delete_sheet_by_id(self.get_sheet_id(title))

    def delete_sheet_by_id(self, sheet_id: int) -> None:
        self.batch_update([{"deleteSheet": {"sheetId": sheet_id}}])

    # --- чтение ---

    def read_all_cells(self, sheet: str) -> list[list[Any]]:
        return self.read_range(quote_sheet(sheet))

    def read_range(self, cell_range: str) -> list[list[Any]]:
        response = self._execute(
            self._values().get(spreadsheetId=self.spreadsheet_id, range=cell_range)
        )
        return clean_values(response.get("values", []))

    # --- запись ---
    # Текст пишется в режиме RAW: «+7 999…», «-скидка», «=не формула» остаются
    # строками и не превращаются в формулу с ERROR. Формулы — только через
    # write_formula / write_formulas в режиме USER_ENTERED.

    def write_range(self, cell_range: str, values: Iterable[Iterable[Any]]) -> dict:
        return self._execute(
            self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=cell_range,
                valueInputOption="RAW",
                body={"values": _as_rows(values)},
            )
        )

    def write_ranges(self, data: Iterable[tuple[str, Iterable[Iterable[Any]]]]) -> dict:
        """Несколько диапазонов текста одним запросом (RAW)."""
        return self._batch_write(data, "RAW")

    def write_formula(self, cell: str, formula: str) -> dict:
        return self.write_formulas(cell, [[formula]])

    def write_formulas(self, cell_range: str, values: Iterable[Iterable[Any]]) -> dict:
        rows = _as_rows(values)
        _check_formulas(rows)
        return self._execute(
            self._values().update(
                spreadsheetId=self.spreadsheet_id,
                range=cell_range,
                valueInputOption="USER_ENTERED",
                body={"values": rows},
            )
        )

    def write_formula_ranges(self, data: Iterable[tuple[str, Iterable[Iterable[Any]]]]) -> dict:
        """Несколько диапазонов формул одним запросом (USER_ENTERED)."""
        return self._batch_write(data, "USER_ENTERED")

    def _batch_write(self, data: Iterable[tuple[str, Iterable[Iterable[Any]]]], option: str) -> dict:
        payload = []
        for cell_range, values in data:
            rows = _as_rows(values)
            if option == "USER_ENTERED":
                _check_formulas(rows)
            payload.append({"range": cell_range, "values": rows})
        if not payload:
            return {}
        return self._execute(
            self._values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": option, "data": payload},
            )
        )

    def append_rows(self, sheet: str, rows: Iterable[Iterable[Any]]) -> dict:
        return self._execute(
            self._values().append(
                spreadsheetId=self.spreadsheet_id,
                range=quote_sheet(sheet),
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": _as_rows(rows)},
            )
        )

    def update_cell(self, cell: str, value: Any) -> dict:
        return self.write_range(cell, [[value]])

    def clear_range(self, cell_range: str) -> dict:
        return self._execute(
            self._values().clear(
                spreadsheetId=self.spreadsheet_id, range=cell_range, body={}
            )
        )

    # --- форматирование ---

    def batch_update(self, requests: list[dict]) -> dict:
        if not requests:
            return {"replies": []}
        return self._execute(
            self._spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            )
        )

    def _grid(self, sheet: str, cell_range: str) -> dict:
        return a1_to_grid_range(self.get_sheet_id(sheet), cell_range)

    def format_range(self, sheet: str, cell_range: str, **fmt: Any) -> dict:
        """Параметры — как у format_request: bg_color, font_color, font_size,
        bold, h_align, wrap, number_format («currency», «date», …)."""
        return self.batch_update([format_request(self._grid(sheet, cell_range), **fmt)])

    def merge_cells(self, sheet: str, cell_range: str, merge_type: str = "MERGE_ALL") -> dict:
        return self.batch_update([merge_request(self._grid(sheet, cell_range), merge_type)])

    def set_borders(self, sheet: str, cell_range: str, **options: Any) -> dict:
        return self.batch_update([borders_request(self._grid(sheet, cell_range), **options)])

    def set_column_width(self, sheet: str, columns: str, width: int) -> dict:
        """columns — «B» или «B:D», width — в пикселях."""
        start, end = _span(columns, "COLUMNS")
        return self.batch_update(
            [dimension_size_request(self.get_sheet_id(sheet), "COLUMNS", start, end, width)]
        )

    def set_row_height(self, sheet: str, rows: str, height: int) -> dict:
        """rows — «1» или «1:3», height — в пикселях."""
        start, end = _span(rows, "ROWS")
        return self.batch_update(
            [dimension_size_request(self.get_sheet_id(sheet), "ROWS", start, end, height)]
        )

    def freeze_rows(self, sheet: str, count: int) -> dict:
        return self.batch_update(
            [sheet_properties_request(self.get_sheet_id(sheet), frozen_rows=count)]
        )

    def auto_resize_columns(self, sheet: str, columns: str = "A:Z") -> dict:
        start, end = _span(columns, "COLUMNS")
        return self.batch_update([auto_resize_request(self.get_sheet_id(sheet), start, end)])

    def add_banding(self, sheet: str, cell_range: str, first_color: str, second_color: str) -> dict:
        return self.batch_update(
            [banding_request(self._grid(sheet, cell_range), first_color, second_color)]
        )


def main() -> int:
    try:
        sheets = GoogleSheets()
        names = sheets.get_sheet_names()
        print("Листы таблицы:")
        for number, name in enumerate(names, start=1):
            print(f"  {number}. {name}")
        if not names:
            return 0

        first = names[0]
        rows = sheets.read_all_cells(first)
        print(f"\nСодержимое листа «{first}» (строк: {len(rows)}):")
        if not rows:
            print("  (лист пустой)")
        for number, row in enumerate(rows, start=1):
            print(f"  {number:>3}: " + " | ".join(str(cell) for cell in row))
    except (ConfigError, GoogleSheetsError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
