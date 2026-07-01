from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.enums import PaymentDisplayStatus, PaymentMethod, PaymentStatus, UserRole
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.client_service import normalize_phone


MONEY_ZERO = Decimal("0.00")
PAYMENT_AMOUNT_NUMBER_MESSAGE = "Сумма оплаты должна быть числом."
PAYMENT_AMOUNT_NON_NEGATIVE_MESSAGE = "Сумма оплаты не может быть отрицательной."
PAYMENT_METHOD_MESSAGE = "Выберите способ оплаты."
PAYMENT_ALREADY_PAID_MESSAGE = "Заявка уже оплачена."
PAYMENT_EXCEEDS_REMAINING_MESSAGE = "Сумма оплаты больше остатка по заявке."
PAYMENT_AMOUNT_REQUIRED_MESSAGE = "Укажите сумму оплаты."


@dataclass(frozen=True)
class PaymentRow:
    order: Order
    payment: Payment | None
    payment_status: PaymentDisplayStatus
    payment_amount: Decimal
    paid_amount: Decimal
    remaining_amount: Decimal


@dataclass(frozen=True)
class PaymentList:
    rows: list[PaymentRow]
    shown_count: int
    total_count: int
    pending_count: int
    partial_count: int
    paid_count: int
    paid_total: Decimal
    delivery_cost_total: Decimal
    courier_pay_total: Decimal


def list_payment_rows(
    db: Session,
    *,
    search: str | None = None,
    payment_status: str | None = None,
    delivery_date: date | None = None,
    detail: str | None = None,
    unpaid_all_time: bool = False,
) -> PaymentList:
    order_by = (
        (Order.delivery_date.desc().nullslast(), Order.created_at.desc(), Order.id.desc())
        if unpaid_all_time
        else (Order.delivery_date.asc().nullslast(), Order.created_at.desc(), Order.id.desc())
    )
    query = (
        select(Order)
        .options(
            selectinload(Order.client),
            selectinload(Order.courier),
            selectinload(Order.payments),
        )
        .where(Order.is_archived.is_(False))
        .order_by(*order_by)
    )

    if delivery_date and not unpaid_all_time:
        query = query.where(Order.delivery_date == delivery_date)

    orders = list(db.scalars(query).all())
    cleaned_search = _clean_text(search)
    if cleaned_search and not unpaid_all_time:
        orders = [order for order in orders if _order_matches_search(order, cleaned_search)]

    rows = [_row_for_order(order) for order in orders]

    if unpaid_all_time:
        rows = [row for row in rows if row.payment_status == PaymentDisplayStatus.PENDING]
    elif payment_status in {status.value for status in PaymentDisplayStatus}:
        rows = [row for row in rows if row.payment_status.value == payment_status]

    summary_rows = rows
    if detail == "unpaid":
        rows = [
            row
            for row in rows
            if row.payment_status in (PaymentDisplayStatus.PENDING, PaymentDisplayStatus.PARTIAL)
        ]
    detail_status = _detail_payment_status(detail)
    if detail_status is not None:
        rows = [row for row in rows if row.payment_status == detail_status]
    if payment_status == PaymentDisplayStatus.PAID.value or detail_status == PaymentDisplayStatus.PAID:
        rows = sorted(rows, key=_paid_row_sort_key, reverse=True)

    return _payment_list(rows=rows, summary_rows=summary_rows)


def empty_payment_list() -> PaymentList:
    return _payment_list(rows=[], summary_rows=[])


def _payment_list(*, rows: list[PaymentRow], summary_rows: list[PaymentRow]) -> PaymentList:
    return PaymentList(
        rows=rows,
        shown_count=len(rows),
        total_count=len(summary_rows),
        pending_count=sum(1 for row in summary_rows if row.payment_status == PaymentDisplayStatus.PENDING),
        partial_count=sum(1 for row in summary_rows if row.payment_status == PaymentDisplayStatus.PARTIAL),
        paid_count=sum(1 for row in summary_rows if row.payment_status == PaymentDisplayStatus.PAID),
        paid_total=sum(
            (row.paid_amount for row in summary_rows if row.payment_status == PaymentDisplayStatus.PAID),
            MONEY_ZERO,
        ),
        delivery_cost_total=sum(
            (_money(row.order.delivery_cost or MONEY_ZERO) for row in summary_rows),
            MONEY_ZERO,
        ),
        courier_pay_total=sum(
            (_money(row.order.courier_pay or MONEY_ZERO) for row in summary_rows),
            MONEY_ZERO,
        ),
    )


