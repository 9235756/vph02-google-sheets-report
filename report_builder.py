"""Раскладка и оформление листа отчёта. Работает без tkinter.

Лист собирается за четыре запроса к API: создать лист, записать значения
(RAW), записать формулы (USER_ENTERED), применить всё оформление одним
batchUpdate. Если что-то упало посередине, недоделанный лист удаляется.

Раскладка (колонки A–G):
    1      название компании и тип отчёта
    2      период, менеджер или отдел, время формирования
    5–6    итоги: выручка, количество сделок, средний чек, конверсия
    8      шапка таблицы (закреплена)
    9…     детализация
    после  «Итого» (SUM) и «Среднее по сделке» (AVERAGE)
"""

from __future__ import annotations

import contextlib
import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Protocol, Sequence

from data_generator import (
    CANCELLED,
    IN_PROGRESS,
    PAID,
    REPORT_TITLES,
    ReportParams,
    ReportRow,
    ReportSummary,
    format_money,
    generate_rows,
    summarize,
)
from google_sheets import (
    a1,
    a1_to_grid_range,
    banding_request,
    borders_request,
    dimension_size_request,
    format_request,
    merge_request,
    sheet_properties_request,
    text_rule_request,
)

COLUMNS = ("№", "Дата", "Клиент", "Менеджер", "Сделка", "Сумма, ₽", "Статус")
LAST_COL = "G"

TITLE_ROW = 1
SUBTITLE_ROW = 2
KPI_LABEL_ROW = 5
KPI_VALUE_ROW = 6
HEADER_ROW = 8
FIRST_DATA_ROW = 9

# Палитра: тёмно-синий акцент, светлые подложки, приглушённый текст.
ACCENT = "#1F3864"
HEADER_BG = "#2F5496"
KPI_LABEL_BG = "#DEE6F2"
KPI_VALUE_BG = "#F5F8FC"
ZEBRA = "#F3F6FA"
TOTAL_BG = "#DCE3EF"
BORDER = "#C9D1DE"
MUTED = "#5F6B7A"
WHITE = "#FFFFFF"
STATUS_COLORS = {PAID: "#2E7D32", IN_PROGRESS: "#B26A00", CANCELLED: "#B3261E"}

# Ширина колонок по содержимому: примерно 7 px на символ шрифта Arial 10.
CHAR_PX, PADDING_PX, MIN_WIDTH, MAX_WIDTH = 7, 24, 48, 320

ProgressCallback = Callable[[int, str], None]


class SheetsClient(Protocol):
    """То, что report_builder использует из GoogleSheets."""

    def get_sheet_names(self) -> list[str]: ...
    def add_sheet(self, title: str, rows: int = ..., cols: int = ...) -> int: ...
    def delete_sheet_by_id(self, sheet_id: int) -> None: ...
    def write_ranges(self, data: Any) -> dict: ...
    def write_formula_ranges(self, data: Any) -> dict: ...
    def batch_update(self, requests: list[dict]) -> dict: ...
    def sheet_url(self, sheet_id: int | None = None) -> str: ...


@dataclass(frozen=True)
class ReportLayout:
    data_last_row: int
    total_row: int
    average_row: int
    values: list[tuple[str, list[list[Any]]]]
    formulas: list[tuple[str, list[list[Any]]]]
    column_widths: list[int]


@dataclass(frozen=True)
class ReportResult:
    sheet_title: str
    sheet_id: int
    url: str
    summary: ReportSummary


def sheet_title(date_from: date, date_to: date, now: datetime) -> str:
    return f"Отчёт {date_from:%d.%m.%Y}–{date_to:%d.%m.%Y} {now:%H-%M}"


def unique_title(base: str, existing: Sequence[str]) -> str:
    """Два отчёта за одну минуту получат «… (2)», а не ошибку API."""
    if base not in existing:
        return base
    number = 2
    while f"{base} ({number})" in existing:
        number += 1
    return f"{base} ({number})"


def date_to_serial(value: date) -> int:
    """Дата как число дней от 30.12.1899 — так Sheets хранит даты."""
    return (value - date(1899, 12, 30)).days


def _width(texts: Sequence[str]) -> int:
    longest = max((len(text) for text in texts), default=0)
    return max(MIN_WIDTH, min(MAX_WIDTH, longest * CHAR_PX + PADDING_PX))


