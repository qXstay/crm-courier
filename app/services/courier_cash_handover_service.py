from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.courier_cash_handover import CourierCashHandover
from app.models.enums import (
    CourierCashHandoverStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
)
from app.models.order import Order
from app.models.user import User


MONEY_ZERO = Decimal("0.00")
HANDOVER_AMOUNT_NUMBER_MESSAGE = "Введите корректную сумму"
HANDOVER_AMOUNT_POSITIVE_MESSAGE = "Сумма должна быть больше нуля"
HANDOVER_ALREADY_PROCESSED_MESSAGE = "Эта сдача уже обработана"
HANDOVER_ACCESS_MESSAGE = "Нет доступа к этому действию"
HANDOVER_SAVE_MESSAGE = "Не удалось сохранить. Проверьте данные"


@dataclass(frozen=True)
class CourierPeriodMoney:
    total_delivery: Decimal
    paid_total: Decimal
    cash_total: Decimal
    courier_pay_total: Decimal
    paid_courier_pay_total: Decimal
    confirmed_total: Decimal
    pending_total: Decimal
    due_to_office: Decimal


@dataclass(frozen=True)
class CourierHandoverList:
    rows: list[CourierCashHandover]
    pending_count: int
    pending_total: Decimal
    confirmed_total: Decimal
    rejected_total: Decimal


def calculate_period_money(
    db: Session,
    *,
    courier_id: int,
    period_start: date,
    period_end: date,
) -> CourierPeriodMoney:
    orders = _list_period_orders(
        db,
        courier_id=courier_id,
        period_start=period_start,
        period_end=period_end,
    )
    confirmed_total = _handover_total(
        db,
        courier_id=courier_id,
        period_start=period_start,
        period_end=period_end,
        status=CourierCashHandoverStatus.CONFIRMED,
    )
    pending_total = _handover_total(
        db,
        courier_id=courier_id,
        period_start=period_start,
        period_end=period_end,
        status=CourierCashHandoverStatus.PENDING,
    )
    cash_total = _sum_money(_cash_paid_amount(order) for order in orders)
    courier_pay_total = _sum_money(order.courier_pay for order in orders)
    paid_courier_pay_total = _sum_money(
        order.courier_pay
        for order in orders
        if _paid_amount(order) > MONEY_ZERO
    )
    raw_due = cash_total - paid_courier_pay_total - confirmed_total - pending_total
    return CourierPeriodMoney(
        total_delivery=_sum_money(order.delivery_cost for order in orders),
        paid_total=_sum_money(_paid_amount(order) for order in orders),
        cash_total=cash_total,
        courier_pay_total=courier_pay_total,
        paid_courier_pay_total=paid_courier_pay_total,
        confirmed_total=confirmed_total,
        pending_total=pending_total,
        due_to_office=max(_money(raw_due), MONEY_ZERO),
    )


def list_period_handovers(
    db: Session,
    *,
    courier_id: int,
    period_start: date,
    period_end: date,
) -> list[CourierCashHandover]:
    return list(
        db.scalars(
            select(CourierCashHandover)
            .options(
                selectinload(CourierCashHandover.courier),
                selectinload(CourierCashHandover.created_by),
                selectinload(CourierCashHandover.confirmed_by),
            )
            .where(
                CourierCashHandover.courier_id == courier_id,
                CourierCashHandover.period_start >= period_start,
                CourierCashHandover.period_end <= period_end,
            )
            .order_by(CourierCashHandover.created_at.desc(), CourierCashHandover.id.desc())
        ).all()
    )