def create_or_update_payment(
    db: Session,
    order_id: int,
    form_data: dict[str, Any],
    user: User,
) -> Payment:
    if user.role not in (UserRole.ADMIN, UserRole.MANAGER):
        raise PermissionError("Недостаточно прав")

    order = db.scalar(
        select(Order)
        .options(selectinload(Order.payments))
        .where(Order.id == order_id, Order.is_archived.is_(False))
    )
    if order is None:
        raise ValueError("Заявка не найдена")

    remaining_amount = _remaining_amount_for_order(order)
    if remaining_amount == MONEY_ZERO and _paid_amount_for_order(order) > MONEY_ZERO:
        raise ValueError(PAYMENT_ALREADY_PAID_MESSAGE)

    amount = _payment_amount(form_data.get("amount"), default=remaining_amount)
    if amount <= MONEY_ZERO:
        raise ValueError(PAYMENT_AMOUNT_REQUIRED_MESSAGE)
    if amount > remaining_amount:
        raise ValueError(PAYMENT_EXCEEDS_REMAINING_MESSAGE)
    method = _payment_method(form_data.get("method"))

    payment = Payment(
        order_id=order.id,
        amount=amount,
        method=method,
        status=PaymentStatus.PAID,
        paid_at=datetime.now(timezone.utc),
        created_by_id=user.id,
    )
    db.add(payment)

    db.flush()
    _write_payment_log(db, order, payment, user)
    db.commit()
    db.refresh(payment)
    return payment


def create_quick_payments(
    db: Session,
    order_id: int,
    form_data: dict[str, Any],
    user: User,
) -> list[Payment]:
    if user.role not in (UserRole.ADMIN, UserRole.MANAGER):
        raise PermissionError("Недостаточно прав")

    order = db.scalar(
        select(Order)
        .options(selectinload(Order.payments))
        .where(Order.id == order_id, Order.is_archived.is_(False))
    )
    if order is None:
        raise ValueError("Заявка не найдена")

    remaining_amount = _remaining_amount_for_order(order)
    if remaining_amount == MONEY_ZERO and _paid_amount_for_order(order) > MONEY_ZERO:
        raise ValueError(PAYMENT_ALREADY_PAID_MESSAGE)

    cash_amount = _optional_payment_amount(form_data.get("cash_amount"))
    card_amount = _optional_payment_amount(form_data.get("card_amount"))
    total_amount = cash_amount + card_amount
    if total_amount <= MONEY_ZERO:
        raise ValueError(PAYMENT_AMOUNT_REQUIRED_MESSAGE)
    if total_amount > remaining_amount:
        raise ValueError(PAYMENT_EXCEEDS_REMAINING_MESSAGE)

    payments: list[Payment] = []
    for amount, method in (
        (cash_amount, PaymentMethod.CASH),
        (card_amount, PaymentMethod.TRANSFER),
    ):
        if amount <= MONEY_ZERO:
            continue
        payment = Payment(
            order_id=order.id,
            amount=amount,
            method=method,
            status=PaymentStatus.PAID,
            paid_at=datetime.now(timezone.utc),
            created_by_id=user.id,
        )
        db.add(payment)
        db.flush()
        _write_payment_log(db, order, payment, user)
        payments.append(payment)

    db.commit()
    for payment in payments:
        db.refresh(payment)
    return payments


def delete_payment(db: Session, payment_id: int, user: User) -> None:
    if user.role != UserRole.ADMIN:
        raise PermissionError("Недостаточно прав")

    payment = db.scalar(
        select(Payment)
        .options(selectinload(Payment.order), selectinload(Payment.created_by))
        .where(Payment.id == payment_id)
    )
    if payment is None or payment.order.is_archived:
        raise ValueError("Оплата не найдена")

    order = payment.order
    old_snapshot = _payment_snapshot(payment)
    db.delete(payment)
    db.flush()
    _write_payment_delete_log(db, order, user, old_snapshot)
    db.commit()
    db.expire(order, ["payments"])


