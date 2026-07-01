from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.enums import OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.services.courier_cash_handover_service import (
    CourierPeriodMoney,
    calculate_period_money,
    list_period_handovers,
)


MONEY_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class CourierDashboard:
    courier: User
    working_orders: list[Order]
    delivered_orders: list[Order]
    period_orders: list[Order]
    detail_orders: list[Order]
    handovers: list
    money: CourierPeriodMoney
    mode: str
    detail: str
    detail_title: str
    period_start: date
    period_end: date
    delivered_date: date
    working_days_count: int
    period_orders_count: int
    period_in_work_count: int
    period_at_courier_count: int
    period_delivered_count: int
    is_today: bool
    today_orders_count: int
    today_delivery_total: Decimal
    today_courier_pay_total: Decimal
    delivered_today_count: int
    delivered_today_delivery_total: Decimal
    delivered_today_courier_pay_total: Decimal
    working_today_count: int
    working_today_delivery_total: Decimal
    working_today_courier_pay_total: Decimal
    month_courier_pay_total: Decimal


@dataclass(frozen=True)
class CourierRoute:
    courier: User | None
    route_date: date
    period_start: date
    period_end: date
    orders: list[Order]
    orders_count: int
    in_work_count: int
    at_courier_count: int
    delivered_count: int
    delivery_total: Decimal
    courier_pay_total: Decimal


@dataclass(frozen=True)
class CourierListItem:
    courier: User
    orders_count: int
    in_work_count: int
    at_courier_count: int
    delivered_count: int
    delivery_total: Decimal
    courier_pay_total: Decimal


@dataclass(frozen=True)
class RouteSummary:
    route_date: date
    courier: User | None
    courier_id: str
    detail: str
    couriers: list[CourierListItem]
    orders: list[Order]
    detail_couriers: list[CourierListItem]
    detail_orders: list[Order]
    orders_count: int
    in_work_count: int
    at_courier_count: int
    delivered_count: int
    couriers_count: int
    delivery_total: Decimal
    courier_pay_total: Decimal
    detail_title: str


def list_active_couriers(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.role == UserRole.COURIER, User.is_active.is_(True))
            .order_by(User.full_name, User.id)
        ).all()
    )


def get_courier(db: Session, courier_id: int) -> User | None:
    return db.scalar(
        select(User).where(
            User.id == courier_id,
            User.role == UserRole.COURIER,
            User.is_active.is_(True),
        )
    )


def list_couriers_for_route(db: Session, *, route_date: date | None = None) -> list[CourierListItem]:
    items: list[CourierListItem] = []
    for courier in list_active_couriers(db):
        orders = _list_orders(db, courier.id, route_date=route_date) if route_date else _list_orders(db, courier.id)
        items.append(
            CourierListItem(
                courier=courier,
                orders_count=len(orders),
                in_work_count=sum(1 for order in orders if order.status == OrderStatus.IN_WORK),
                at_courier_count=sum(1 for order in orders if order.status == OrderStatus.AT_COURIER),
                delivered_count=sum(1 for order in orders if order.status == OrderStatus.DELIVERED),
                delivery_total=_sum_money(order.delivery_cost for order in orders),
                courier_pay_total=_sum_money(order.courier_pay for order in orders),
            )
        )
    return items


def get_route_summary(
    db: Session,
    *,
    route_date: date | None = None,
    courier_id: str | int | None = "all",
    detail: str | None = "",
    today: date | None = None,
) -> RouteSummary:
    selected_date = route_date or today or date.today()
    selected_courier_id = str(courier_id or "all").strip() or "all"
    selected_detail = detail if detail in {"couriers", "all", "in_work", "at_courier", "delivered"} else ""
    selected_courier = None
    if selected_courier_id != "all":
        try:
            selected_courier = get_courier(db, int(selected_courier_id))
        except ValueError:
            selected_courier = None
        if selected_courier is None:
            selected_courier_id = "all"

    all_orders = _list_route_orders(db, route_date=selected_date, courier=selected_courier)
    courier_items = _courier_items_for_orders(list_active_couriers(db), all_orders)
    active_courier_items = [item for item in courier_items if item.orders_count > 0]
    detail_orders = _detail_orders(all_orders, selected_detail)
    detail_couriers = active_courier_items if selected_detail == "couriers" else []

    return RouteSummary(
        route_date=selected_date,
        courier=selected_courier,
        courier_id=selected_courier_id,
        detail=selected_detail,
        couriers=courier_items,
        orders=all_orders,
        detail_couriers=detail_couriers,
        detail_orders=[] if selected_detail == "couriers" else detail_orders,
        orders_count=len(all_orders),
        in_work_count=sum(1 for order in all_orders if order.status == OrderStatus.IN_WORK),
        at_courier_count=sum(1 for order in all_orders if order.status == OrderStatus.AT_COURIER),
        delivered_count=sum(1 for order in all_orders if order.status == OrderStatus.DELIVERED),
        couriers_count=len(active_courier_items),
        delivery_total=_sum_money(order.delivery_cost for order in all_orders),
        courier_pay_total=_sum_money(order.courier_pay for order in all_orders),
        detail_title=_detail_title(selected_detail),
    )