def column_widths(rows: Sequence[ReportRow]) -> list[int]:
    total = sum(row.amount for row in rows)
    columns: list[list[str]] = [[header] for header in COLUMNS]
    for number, row in enumerate(rows, start=1):
        for index, text in enumerate(
            (
                str(number),
                row.date.strftime("%d.%m.%Y"),
                row.client,
                row.manager,
                row.deal,
                format_money(row.amount),
                row.status,
            )
        ):
            columns[index].append(text)
    columns[5].append(format_money(total))
    return [_width(texts) for texts in columns]


def plan_layout(params: ReportParams, rows: Sequence[ReportRow], now: datetime) -> ReportLayout:
    if not rows:
        raise ValueError("Нет строк для отчёта")
    first, last = FIRST_DATA_ROW, FIRST_DATA_ROW + len(rows) - 1
    total_row, average_row = last + 1, last + 2

    heading = f"{params.company} · {REPORT_TITLES[params.report_type]}"
    subtitle = (
        f"Период: {params.date_from:%d.%m.%Y} — {params.date_to:%d.%m.%Y}   ·   "
        f"Менеджер / отдел: {params.owner}   ·   Сформирован {now:%d.%m.%Y %H:%M}"
    )
    data = [
        [
            number,
            date_to_serial(row.date),
            row.client,
            row.manager,
            row.deal,
            row.amount,
            row.status,
        ]
        for number, row in enumerate(rows, start=1)
    ]

    status = f"G{first}:G{last}"
    amount = f"F{first}:F{last}"
    values = [
        (f"A{TITLE_ROW}", [[heading]]),
        (f"A{SUBTITLE_ROW}", [[subtitle]]),
        (
            f"A{KPI_LABEL_ROW}:{LAST_COL}{KPI_LABEL_ROW}",
            [["Выручка (оплачено)", "", "", "Количество сделок", "Средний чек", "Конверсия в оплату", ""]],
        ),
        (f"A{HEADER_ROW}:{LAST_COL}{HEADER_ROW}", [list(COLUMNS)]),
        (f"A{first}:{LAST_COL}{last}", data),
        (f"A{total_row}", [["Итого"]]),
        (f"A{average_row}", [["Среднее по сделке"]]),
    ]
    formulas = [
        (
            f"A{KPI_VALUE_ROW}:{LAST_COL}{KPI_VALUE_ROW}",
            [
                [
                    f'=SUMIF({status},"{PAID}",{amount})',
                    "",
                    "",
                    f"=COUNTA({status})",
                    f'=IFERROR(AVERAGEIF({status},"{PAID}",{amount}),0)',
                    f'=IFERROR(COUNTIF({status},"{PAID}")/COUNTA({status}),0)',
                    "",
                ]
            ],
        ),
        (f"F{total_row}", [[f"=SUM({amount})"]]),
        (f"F{average_row}", [[f"=AVERAGE({amount})"]]),
    ]
    return ReportLayout(last, total_row, average_row, values, formulas, column_widths(rows))