def update_payment(
    db: Session,
    payment_id: int,
    form_data: dict[str, Any],
    user: User,
    *,
    order_id: int | None = None,
) -> Payment:
    if user.role != UserRole.ADMIN:
        raise PermissionError("Недостаточно прав")

    payment = db.scalar(
        select(Payment)
        .options(
            selectinload(Payment.order).selectinload(Order.payments),
            selectinload(Payment.created_by),
        )
        .where(Payment.id == payment_id)
    )
    if payment is None or payment.order.is_archived:
        raise ValueError("Оплата не найдена")
    if order_id is not None and payment.order_id != order_id:
        raise ValueError("Оплата не относится к этой заявке.")

    order = payment.order
    amount = _payment_amount(form_data.get("amount"), default=MONEY_ZERO)
    if amount <= MONEY_ZERO:
        raise ValueError(PAYMENT_AMOUNT_REQUIRED_MESSAGE)
    method = _payment_method(form_data.get("method"))

    other_paid_total = sum(
        (
            _money(item.amount)
            for item in order.payments
            if item.id != payment.id and item.status == PaymentStatus.PAID
        ),
        MONEY_ZERO,
    )
    if other_paid_total + amount > _money(order.delivery_cost):
        raise ValueError(PAYMENT_EXCEEDS_REMAINING_MESSAGE)

    old_snapshot = _payment_snapshot(payment)
    payment.amount = amount
    payment.method = method
    payment.status = PaymentStatus.PAID

    db.flush()
    _write_payment_update_log(db, order, payment, user, old_snapshot)
    db.commit()
    db.refresh(payment)
    return payment


def payment_status_for_order(order: Order) -> PaymentDisplayStatus:
    return _display_status(_paid_amount_for_order(order), _remaining_amount_for_order(order))


def payment_method_label(method: PaymentMethod | str | None) -> str:
    value = method.value if isinstance(method, PaymentMethod) else method
    return {
        PaymentMethod.CASH.value: "Наличными",
        PaymentMethod.TRANSFER.value: "Карта",
    }.get(value or "", "Не указан")


def payment_status_label(status: PaymentDisplayStatus | PaymentStatus | str | None) -> str:
    value = status.value if isinstance(status, (PaymentDisplayStatus, PaymentStatus)) else status
    return {
        PaymentDisplayStatus.PENDING.value: "Не оплачено",
        PaymentDisplayStatus.PARTIAL.value: "Частично",
        PaymentDisplayStatus.PAID.value: "Оплачено",
    }.get(value or "", "Не оплачено")


def payment_status_class(status: PaymentDisplayStatus | PaymentStatus | str | None) -> str:
    value = status.value if isinstance(status, (PaymentDisplayStatus, PaymentStatus)) else status
    return {
        PaymentDisplayStatus.PENDING.value: "status-wait",
        PaymentDisplayStatus.PARTIAL.value: "status-partial",
        PaymentDisplayStatus.PAID.value: "status-paid",
    }.get(value or "", "status-wait")


def _detail_payment_status(value: str | None) -> PaymentDisplayStatus | None:
    if value in ("", None, "all"):
        return None
    try:
        return PaymentDisplayStatus(str(value))
    except ValueError:
        return None


def _row_for_order(order: Order) -> PaymentRow:
    payment = _payment_for_order(order)
    paid_amount = _paid_amount_for_order(order)
    remaining_amount = _remaining_amount_for_order(order)
    payment_status = _display_status(paid_amount, remaining_amount)
    return PaymentRow(
        order=order,
        payment=payment,
        payment_status=payment_status,
        payment_amount=paid_amount if payment_status == PaymentDisplayStatus.PAID else remaining_amount,
        paid_amount=paid_amount,
        remaining_amount=remaining_amount,
    )


def _payment_for_order(order: Order) -> Payment | None:
    if not order.payments:
        return None
    return sorted(order.payments, key=lambda payment: payment.id or 0, reverse=True)[0]


def _paid_row_sort_key(row: PaymentRow) -> tuple[float, date, float, int]:
    latest_paid_at = max(
        (
            _datetime_sort_value(payment.paid_at)
            for payment in row.order.payments
            if payment.status == PaymentStatus.PAID
        ),
        default=0.0,
    )
    return (
        latest_paid_at,
        row.order.delivery_date or date.min,
        _datetime_sort_value(row.order.created_at),
        row.order.id or 0,
    )