def get_courier_dashboard(
    db: Session,
    courier: User,
    *,
    today: date | None = None,
    delivered_date: date | None = None,
    mode: str = "today",
    period_start: date | None = None,
    period_end: date | None = None,
    detail: str | None = "",
) -> CourierDashboard:
    today = today or date.today()
    selected_mode, selected_start, selected_end = _resolve_period(
        mode=mode,
        today=today,
        period_start=period_start or delivered_date,
        period_end=period_end,
    )
    delivered_date = delivered_date or selected_start
    working_orders = _list_orders(
        db,
        courier.id,
        statuses=(OrderStatus.IN_WORK, OrderStatus.AT_COURIER),
    )
    delivered_orders = _list_orders(
        db,
        courier.id,
        route_date=delivered_date,
        statuses=(OrderStatus.DELIVERED,),
    )
    delivered_today_orders = (
        delivered_orders
        if delivered_date == today
        else _list_orders(
            db,
            courier.id,
            route_date=today,
            statuses=(OrderStatus.DELIVERED,),
        )
    )
    month_start = today.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    month_delivered_orders = _list_orders(
        db,
        courier.id,
        statuses=(OrderStatus.DELIVERED,),
        period_start=month_start,
        period_end=month_end,
    )
    working_today_orders = [order for order in working_orders if order.delivery_date == today]
    today_orders = [*working_today_orders, *delivered_today_orders]
    period_orders = _list_orders(
        db,
        courier.id,
        period_start=selected_start,
        period_end=selected_end,
    )
    selected_detail = _dashboard_detail(detail)
    detail_orders = _dashboard_detail_orders(period_orders, selected_detail)
    money = calculate_period_money(
        db,
        courier_id=courier.id,
        period_start=selected_start,
        period_end=selected_end,
    )
    return CourierDashboard(
        courier=courier,
        working_orders=working_orders,
        delivered_orders=delivered_orders,
        period_orders=period_orders,
        detail_orders=detail_orders,
        handovers=list_period_handovers(
            db,
            courier_id=courier.id,
            period_start=selected_start,
            period_end=selected_end,
        ),
        money=money,
        mode=selected_mode,
        detail=selected_detail,
        detail_title=_dashboard_detail_title(selected_detail),
        period_start=selected_start,
        period_end=selected_end,
        delivered_date=delivered_date,
        working_days_count=len({order.delivery_date for order in period_orders if order.delivery_date}),
        period_orders_count=len(period_orders),
        period_in_work_count=sum(1 for order in period_orders if order.status == OrderStatus.IN_WORK),
        period_at_courier_count=sum(1 for order in period_orders if order.status == OrderStatus.AT_COURIER),
        period_delivered_count=sum(1 for order in period_orders if order.status == OrderStatus.DELIVERED),
        is_today=selected_start == today and selected_end == today,
        today_orders_count=len(today_orders),
        today_delivery_total=_sum_money(order.delivery_cost for order in today_orders),
        today_courier_pay_total=_sum_money(order.courier_pay for order in today_orders),
        delivered_today_count=len(delivered_today_orders),
        delivered_today_delivery_total=_sum_money(order.delivery_cost for order in delivered_today_orders),
        delivered_today_courier_pay_total=_sum_money(order.courier_pay for order in delivered_today_orders),
        working_today_count=len(working_today_orders),
        working_today_delivery_total=_sum_money(order.delivery_cost for order in working_today_orders),
        working_today_courier_pay_total=_sum_money(order.courier_pay for order in working_today_orders),
        month_courier_pay_total=_sum_money(order.courier_pay for order in month_delivered_orders),
    )


def get_first_courier_dashboard(
    db: Session,
    *,
    today: date | None = None,
    delivered_date: date | None = None,
    mode: str = "today",
    period_start: date | None = None,
    period_end: date | None = None,
    detail: str | None = "",
) -> CourierDashboard | None:
    courier = next(iter(list_active_couriers(db)), None)
    if courier is None:
        return None
    return get_courier_dashboard(
        db,
        courier,
        today=today,
        delivered_date=delivered_date,
        mode=mode,
        period_start=period_start,
        period_end=period_end,
        detail=detail,
    )


def get_courier_route(
    db: Session,
    courier_id: int,
    route_date: date,
    period_end: date | None = None,
) -> CourierRoute:
    selected_end = period_end or route_date
    if selected_end < route_date:
        selected_end = route_date
    courier = get_courier(db, courier_id)
    if courier is None:
        return CourierRoute(
            courier=None,
            route_date=route_date,
            period_start=route_date,
            period_end=selected_end,
            orders=[],
            orders_count=0,
            in_work_count=0,
            at_courier_count=0,
            delivered_count=0,
            delivery_total=MONEY_ZERO,
            courier_pay_total=MONEY_ZERO,
        )

    orders = _list_orders(db, courier.id, period_start=route_date, period_end=selected_end)
    return CourierRoute(
        courier=courier,
        route_date=route_date,
        period_start=route_date,
        period_end=selected_end,
        orders=orders,
        orders_count=len(orders),
        in_work_count=sum(1 for order in orders if order.status == OrderStatus.IN_WORK),
        at_courier_count=sum(1 for order in orders if order.status == OrderStatus.AT_COURIER),
        delivered_count=sum(1 for order in orders if order.status == OrderStatus.DELIVERED),
        delivery_total=_sum_money(order.delivery_cost for order in orders),
        courier_pay_total=_sum_money(order.courier_pay for order in orders),
    )


