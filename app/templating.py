from datetime import date, datetime
from decimal import Decimal
import json

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models.enums import OrderStatus
from app.services.client_service import format_russian_phone
from app.services.courier_cash_handover_service import (
    handover_status_class,
    handover_status_label,
)


templates = Jinja2Templates(directory="app/templates")

# Доступно во всех шаблонах для режима публичной витрины.
templates.env.globals["demo_mode"] = bool(settings.demo_mode)


def status_label(status: OrderStatus | str | None) -> str:
    value = status.value if isinstance(status, OrderStatus) else status
    return {
        OrderStatus.IN_WORK.value: "В работе",
        OrderStatus.AT_COURIER.value: "У курьера",
        OrderStatus.DELIVERED.value: "Доставлено",
    }.get(value or "", "Не указан")


def status_class(status: OrderStatus | str | None) -> str:
    value = status.value if isinstance(status, OrderStatus) else status
    return {
        OrderStatus.IN_WORK.value: "status-work",
        OrderStatus.AT_COURIER.value: "status-courier",
        OrderStatus.DELIVERED.value: "status-done",
    }.get(value or "", "status-work")


def money(value: Decimal | int | str | None) -> str:
    amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    formatted = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    formatted = formatted.removesuffix(",00")
    return f"{formatted} ₽"


def phone(value: str | None, empty: str = "Не указан") -> str:
    if value is None or value == "":
        return empty
    try:
        return format_russian_phone(value)
    except ValueError:
        return str(value).replace("+ ", "+")


def order_number(value: Decimal | int | str | None, empty: str = "Не указан") -> str:
    if value is None or value == "":
        return empty
    amount = Decimal(str(value)).normalize()
    return format(amount, "f").replace(".", ",")


def ru_date(value: date | datetime | None) -> str:
    if not value:
        return "Не указана"
    return value.strftime("%d.%m.%Y")


def ru_datetime(value: datetime | None) -> str:
    if not value:
        return "Не указана"
    return value.strftime("%d.%m.%Y %H:%M")


def log_summary(log) -> str:
    if log.action == "оплата":
        data = _json_dict(log.new_value)
        amount = data.get("amount")
        method = data.get("method_label") or data.get("method")
        author = data.get("created_by")
        parts = ["оплата"]
        if amount:
            parts.append(money(amount))
        if method:
            parts.append(str(method))
        if author:
            parts.append(str(author))
        return " · ".join(parts)

    if log.action == "удаление оплаты":
        data = _json_dict(log.old_value)
        cancel_data = _json_dict(log.new_value)
        amount = data.get("amount")
        method = data.get("method_label") or data.get("method")
        author = cancel_data.get("cancelled_by")
        parts = ["отмена оплаты"]
        if amount:
            parts.append(money(amount))
        if method:
            parts.append(str(method))
        if author:
            parts.append(str(author))
        return " · ".join(parts)

    if log.action == "изменение оплаты":
        old_data = _json_dict(log.old_value)
        new_data = _json_dict(log.new_value)
        old_amount = old_data.get("amount")
        new_amount = new_data.get("amount")
        old_method = old_data.get("method_label") or old_data.get("method")
        new_method = new_data.get("method_label") or new_data.get("method")
        author = new_data.get("changed_by")
        before = " ".join(
            str(item)
            for item in (money(old_amount) if old_amount else "", old_method or "")
            if item
        )
        after = " ".join(
            str(item)
            for item in (money(new_amount) if new_amount else "", new_method or "")
            if item
        )
        parts = ["изменение оплаты"]
        if before or after:
            parts.append(f"{before} -> {after}".strip())
        if author:
            parts.append(str(author))
        return " · ".join(parts)

    return str(log.action or "")


def _json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


templates.env.filters["status_label"] = status_label
templates.env.filters["status_class"] = status_class
templates.env.filters["money"] = money
templates.env.filters["phone"] = phone
templates.env.filters["order_number"] = order_number
templates.env.filters["ru_date"] = ru_date
templates.env.filters["ru_datetime"] = ru_datetime
templates.env.filters["log_summary"] = log_summary
templates.env.filters["handover_status_label"] = handover_status_label
templates.env.filters["handover_status_class"] = handover_status_class