def _datetime_sort_value(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _paid_amount_for_order(order: Order) -> Decimal:
    return sum(
        (
            _money(payment.amount)
            for payment in order.payments
            if payment.status == PaymentStatus.PAID
        ),
        MONEY_ZERO,
    )


def _remaining_amount_for_order(order: Order) -> Decimal:
    return max(_money(order.delivery_cost) - _paid_amount_for_order(order), MONEY_ZERO)


def _display_status(paid_amount: Decimal, remaining_amount: Decimal) -> PaymentDisplayStatus:
    if paid_amount <= MONEY_ZERO:
        return PaymentDisplayStatus.PENDING
    if remaining_amount > MONEY_ZERO:
        return PaymentDisplayStatus.PARTIAL
    return PaymentDisplayStatus.PAID


def _payment_method(value: Any) -> PaymentMethod:
    try:
        return PaymentMethod(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(PAYMENT_METHOD_MESSAGE) from exc


def _payment_amount(value: Any, *, default: Decimal) -> Decimal:
    raw_value = str(value or "").replace(",", ".").strip()
    if not raw_value:
        return _money(default)
    return _money(raw_value)


def _optional_payment_amount(value: Any) -> Decimal:
    raw_value = str(value or "").replace(",", ".").strip()
    if not raw_value:
        return MONEY_ZERO
    return _money(raw_value)


def _money(value: Any) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise InvalidOperation
        if decimal_value < MONEY_ZERO:
            raise ValueError(PAYMENT_AMOUNT_NON_NEGATIVE_MESSAGE)
        return decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(PAYMENT_AMOUNT_NUMBER_MESSAGE) from exc


def _write_payment_log(
    db: Session,
    order: Order,
    payment: Payment,
    user: User,
) -> None:
    db.add(
        OrderChangeLog(
            order_id=order.id,
            user_id=user.id,
            action="оплата",
            old_value=None,
            new_value=_to_json(_payment_snapshot(payment, user=user)),
        )
    )


def _write_payment_delete_log(
    db: Session,
    order: Order,
    user: User,
    old_snapshot: dict[str, Any] | None,
) -> None:
    db.add(
        OrderChangeLog(
            order_id=order.id,
            user_id=user.id,
            action="удаление оплаты",
            old_value=_to_json(old_snapshot) if old_snapshot else None,
            new_value=_to_json(
                {
                    "cancelled_by": user.full_name,
                    "cancelled_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
    )


def _write_payment_update_log(
    db: Session,
    order: Order,
    payment: Payment,
    user: User,
    old_snapshot: dict[str, Any] | None,
) -> None:
    new_snapshot = _payment_snapshot(payment)
    if new_snapshot is not None:
        new_snapshot = {
            **new_snapshot,
            "changed_by": user.full_name,
            "changed_at": datetime.now(timezone.utc).isoformat(),
        }
    db.add(
        OrderChangeLog(
            order_id=order.id,
            user_id=user.id,
            action="изменение оплаты",
            old_value=_to_json(old_snapshot) if old_snapshot else None,
            new_value=_to_json(new_snapshot) if new_snapshot else None,
        )
    )


def _payment_snapshot(payment: Payment | None, *, user: User | None = None) -> dict[str, Any] | None:
    if payment is None:
        return None
    author = user or payment.created_by
    return {
        "amount": str(payment.amount),
        "method": payment.method.value if isinstance(payment.method, PaymentMethod) else payment.method,
        "method_label": payment_method_label(payment.method),
        "status": payment.status.value if isinstance(payment.status, PaymentStatus) else payment.status,
        "created_by": author.full_name if author else None,
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
    }


def _to_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _order_matches_search(order: Order, search: str) -> bool:
    search_text = search.casefold()
    text_values = (
        order.order_code,
        order.client_name_snapshot,
        order.client_phone_snapshot,
    )
    if any(search_text in str(value or "").casefold() for value in text_values):
        return True

    phone_digits = normalize_phone(search)
    if not phone_digits:
        return False

    order_phone = normalize_phone(order.client_phone_snapshot)
    return any(term in order_phone for term in _phone_search_terms(phone_digits))


def _phone_search_terms(phone_digits: str) -> tuple[str, ...]:
    terms = [phone_digits]
    if len(phone_digits) == 10:
        terms.append(f"7{phone_digits}")
    elif len(phone_digits) == 11 and phone_digits.startswith("8"):
        terms.append(f"7{phone_digits[1:]}")
    return tuple(dict.fromkeys(terms))