def get_order_for_courier(db: Session, order_id: int, courier: User) -> Order | None:
    return db.scalar(
        select(Order)
        .options(selectinload(Order.client), selectinload(Order.courier))
        .where(
            Order.id == order_id,
            Order.courier_id == courier.id,
            Order.is_archived.is_(False),
        )
    )


def _list_orders(
    db: Session,
    courier_id: int,
    *,
    route_date: date | None = None,
    statuses: tuple[OrderStatus, ...] | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> list[Order]:
    statement = (
        select(Order)
        .options(selectinload(Order.client), selectinload(Order.courier))
        .where(Order.courier_id == courier_id, Order.is_archived.is_(False))
    )
    if statuses is not None:
        statement = statement.where(Order.status.in_(statuses))
    if route_date is not None:
        statement = statement.where(Order.delivery_date == route_date)
        statement = statement.order_by(Order.delivery_date.asc(), Order.created_at.asc(), Order.id.asc())
    else:
        if period_start is not None:
            statement = statement.where(Order.delivery_date >= period_start)
        if period_end is not None:
            statement = statement.where(Order.delivery_date <= period_end)
        statement = statement.order_by(
            Order.delivery_date.desc().nullslast(),
            Order.created_at.desc(),
            Order.id.desc(),
        )
    return list(db.scalars(statement).all())


def _list_route_orders(
    db: Session,
    *,
    route_date: date,
    courier: User | None = None,
) -> list[Order]:
    statement = (
        select(Order)
        .options(selectinload(Order.client), selectinload(Order.courier))
        .where(Order.delivery_date == route_date, Order.is_archived.is_(False))
        .order_by(
            Order.courier_id.is_(None),
            Order.courier_id.asc(),
            Order.created_at.asc(),
            Order.id.asc(),
        )
    )
    if courier is not None:
        statement = statement.where(Order.courier_id == courier.id)
    return list(db.scalars(statement).all())


def _courier_items_for_orders(couriers: list[User], orders: list[Order]) -> list[CourierListItem]:
    items: list[CourierListItem] = []
    for courier in couriers:
        courier_orders = [order for order in orders if order.courier_id == courier.id]
        items.append(
            CourierListItem(
                courier=courier,
                orders_count=len(courier_orders),
                in_work_count=sum(1 for order in courier_orders if order.status == OrderStatus.IN_WORK),
                at_courier_count=sum(1 for order in courier_orders if order.status == OrderStatus.AT_COURIER),
                delivered_count=sum(1 for order in courier_orders if order.status == OrderStatus.DELIVERED),
                delivery_total=_sum_money(order.delivery_cost for order in courier_orders),
                courier_pay_total=_sum_money(order.courier_pay for order in courier_orders),
            )
        )
    return items


def _detail_orders(orders: list[Order], detail: str) -> list[Order]:
    if detail == "all":
        return orders
    status_by_detail = {
        "in_work": OrderStatus.IN_WORK,
        "at_courier": OrderStatus.AT_COURIER,
        "delivered": OrderStatus.DELIVERED,
    }
    status = status_by_detail.get(detail)
    if status is None:
        return []
    return [order for order in orders if order.status == status]


def _detail_title(detail: str) -> str:
    return {
        "couriers": "Курьеры с заявками",
        "all": "Все заявки",
        "in_work": "В работе",
        "at_courier": "У курьера",
        "delivered": "Доставлено",
    }.get(detail, "")


def _dashboard_detail(value: str | None) -> str:
    return value if value in {"assigned", "delivered"} else ""


def _dashboard_detail_orders(orders: list[Order], detail: str) -> list[Order]:
    if detail == "assigned":
        return orders
    if detail == "delivered":
        return [order for order in orders if order.status == OrderStatus.DELIVERED]
    return []


def _dashboard_detail_title(detail: str) -> str:
    return {
        "assigned": "Всего заявок назначено",
        "delivered": "Доставлено",
    }.get(detail, "")


def _sum_money(values) -> Decimal:
    total = MONEY_ZERO
    for value in values:
        total += value or MONEY_ZERO
    return total.quantize(MONEY_ZERO)


def _resolve_period(
    *,
    mode: str,
    today: date,
    period_start: date | None,
    period_end: date | None,
) -> tuple[str, date, date]:
    clean_mode = "period" if mode == "period" else "date"

    selected_start = period_start or today
    selected_end = period_end or selected_start
    if clean_mode == "date":
        selected_end = selected_start
    if selected_end < selected_start:
        selected_end = selected_start

    return clean_mode, selected_start, selected_end