def build_requests(sheet_id: int, layout: ReportLayout) -> list[dict]:
    """Всё оформление листа — один список запросов для batchUpdate."""

    def grid(cell_range: str) -> dict:
        return a1_to_grid_range(sheet_id, cell_range)

    def rows_height(row: int, pixels: int) -> dict:
        return dimension_size_request(sheet_id, "ROWS", row - 1, row, pixels)

    first, last = FIRST_DATA_ROW, layout.data_last_row
    total, average = layout.total_row, layout.average_row
    end = LAST_COL

    requests = [
        sheet_properties_request(sheet_id, frozen_rows=HEADER_ROW, hide_gridlines=True),
        format_request(grid(f"A1:{end}{average}"), font_family="Arial", font_size=10, v_align="middle"),
        # Шапка документа
        merge_request(grid(f"A{TITLE_ROW}:{end}{TITLE_ROW}")),
        merge_request(grid(f"A{SUBTITLE_ROW}:{end}{SUBTITLE_ROW}")),
        format_request(
            grid(f"A{TITLE_ROW}:{end}{TITLE_ROW}"),
            bg_color=ACCENT, font_color=WHITE, font_size=16, bold=True, h_align="left",
        ),
        format_request(grid(f"A{SUBTITLE_ROW}:{end}{SUBTITLE_ROW}"), font_color=MUTED, h_align="left"),
        rows_height(TITLE_ROW, 44),
        rows_height(SUBTITLE_ROW, 26),
        # Блок итогов
        merge_request(grid(f"A{KPI_LABEL_ROW}:C{KPI_LABEL_ROW}")),
        merge_request(grid(f"A{KPI_VALUE_ROW}:C{KPI_VALUE_ROW}")),
        merge_request(grid(f"F{KPI_LABEL_ROW}:G{KPI_LABEL_ROW}")),
        merge_request(grid(f"F{KPI_VALUE_ROW}:G{KPI_VALUE_ROW}")),
        format_request(
            grid(f"A{KPI_LABEL_ROW}:{end}{KPI_LABEL_ROW}"),
            bg_color=KPI_LABEL_BG, font_color=MUTED, font_size=9, bold=True,
            h_align="center", wrap=True,
        ),
        format_request(
            grid(f"A{KPI_VALUE_ROW}:{end}{KPI_VALUE_ROW}"),
            bg_color=KPI_VALUE_BG, font_color=ACCENT, font_size=14, bold=True, h_align="center",
        ),
        format_request(grid(f"A{KPI_VALUE_ROW}"), number_format="currency"),
        format_request(grid(f"D{KPI_VALUE_ROW}"), number_format="number"),
        format_request(grid(f"E{KPI_VALUE_ROW}"), number_format="currency"),
        format_request(grid(f"F{KPI_VALUE_ROW}"), number_format="percent"),
        borders_request(grid(f"A{KPI_LABEL_ROW}:{end}{KPI_VALUE_ROW}"), color=BORDER),
        rows_height(KPI_VALUE_ROW, 38),
        # Шапка таблицы
        format_request(
            grid(f"A{HEADER_ROW}:{end}{HEADER_ROW}"),
            bg_color=HEADER_BG, font_color=WHITE, bold=True, h_align="center", wrap=True,
        ),
        rows_height(HEADER_ROW, 30),
        # Детализация
        format_request(grid(f"A{first}:A{last}"), h_align="center", font_color=MUTED),
        format_request(grid(f"B{first}:B{last}"), number_format="date", h_align="center"),
        format_request(grid(f"F{first}:F{last}"), number_format="currency", h_align="right"),
        format_request(grid(f"G{first}:G{last}"), h_align="center"),
        banding_request(grid(f"A{first}:{end}{last}"), WHITE, ZEBRA),
        *(
            text_rule_request(grid(f"G{first}:G{last}"), status, font_color=color, bold=True, index=i)
            for i, (status, color) in enumerate(STATUS_COLORS.items())
        ),
        borders_request(grid(f"A{HEADER_ROW}:{end}{average}"), color=BORDER),
        # Итоговые строки
        merge_request(grid(f"A{total}:E{total}")),
        merge_request(grid(f"A{average}:E{average}")),
        format_request(grid(f"A{total}:{end}{average}"), bg_color=TOTAL_BG, bold=True),
        format_request(grid(f"A{total}:E{average}"), h_align="right"),
        format_request(grid(f"F{total}:F{average}"), number_format="currency", h_align="right"),
        borders_request(
            grid(f"A{total}:{end}{total}"), style="SOLID_MEDIUM", color=HEADER_BG,
            sides=("top",), inner=False,
        ),
    ]
    requests += [
        dimension_size_request(sheet_id, "COLUMNS", index, index + 1, width)
        for index, width in enumerate(layout.column_widths)
    ]
    return requests


def build_report(
    sheets: SheetsClient,
    params: ReportParams,
    rows: Sequence[ReportRow],
    *,
    now: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> ReportResult:
    now = now or datetime.now()
    notify = progress or (lambda percent, message: None)
    layout = plan_layout(params, rows, now)

    notify(15, "Проверяю листы таблицы…")
    title = unique_title(
        sheet_title(params.date_from, params.date_to, now), sheets.get_sheet_names()
    )

    notify(30, f"Создаю лист «{title}»…")
    sheet_id = sheets.add_sheet(title, rows=layout.average_row + 2, cols=len(COLUMNS))
    try:
        notify(50, "Записываю данные…")
        sheets.write_ranges([(a1(title, rng), values) for rng, values in layout.values])
        notify(65, "Добавляю формулы итогов…")
        sheets.write_formula_ranges([(a1(title, rng), values) for rng, values in layout.formulas])
        notify(80, "Оформляю лист…")
        sheets.batch_update(build_requests(sheet_id, layout))
    except Exception:
        # Не оставляем в таблице полупустой лист.
        with contextlib.suppress(Exception):
            sheets.delete_sheet_by_id(sheet_id)
        raise

    notify(100, "Готово")
    return ReportResult(title, sheet_id, sheets.sheet_url(sheet_id), summarize(rows))


def generate_report(
    sheets: SheetsClient,
    params: ReportParams,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> ReportResult:
    """Сгенерировать данные и сразу собрать из них лист."""
    if progress:
        progress(5, "Генерирую данные…")
    rows = generate_rows(params, rng)
    return build_report(sheets, params, rows, now=now, progress=progress)
