"""«Генератор отчётов» — окно на tkinter.

Генерация и запись в Google Таблицу идут в отдельном потоке, поэтому окно
не зависает. Поток не трогает виджеты: о прогрессе он сообщает через
очередь, а окно забирает сообщения раз в 100 мс.

Запуск:
    python report_generator.py
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
from datetime import date
from tkinter import messagebox, ttk

from config import DEFAULT_COMPANY, ConfigError, load_settings
from data_generator import (
    ALL_OWNERS,
    DATE_FORMAT,
    MAX_ROWS,
    MIN_ROWS,
    REPORT_TYPES,
    ReportParams,
    ValidationError,
    format_money,
    owner_choices,
    validate_period,
    validate_rows_count,
)
from google_sheets import GoogleSheets, GoogleSheetsError
from report_builder import ReportResult, generate_report

POLL_MS = 100
ERROR_COLOR = "#B3261E"
OK_COLOR = "#2E7D32"
LINK_COLOR = "#1A56DB"


class ReportApp(ttk.Frame):
    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=16)
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.report_url: str | None = None

        config_error = None
        try:
            company = load_settings().company_name
        except ConfigError as exc:
            config_error = str(exc)
            company = os.getenv("REPORT_COMPANY_NAME", "").strip() or DEFAULT_COMPANY

        today = date.today()
        self.var_from = tk.StringVar(value=today.replace(day=1).strftime(DATE_FORMAT))
        self.var_to = tk.StringVar(value=today.strftime(DATE_FORMAT))
        self.var_type = tk.StringVar(value=REPORT_TYPES[0])
        self.var_owner = tk.StringVar(value=ALL_OWNERS)
        self.var_rows = tk.StringVar(value="30")
        self.var_company = tk.StringVar(value=company)
        self.var_status = tk.StringVar(value="Заполните поля и нажмите «Сгенерировать отчёт».")

        self._build()
        if config_error:
            self._show_status(config_error, ERROR_COLOR)
            self.root.after(200, lambda: messagebox.showwarning("Проверьте настройки", config_error, parent=self.root))

    # --- интерфейс ---

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="Генератор отчётов", font=("TkDefaultFont", 15, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        fields = [
            ("Дата с", ttk.Entry(self, textvariable=self.var_from, width=14)),
            ("Дата по", ttk.Entry(self, textvariable=self.var_to, width=14)),
            ("Тип отчёта", ttk.Combobox(self, textvariable=self.var_type, values=REPORT_TYPES, state="readonly", width=34)),
            ("Менеджер или отдел", ttk.Combobox(self, textvariable=self.var_owner, values=owner_choices(), state="readonly", width=34)),
            ("Количество строк", ttk.Spinbox(self, textvariable=self.var_rows, from_=MIN_ROWS, to=MAX_ROWS, width=8)),
            ("Название компании", ttk.Entry(self, textvariable=self.var_company, width=36)),
        ]
        for row, (label, widget) in enumerate(fields, start=1):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            widget.grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(self, text="Даты — в формате ДД.ММ.ГГГГ", foreground="#5F6B7A").grid(
            row=len(fields) + 1, column=1, sticky="w"
        )

        base = len(fields) + 2
        self.button = ttk.Button(self, text="Сгенерировать отчёт", command=self.on_generate)
        self.button.grid(row=base, column=0, columnspan=2, sticky="ew", pady=(14, 8))

        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.grid(row=base + 1, column=0, columnspan=2, sticky="ew")

        self.status = tk.Label(self, textvariable=self.var_status, anchor="w", justify="left", wraplength=440)
        self.status.grid(row=base + 2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.link = tk.Label(self, text="", fg=LINK_COLOR, cursor="hand2", anchor="w", font=("TkDefaultFont", 11, "underline"))
        self.link.bind("<Button-1>", lambda _event: self._open_link())
        self.copy_button = ttk.Button(self, text="Скопировать ссылку", command=self._copy_link)

    def _show_status(self, text: str, color: str | None = None) -> None:
        self.var_status.set(text)
        self.status.configure(fg=color or "black")

    def _set_busy(self, busy: bool) -> None:
        self.button.configure(state="disabled" if busy else "normal")
        self.root.configure(cursor="watch" if busy else "")

    def _show_link(self, url: str) -> None:
        self.report_url = url
        self.link.configure(text="Открыть отчёт в Google Таблице")
        self.link.grid(row=99, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.copy_button.grid(row=100, column=0, sticky="w", pady=(6, 0))

    def _hide_link(self) -> None:
        self.report_url = None
        self.link.grid_remove()
        self.copy_button.grid_remove()

    def _open_link(self) -> None:
        if self.report_url:
            webbrowser.open(self.report_url)

    def _copy_link(self) -> None:
        if self.report_url:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.report_url)
            self._show_status("Ссылка скопирована в буфер обмена.", OK_COLOR)

    # --- генерация ---

    def on_generate(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            start, end = validate_period(self.var_from.get(), self.var_to.get())
            params = ReportParams(
                date_from=start,
                date_to=end,
                report_type=self.var_type.get(),
                owner=self.var_owner.get(),
                rows_count=validate_rows_count(self.var_rows.get()),
                company=self.var_company.get().strip(),
            )
        except ValidationError as exc:
            messagebox.showerror("Проверьте поля", str(exc), parent=self.root)
            return

        self._hide_link()
        self._set_busy(True)
        self.progress.configure(value=0)
        self._show_status("Подключаюсь к Google Sheets…")
        self.worker = threading.Thread(target=self._run, args=(params,), daemon=True)
        self.worker.start()
        self.root.after(POLL_MS, self._poll)

    def _run(self, params: ReportParams) -> None:
        """Фоновый поток: только очередь, никаких вызовов tkinter."""
        try:
            sheets = GoogleSheets()
            result = generate_report(
                sheets, params, progress=lambda percent, text: self.events.put(("progress", percent, text))
            )
            self.events.put(("done", result))
        except (ConfigError, GoogleSheetsError, ValidationError) as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 — окно должно сообщить о любой ошибке
            self.events.put(("error", f"Непредвиденная ошибка: {exc!r}"))

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _, percent, text = event
                    self.progress.configure(value=percent)
                    self._show_status(text)
                elif event[0] == "done":
                    self._finish_ok(event[1])
                elif event[0] == "error":
                    self._finish_error(event[1])
        except queue.Empty:
            pass
        if (self.worker and self.worker.is_alive()) or not self.events.empty():
            self.root.after(POLL_MS, self._poll)

    def _finish_ok(self, result: ReportResult) -> None:
        summary = result.summary
        self._set_busy(False)
        self.progress.configure(value=100)
        self._show_status(
            f"Готово: лист «{result.sheet_title}». Строк: {summary.deals_count}, "
            f"выручка {format_money(summary.revenue)}, конверсия {summary.conversion:.0%}.",
            OK_COLOR,
        )
        self._show_link(result.url)

    def _finish_error(self, message: str) -> None:
        self._set_busy(False)
        self.progress.configure(value=0)
        self._show_status(message, ERROR_COLOR)
        messagebox.showerror("Не удалось сформировать отчёт", message, parent=self.root)


def main() -> None:
    root = tk.Tk()
    root.title("Генератор отчётов")
    root.resizable(False, False)
    ReportApp(root).pack(fill="both", expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
