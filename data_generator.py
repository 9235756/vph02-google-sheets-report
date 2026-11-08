"""Случайные, но правдоподобные отчётные данные. Работает без tkinter и сети.

Все имена менеджеров и названия компаний вымышлены — реальных
персональных данных здесь нет.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

DATE_FORMAT = "%d.%m.%Y"
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

REPORT_TYPES = ("Продажи", "Клиенты", "Сделки")
REPORT_TITLES = {
    "Продажи": "Отчёт по продажам",
    "Клиенты": "Отчёт по клиентам",
    "Сделки": "Отчёт по сделкам",
}

PAID = "Оплачено"
IN_PROGRESS = "В работе"
CANCELLED = "Отменено"
STATUSES = (PAID, IN_PROGRESS, CANCELLED)

MIN_ROWS, MAX_ROWS = 1, 1000

ALL_OWNERS = "Все сотрудники"
DEPARTMENTS: dict[str, tuple[str, ...]] = {
    "Отдел корпоративных продаж": ("Орлова А. В.", "Соколов Д. И.", "Мельникова Е. П."),
    "Отдел розничных продаж": ("Ковалёв С. А.", "Захарова Н. Г.", "Тихонов П. Р."),
    "Отдел развития": ("Гусева Л. М.", "Лебедев Р. О."),
}
MANAGERS = tuple(name for names in DEPARTMENTS.values() for name in names)

CLIENTS = (
    "ООО «Северная гавань»", "АО «Кедровый край»", "ООО «Меридиан-Строй»",
    "ООО «Ладья Логистик»", "АО «Сфера Технологий»", "ООО «Прима Сервис»",
    "ООО «Горизонт Плюс»", "АО «Волжский терминал»", "ООО «Оникс Групп»",
    "ООО «Тёплый дом»", "ООО «Вектор Трейд»", "АО «Уральские мастерские»",
    "ООО «Сибирский хлеб»", "ООО «Белый парус»", "ООО «Агроресурс»",
    "АО «Новая энергия»", "ООО «Студия Пиксель»", "ООО «Медтехника Про»",
    "ООО «Городские сады»", "АО «Балтийский склад»",
)

DEALS: dict[str, tuple[str, ...]] = {
    "Продажи": (
        "Поставка офисной техники", "Лицензии CRM на 12 месяцев",
        "Комплект серверного оборудования", "Расходные материалы, квартал",
        "Подписка на облачное хранилище", "Монтаж видеонаблюдения",
        "Сервисный контракт на год",
    ),
    "Клиенты": (
        "Первичная консультация", "Повторный заказ", "Расширение тарифа",
        "Пилотный проект", "Продление договора", "Обучение сотрудников клиента",
    ),
    "Сделки": (
        "Внедрение учётной системы", "Автоматизация склада",
        "Корпоративный портал под ключ", "Интеграция с маркетплейсами",
        "Аудит ИТ-инфраструктуры", "Миграция в облако",
    ),
}

AMOUNT_RANGES = {
    "Продажи": (15_000, 450_000),
    "Клиенты": (5_000, 150_000),
    "Сделки": (80_000, 2_500_000),
}
STATUS_WEIGHTS = {
    "Продажи": (65, 25, 10),
    "Клиенты": (50, 35, 15),
    "Сделки": (40, 40, 20),
}


class ValidationError(ValueError):
    """Неверно заполнено поле: сообщение показывается пользователю как есть."""


# --- Валидация полей формы ---------------------------------------------------


def parse_date(text: str, field: str = "Дата") -> date:
    text = text.strip()
    if not _DATE_RE.match(text):
        raise ValidationError(f"{field}: введите дату в формате ДД.ММ.ГГГГ, например 01.03.2026")
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        raise ValidationError(f"{field}: такой даты не существует — {text}") from None


def validate_period(date_from: str, date_to: str) -> tuple[date, date]:
    start = parse_date(date_from, "Дата «с»")
    end = parse_date(date_to, "Дата «по»")
    if start > end:
        raise ValidationError("Дата «с» должна быть не позже даты «по»")
    return start, end


def validate_rows_count(value: str | int) -> int:
    try:
        count = int(str(value).strip())
    except ValueError:
        raise ValidationError("Количество строк должно быть целым числом") from None
    if not MIN_ROWS <= count <= MAX_ROWS:
        raise ValidationError(f"Количество строк — от {MIN_ROWS} до {MAX_ROWS}")
    return count


def owner_choices() -> list[str]:
    """Значения выпадающего списка «Менеджер или отдел»."""
    return [ALL_OWNERS, *DEPARTMENTS, *MANAGERS]


def managers_for(owner: str) -> tuple[str, ...]:
    if owner == ALL_OWNERS:
        return MANAGERS
    if owner in DEPARTMENTS:
        return DEPARTMENTS[owner]
    if owner in MANAGERS:
        return (owner,)
    raise ValidationError(f"Неизвестный менеджер или отдел: {owner}")


# --- Модель данных -----------------------------------------------------------


@dataclass(frozen=True)
class ReportParams:
    date_from: date
    date_to: date
    report_type: str
    owner: str
    rows_count: int
    company: str

    def __post_init__(self) -> None:
        if self.date_from > self.date_to:
            raise ValidationError("Дата «с» должна быть не позже даты «по»")
        if self.report_type not in REPORT_TYPES:
            raise ValidationError(f"Неизвестный тип отчёта: {self.report_type}")
        managers_for(self.owner)
        if not MIN_ROWS <= self.rows_count <= MAX_ROWS:
            raise ValidationError(f"Количество строк — от {MIN_ROWS} до {MAX_ROWS}")
        if not self.company.strip():
            raise ValidationError("Укажите название компании")


@dataclass(frozen=True)
class ReportRow:
    date: date
    client: str
    manager: str
    deal: str
    amount: float
    status: str


@dataclass(frozen=True)
class ReportSummary:
    revenue: float
    deals_count: int
    paid_count: int
    average_check: float
    conversion: float


def generate_rows(params: ReportParams, rng: random.Random | None = None) -> list[ReportRow]:
    """Строки детализации, отсортированные по дате."""
    rng = rng or random.Random()
    managers = managers_for(params.owner)
    # Ограниченный пул клиентов, чтобы одни и те же компании повторялись.
    pool_size = min(len(CLIENTS), max(3, params.rows_count // 2))
    clients = rng.sample(CLIENTS, k=pool_size)
    days = (params.date_to - params.date_from).days
    low, high = AMOUNT_RANGES[params.report_type]

    rows = [
        ReportRow(
            date=params.date_from + timedelta(days=rng.randint(0, days)),
            client=rng.choice(clients),
            manager=rng.choice(managers),
            deal=rng.choice(DEALS[params.report_type]),
            amount=float(round(rng.uniform(low, high) / 100) * 100),
            status=rng.choices(STATUSES, weights=STATUS_WEIGHTS[params.report_type])[0],
        )
        for _ in range(params.rows_count)
    ]
    rows.sort(key=lambda row: (row.date, row.client))
    return rows


def summarize(rows: Sequence[ReportRow]) -> ReportSummary:
    paid = [row for row in rows if row.status == PAID]
    revenue = round(sum(row.amount for row in paid), 2)
    return ReportSummary(
        revenue=revenue,
        deals_count=len(rows),
        paid_count=len(paid),
        average_check=round(revenue / len(paid), 2) if paid else 0.0,
        conversion=len(paid) / len(rows) if rows else 0.0,
    )


def format_money(value: float) -> str:
    """12345.5 → «12 345,50 ₽» (для окна приложения, не для таблицы)."""
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
