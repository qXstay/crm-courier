from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import calendar

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.enums import PaymentDisplayStatus, PaymentStatus
from app.models.order import Order


MONEY_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class AccountingSummary:
    orders_count: int
    orders_total: Decimal
    paid_total: Decimal
    pending_total: Decimal
    market_expenses_total: Decimal
    courier_pay_total: Decimal
    profit_total: Decimal


@dataclass(frozen=True)
class AccountingExpenseBreakdown:
    cube: Decimal
    loader: Decimal
    storage: Decimal
    kara: Decimal
    other: Decimal


@dataclass(frozen=True)
class AccountingRow:
    order: Order
    paid_amount: Decimal
    pending_amount: Decimal
    market_expenses: Decimal
    courier_pay: Decimal
    profit: Decimal
    payment_status: PaymentDisplayStatus


@dataclass(frozen=True)
class AccountingReport:
    period: str
    selected_date: date
    period_start: date
    period_end: date
    rows: list[AccountingRow]
    summary: AccountingSummary
    expenses: AccountingExpenseBreakdown


def get_accounting_report(
    db: Session,
    *,
    period: str,
    selected_date: date,
) -> AccountingReport:
    clean_period = period if period in {"day", "month"} else "day"
    period_start, period_end = _period_bounds(clean_period, selected_date)
    orders = list(
        db.scalars(
            select(Order)
            .options(
                selectinload(Order.courier),
                selectinload(Order.payments),
            )
            .where(
                Order.is_archived.is_(False),
                Order.delivery_date.is_not(None),
                Order.delivery_date >= period_start,
                Order.delivery_date < period_end,
            )
            .order_by(Order.delivery_date.desc(), Order.created_at.desc(), Order.id.desc())
        ).all()
    )

    rows = [_row_for_order(order) for order in orders]
    expenses = AccountingExpenseBreakdown(
        cube=_sum_money(order.market_cube_cost for order in orders),
        loader=_sum_money(order.market_loader_cost for order in orders),
        storage=_sum_money(order.market_storage_cost for order in orders),
        kara=_sum_money(order.market_kara_cost for order in orders),
        other=_sum_money(order.market_other_cost for order in orders),
    )
    summary = AccountingSummary(
        orders_count=len(rows),
        orders_total=_sum_money(row.order.delivery_cost for row in rows),
        paid_total=_sum_money(row.paid_amount for row in rows),
        pending_total=_sum_money(row.pending_amount for row in rows),
        market_expenses_total=_sum_money(row.market_expenses for row in rows),
        courier_pay_total=_sum_money(row.courier_pay for row in rows),
        profit_total=_sum_money(row.profit for row in rows),
    )
    return AccountingReport(
        period=clean_period,
        selected_date=selected_date,
        period_start=period_start,
        period_end=period_end,
        rows=rows,
        summary=summary,
        expenses=expenses,
    )


def accounting_period_label(period: str) -> str:
    return "Месяц" if period == "month" else "День"


def accounting_period_caption(report: AccountingReport) -> str:
    if report.period == "month":
        return f"Заявки за {report.selected_date.strftime('%m.%Y')}."
    return f"Заявки за {report.selected_date.strftime('%d.%m.%Y')}."


def accounting_payment_status_label(status: PaymentDisplayStatus | PaymentStatus | str | None) -> str:
    value = status.value if isinstance(status, (PaymentDisplayStatus, PaymentStatus)) else status
    return {
        PaymentDisplayStatus.PAID.value: "Оплачено",
        PaymentDisplayStatus.PARTIAL.value: "Частично",
        PaymentDisplayStatus.PENDING.value: "Не оплачено",
    }.get(value or "", "Не оплачено")


def accounting_payment_status_class(status: PaymentDisplayStatus | PaymentStatus | str | None) -> str:
    value = status.value if isinstance(status, (PaymentDisplayStatus, PaymentStatus)) else status
    return {
        PaymentDisplayStatus.PAID.value: "status-paid",
        PaymentDisplayStatus.PARTIAL.value: "status-partial",
        PaymentDisplayStatus.PENDING.value: "status-wait",
    }.get(value or "", "status-wait")


def _row_for_order(order: Order) -> AccountingRow:
    paid_amount = _paid_amount(order)
    market_expenses = _market_expenses(order)
    courier_pay = _money(order.courier_pay)
    pending_amount = max(_money(order.delivery_cost) - paid_amount, MONEY_ZERO)
    if paid_amount <= MONEY_ZERO:
        payment_status = PaymentDisplayStatus.PENDING
    elif pending_amount > MONEY_ZERO:
        payment_status = PaymentDisplayStatus.PARTIAL
    else:
        payment_status = PaymentDisplayStatus.PAID
    return AccountingRow(
        order=order,
        paid_amount=paid_amount,
        pending_amount=pending_amount,
        market_expenses=market_expenses,
        courier_pay=courier_pay,
        profit=paid_amount - market_expenses - courier_pay,
        payment_status=payment_status,
    )


def _paid_amount(order: Order) -> Decimal:
    return _sum_money(
        payment.amount
        for payment in order.payments
        if payment.status == PaymentStatus.PAID
    )


def _market_expenses(order: Order) -> Decimal:
    return _sum_money(
        (
            order.market_cube_cost,
            order.market_loader_cost,
            order.market_storage_cost,
            order.market_kara_cost,
            order.market_other_cost,
        )
    )


def _sum_money(values) -> Decimal:
    return sum((_money(value) for value in values), MONEY_ZERO)


def _money(value) -> Decimal:
    return Decimal(str(value or MONEY_ZERO)).quantize(Decimal("0.01"))


def _period_bounds(period: str, selected_date: date) -> tuple[date, date]:
    if period == "month":
        start = selected_date.replace(day=1)
        days_in_month = calendar.monthrange(selected_date.year, selected_date.month)[1]
        return start, start + timedelta(days=days_in_month)
    return selected_date, selected_date + timedelta(days=1)