def list_handovers(
    db: Session,
    *,
    status: CourierCashHandoverStatus | None = None,
    courier_id: int | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> CourierHandoverList:
    statement = (
        select(CourierCashHandover)
        .options(
            selectinload(CourierCashHandover.courier),
            selectinload(CourierCashHandover.created_by),
            selectinload(CourierCashHandover.confirmed_by),
        )
        .order_by(CourierCashHandover.created_at.desc(), CourierCashHandover.id.desc())
    )
    if status is not None:
        statement = statement.where(CourierCashHandover.status == status)
    if courier_id is not None:
        statement = statement.where(CourierCashHandover.courier_id == courier_id)
    if period_start is not None:
        statement = statement.where(CourierCashHandover.period_start >= period_start)
    if period_end is not None:
        statement = statement.where(CourierCashHandover.period_end <= period_end)

    rows = list(db.scalars(statement).all())
    return CourierHandoverList(
        rows=rows,
        pending_count=sum(1 for row in rows if row.status == CourierCashHandoverStatus.PENDING),
        pending_total=_sum_money(
            row.amount for row in rows if row.status == CourierCashHandoverStatus.PENDING
        ),
        confirmed_total=_sum_money(
            row.amount for row in rows if row.status == CourierCashHandoverStatus.CONFIRMED
        ),
        rejected_total=_sum_money(
            row.amount for row in rows if row.status == CourierCashHandoverStatus.REJECTED
        ),
    )


def create_handover(
    db: Session,
    *,
    courier: User,
    amount_value: Any,
    period_start: date,
    period_end: date,
) -> CourierCashHandover:
    if courier.role != UserRole.COURIER:
        raise PermissionError(HANDOVER_ACCESS_MESSAGE)
    if period_end < period_start:
        raise ValueError(HANDOVER_SAVE_MESSAGE)

    amount = _positive_money(amount_value)
    handover = CourierCashHandover(
        courier_id=courier.id,
        period_start=period_start,
        period_end=period_end,
        amount=amount,
        status=CourierCashHandoverStatus.PENDING,
        created_by_id=courier.id,
    )
    db.add(handover)
    db.commit()
    db.refresh(handover)
    return handover


def confirm_handover(
    db: Session,
    handover_id: int,
    admin: User,
    *,
    comment: Any = None,
) -> CourierCashHandover:
    return _process_handover(
        db,
        handover_id,
        admin,
        next_status=CourierCashHandoverStatus.CONFIRMED,
        comment=comment,
    )


def reject_handover(
    db: Session,
    handover_id: int,
    admin: User,
    *,
    comment: Any = None,
) -> CourierCashHandover:
    return _process_handover(
        db,
        handover_id,
        admin,
        next_status=CourierCashHandoverStatus.REJECTED,
        comment=comment,
    )


def handover_status_label(status: CourierCashHandoverStatus | str | None) -> str:
    value = status.value if isinstance(status, CourierCashHandoverStatus) else status
    return {
        CourierCashHandoverStatus.PENDING.value: "На проверке",
        CourierCashHandoverStatus.CONFIRMED.value: "Подтверждено",
        CourierCashHandoverStatus.REJECTED.value: "Отклонено",
    }.get(value or "", "Не указан")


def handover_status_class(status: CourierCashHandoverStatus | str | None) -> str:
    value = status.value if isinstance(status, CourierCashHandoverStatus) else status
    return {
        CourierCashHandoverStatus.PENDING.value: "status-wait",
        CourierCashHandoverStatus.CONFIRMED.value: "status-paid",
        CourierCashHandoverStatus.REJECTED.value: "status-rejected",
    }.get(value or "", "status-wait")


def _process_handover(
    db: Session,
    handover_id: int,
    admin: User,
    *,
    next_status: CourierCashHandoverStatus,
    comment: Any = None,
) -> CourierCashHandover:
    if admin.role != UserRole.ADMIN:
        raise PermissionError(HANDOVER_ACCESS_MESSAGE)

    handover = db.scalar(
        select(CourierCashHandover)
        .options(selectinload(CourierCashHandover.courier))
        .where(CourierCashHandover.id == handover_id)
    )
    if handover is None:
        raise ValueError(HANDOVER_SAVE_MESSAGE)
    if handover.status != CourierCashHandoverStatus.PENDING:
        raise ValueError(HANDOVER_ALREADY_PROCESSED_MESSAGE)
    if handover.courier_id == admin.id:
        raise PermissionError(HANDOVER_ACCESS_MESSAGE)

    handover.status = next_status
    handover.confirmed_by_id = admin.id
    handover.confirmed_at = datetime.now(timezone.utc)
    handover.comment = _nullable_text(comment)
    db.commit()
    db.refresh(handover)
    return handover


def _list_period_orders(
    db: Session,
    *,
    courier_id: int,
    period_start: date,
    period_end: date,
) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.payments))
            .where(
                Order.courier_id == courier_id,
                Order.is_archived.is_(False),
                Order.delivery_date.is_not(None),
                Order.delivery_date >= period_start,
                Order.delivery_date <= period_end,
            )
            .order_by(Order.delivery_date.asc(), Order.created_at.asc(), Order.id.asc())
        ).all()
    )


def _handover_total(
    db: Session,
    *,
    courier_id: int,
    period_start: date,
    period_end: date,
    status: CourierCashHandoverStatus,
) -> Decimal:
    handovers = db.scalars(
        select(CourierCashHandover.amount).where(
            CourierCashHandover.courier_id == courier_id,
            CourierCashHandover.status == status,
            CourierCashHandover.period_start >= period_start,
            CourierCashHandover.period_end <= period_end,
        )
    ).all()
    return _sum_money(handovers)


def _paid_amount(order: Order) -> Decimal:
    return _sum_money(
        payment.amount
        for payment in order.payments
        if payment.status == PaymentStatus.PAID
    )


def _cash_paid_amount(order: Order) -> Decimal:
    return _sum_money(
        payment.amount
        for payment in order.payments
        if payment.status == PaymentStatus.PAID and payment.method == PaymentMethod.CASH
    )


def _positive_money(value: Any) -> Decimal:
    amount = _money_from_form(value)
    if amount <= MONEY_ZERO:
        raise ValueError(HANDOVER_AMOUNT_POSITIVE_MESSAGE)
    return amount


def _money_from_form(value: Any) -> Decimal:
    raw_value = str(value or "").replace(",", ".").strip()
    if not raw_value:
        raise ValueError(HANDOVER_AMOUNT_NUMBER_MESSAGE)
    try:
        decimal_value = Decimal(raw_value)
        if not decimal_value.is_finite():
            raise InvalidOperation
        return decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(HANDOVER_AMOUNT_NUMBER_MESSAGE) from exc


def _money(value: Any) -> Decimal:
    return Decimal(str(value or MONEY_ZERO)).quantize(Decimal("0.01"))


def _sum_money(values) -> Decimal:
    return sum((_money(value) for value in values), MONEY_ZERO).quantize(MONEY_ZERO)


def _nullable_text(value: Any) -> str | None:
    text = " ".join(str(value or "").strip().split())
    return text or None
