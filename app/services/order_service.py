from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.enums import OrderStatus, UserRole
from app.models.client import Client
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.client_service import (
    find_client_by_phone,
    format_russian_phone,
    get_client,
    get_or_create_client_by_phone,
    normalize_client_name,
    normalized_name_key,
    normalize_phone,
)
from app.utils.dates import default_delivery_date


ORDER_SERIES = "BP"
MONEY_ZERO = Decimal("0.00")
CLIENT_NAME_REQUIRED_MESSAGE = "Укажите ФИО клиента."
CLIENT_PHONE_REQUIRED_MESSAGE = "Укажите телефон клиента."
ADDRESS_REQUIRED_MESSAGE = "Укажите адрес доставки."
WEIGHT_NUMBER_MESSAGE = "Вес должен быть числом."
VOLUME_NUMBER_MESSAGE = "Объём должен быть числом."
PLACES_INTEGER_MESSAGE = "Кол-во мест должно быть целым числом."
EXPENSE_NUMBER_MESSAGE = "Расходы должны быть числом."
EXPENSE_NON_NEGATIVE_MESSAGE = "Расходы не могут быть отрицательными."
CARGO_PHONE_INVALID_MESSAGE = "Проверьте телефон карго. Формат: +7 900 000-00-00."
COURIER_REQUIRED_MESSAGE = "Выберите курьера для статуса «У курьера»."
COURIER_NOT_FOUND_MESSAGE = "Курьер не найден."
CLIENT_AMBIGUOUS_NAME_MESSAGE = (
    "Нашли несколько клиентов с таким ФИО. Уточните телефон или выберите клиента из карточки."
)
CLIENT_NOT_FOUND_MESSAGE = "Клиент не найден."
ACTIVE_CARGO_DUPLICATE_MESSAGE = "Заявка с таким номером груза уже есть: {order_code}"
ACTIVE_ORDER_STATUSES = (
    OrderStatus.IN_WORK,
    OrderStatus.AT_COURIER,
    OrderStatus.DELIVERED,
)


@dataclass(frozen=True)
class OrderListFilters:
    search: str | None = None
    delivery_date_from: date | None = None
    delivery_date_to: date | None = None
    status: OrderStatus | None = None
    courier_id: int | None = None


class DuplicateCargoNumberError(ValueError):
    def __init__(self, order: Order):
        super().__init__(ACTIVE_CARGO_DUPLICATE_MESSAGE.format(order_code=order.order_code))
        self.order = order


def list_active_orders(
    db: Session,
    *,
    filters: OrderListFilters | None = None,
) -> list[Order]:
    statement = (
        select(Order)
        .options(selectinload(Order.client), selectinload(Order.courier))
        .where(Order.is_archived.is_(False))
    )
    if filters:
        if filters.delivery_date_from is not None:
            statement = statement.where(Order.delivery_date >= filters.delivery_date_from)
        if filters.delivery_date_to is not None:
            statement = statement.where(Order.delivery_date <= filters.delivery_date_to)
        if filters.status is not None:
            statement = statement.where(Order.status == filters.status)
        if filters.courier_id is not None:
            statement = statement.where(Order.courier_id == filters.courier_id)

    statement = statement.order_by(Order.created_at.desc(), Order.id.desc())
    orders = list(db.scalars(statement).all())
    search = _clean_text(filters.search if filters else None)
    if search:
        orders = [order for order in orders if _order_matches_search(order, search)]

    return orders


def list_archived_orders(db: Session) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .options(
                selectinload(Order.client),
                selectinload(Order.courier),
                selectinload(Order.archived_by),
            )
            .where(Order.is_archived.is_(True))
            .order_by(Order.archived_at.desc().nullslast(), Order.id.desc())
        ).all()
    )


def get_order(db: Session, order_id: int) -> Order | None:
    return db.scalar(
        select(Order)
        .options(
            selectinload(Order.client),
            selectinload(Order.courier),
            selectinload(Order.created_by),
            selectinload(Order.archived_by),
            selectinload(Order.payments).selectinload(Payment.created_by),
            selectinload(Order.change_logs).selectinload(OrderChangeLog.user),
        )
        .where(Order.id == order_id)
    )


def list_active_couriers(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.role == UserRole.COURIER, User.is_active.is_(True))
            .order_by(User.full_name)
        ).all()
    )


def create_order(db: Session, form_data: dict[str, Any], user: User) -> Order:
    _validate_order_form(form_data)
    _ensure_unique_active_cargo_number(db, form_data.get("cargo_number"))
    client_name = normalize_client_name(form_data.get("client_name"))
    client_phone = format_russian_phone(form_data.get("client_phone"))
    address = _normalize_address(form_data.get("address"))
    client = resolve_order_client(db, form_data, client_name, client_phone, user)
    if _nullable_int(form_data.get("client_id")) is not None:
        client_name = normalize_client_name(client.full_name)
        client_phone = format_russian_phone(client.phone)

    order_number = _next_order_number(db)
    courier_id = _nullable_int(form_data.get("courier_id"))
    costs = _costs_from_form(form_data)

    order = Order(
        order_series=ORDER_SERIES,
        order_number=order_number,
        order_code=f"{ORDER_SERIES}-{order_number:04d}",
        client_id=client.id,
        client_name_snapshot=client_name,
        client_phone_snapshot=client_phone,
        address=address,
        delivery_date=_delivery_date_from_form(form_data),
        general_note=_nullable_text(form_data.get("general_note")),
        cargo_number=_nullable_text(form_data.get("cargo_number")),
        cargo_phone=_nullable_cargo_phone(form_data.get("cargo_phone")),
        client_note=_nullable_text(form_data.get("client_note")),
        staff_note=_nullable_text(form_data.get("staff_note")),
        weight=_nullable_decimal(form_data.get("weight"), scale="0.001"),
        volume=_nullable_decimal(form_data.get("volume"), scale="0.001"),
        places_count=_nullable_int(form_data.get("places_count")),
        courier_id=courier_id,
        status=_resolve_status(courier_id, form_data.get("status"), allow_delivered=False),
        created_by_id=user.id,
        **costs,
    )
    order.delivery_cost = calculate_delivery_cost(order)

    db.add(order)
    db.flush()
    _write_log(db, order, user, "создание", None, _order_snapshot(order))
    db.commit()
    db.refresh(order)
    return order


def archive_order(db: Session, order_id: int, user: User) -> Order:
    if user.role not in (UserRole.ADMIN, UserRole.MANAGER):
        raise PermissionError("Недостаточно прав")

    order = get_order(db, order_id)
    if order is None:
        raise ValueError("Заявка не найдена")
    if order.is_archived:
        return order

    old_snapshot = _order_snapshot(order)
    order.is_archived = True
    order.archived_at = datetime.now(timezone.utc)
    order.archived_by_id = user.id

    db.flush()
    _write_log(db, order, user, "архив", old_snapshot, _order_snapshot(order))
    db.commit()
    db.refresh(order)
    return order


def archive_orders_bulk(
    db: Session,
    order_ids: Any,
    user: User,
) -> int:
    """Архивирует несколько заявок одним коммитом. Только admin.

    Возвращает количество реально архивированных заявок. Уже архивные и
    неизвестные id пропускаются без ошибки. Платежи, клиенты, логи и связи
    не затрагиваются. На каждую архивированную заявку пишется order_log.
    """
    if user.role != UserRole.ADMIN:
        raise PermissionError("Недостаточно прав")

    unique_ids: list[int] = []
    seen: set[int] = set()
    for raw in order_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in seen:
            seen.add(value)
            unique_ids.append(value)

    if not unique_ids:
        return 0

    orders = list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.archived_by))
            .where(Order.id.in_(unique_ids), Order.is_archived.is_(False))
        ).all()
    )
    if not orders:
        return 0

    archived_at = datetime.now(timezone.utc)
    for order in orders:
        old_snapshot = _order_snapshot(order)
        order.is_archived = True
        order.archived_at = archived_at
        order.archived_by_id = user.id
        _write_log(db, order, user, "архив", old_snapshot, _order_snapshot(order))

    db.commit()
    return len(orders)


def restore_order(db: Session, order_id: int, user: User) -> Order:
    if user.role != UserRole.ADMIN:
        raise PermissionError("Недостаточно прав")

    order = get_order(db, order_id)
    if order is None:
        raise ValueError("Заявка не найдена")
    if not order.is_archived:
        return order

    _ensure_unique_active_cargo_number(db, order.cargo_number, exclude_order_id=order.id)

    old_snapshot = _order_snapshot(order)
    order.is_archived = False
    order.archived_at = None
    order.archived_by_id = None

    db.flush()
    _write_log(db, order, user, "восстановление", old_snapshot, _order_snapshot(order))
    db.commit()
    db.refresh(order)
    return order


def update_order_status(
    db: Session,
    order_id: int,
    status_value: str,
    user: User,
    *,
    courier_id_value: Any = None,
) -> Order:
    if user.role not in (UserRole.ADMIN, UserRole.MANAGER):
        raise PermissionError("Недостаточно прав")

    order = get_order(db, order_id)
    if order is None or order.is_archived:
        raise ValueError("Заявка не найдена")

    try:
        next_status = OrderStatus(status_value)
    except ValueError as exc:
        raise ValueError("Выберите статус заявки.") from exc

    next_courier_id = order.courier_id
    if next_status == OrderStatus.AT_COURIER:
        requested_courier_id = _nullable_int(
            courier_id_value,
            error_message=COURIER_NOT_FOUND_MESSAGE,
        )
        next_courier_id = requested_courier_id or order.courier_id
        if next_courier_id is None:
            raise ValueError(COURIER_REQUIRED_MESSAGE)
        if not _active_courier_exists(db, next_courier_id):
            raise ValueError(COURIER_NOT_FOUND_MESSAGE)
    elif next_status == OrderStatus.IN_WORK:
        next_courier_id = None

    if order.status == next_status and order.courier_id == next_courier_id:
        return order

    old_snapshot = _order_snapshot(order)
    order.status = next_status
    order.courier_id = next_courier_id

    db.flush()
    _write_log(db, order, user, "статус", old_snapshot, _order_snapshot(order))
    db.commit()
    db.refresh(order)
    return order


def update_order(
    db: Session,
    order_id: int,
    form_data: dict[str, Any],
    user: User,
) -> Order:
    order = get_order(db, order_id)
    if order is None or order.is_archived:
        raise ValueError("Заявка не найдена")

    old_snapshot = _order_snapshot(order)
    _validate_order_form(form_data)
    _ensure_unique_active_cargo_number(db, form_data.get("cargo_number"), exclude_order_id=order.id)
    client_name = normalize_client_name(form_data.get("client_name"))
    client_phone = format_russian_phone(form_data.get("client_phone"))
    client = resolve_order_client(db, form_data, client_name, client_phone, user)
    if _nullable_int(form_data.get("client_id")) is not None:
        client_name = normalize_client_name(client.full_name)
        client_phone = format_russian_phone(client.phone)

    courier_id = _nullable_int(form_data.get("courier_id"))
    costs = _costs_from_form(form_data)

    order.client_id = client.id
    order.client_name_snapshot = client_name
    order.client_phone_snapshot = client_phone
    order.address = _normalize_address(form_data.get("address"))
    order.delivery_date = _nullable_date(form_data.get("delivery_date"))
    order.general_note = _nullable_text(form_data.get("general_note"))
    order.cargo_number = _nullable_text(form_data.get("cargo_number"))
    order.cargo_phone = _nullable_cargo_phone(form_data.get("cargo_phone"))
    order.client_note = _nullable_text(form_data.get("client_note"))
    order.staff_note = _nullable_text(form_data.get("staff_note"))
    order.weight = _nullable_decimal(form_data.get("weight"), scale="0.001")
    order.volume = _nullable_decimal(form_data.get("volume"), scale="0.001")
    order.places_count = _nullable_int(form_data.get("places_count"))
    order.courier_id = courier_id
    order.status = _resolve_status(courier_id, form_data.get("status"), allow_delivered=True)

    for key, value in costs.items():
        setattr(order, key, value)
    order.delivery_cost = calculate_delivery_cost(order)

    db.flush()
    _write_log(db, order, user, "редактирование", old_snapshot, _order_snapshot(order))
    db.commit()
    db.refresh(order)
    return order


def mark_order_delivered_by_courier(db: Session, order_id: int, user: User) -> Order:
    if user.role != UserRole.COURIER:
        raise PermissionError("Недостаточно прав")

    order = db.scalar(
        select(Order).where(
            Order.id == order_id,
            Order.courier_id == user.id,
            Order.is_archived.is_(False),
        )
    )
    if order is None:
        raise ValueError("Заявка не найдена")
    if order.status == OrderStatus.DELIVERED:
        return order

    old_snapshot = _order_snapshot(order)
    order.status = OrderStatus.DELIVERED

    db.flush()
    _write_log(db, order, user, "доставлено", old_snapshot, _order_snapshot(order))
    db.commit()
    db.refresh(order)
    return order


def mark_order_at_courier_by_courier(db: Session, order_id: int, user: User) -> Order:
    if user.role != UserRole.COURIER:
        raise PermissionError("Недостаточно прав")

    order = db.scalar(
        select(Order).where(
            Order.id == order_id,
            Order.courier_id == user.id,
            Order.is_archived.is_(False),
        )
    )
    if order is None:
        raise ValueError("Заявка не найдена")
    if order.status != OrderStatus.IN_WORK:
        return order

    old_snapshot = _order_snapshot(order)
    order.status = OrderStatus.AT_COURIER

    db.flush()
    _write_log(db, order, user, "у курьера", old_snapshot, _order_snapshot(order))
    db.commit()
    db.refresh(order)
    return order


def calculate_delivery_cost(order: Order) -> Decimal:
    return _money(
        (order.base_delivery_cost or MONEY_ZERO)
        + (order.market_cube_cost or MONEY_ZERO)
        + (order.market_loader_cost or MONEY_ZERO)
        + (order.market_storage_cost or MONEY_ZERO)
        + (order.market_kara_cost or MONEY_ZERO)
        + (order.market_other_cost or MONEY_ZERO)
    )


def resolve_order_client(
    db: Session,
    form_data: dict[str, Any],
    client_name: str,
    client_phone: str,
    user: User,
) -> Client:
    client_id = _nullable_int(form_data.get("client_id"))
    if client_id is not None:
        client = get_client(db, client_id)
        if client is None:
            raise ValueError(CLIENT_NOT_FOUND_MESSAGE)
        return client

    phone_client = find_client_by_phone(db, client_phone)
    if phone_client is not None:
        return phone_client

    name_client = find_client_by_exact_name(db, client_name)
    if name_client is not None:
        return name_client

    return get_or_create_client_by_phone(
        db,
        full_name=client_name,
        phone=client_phone,
        created_by=user,
    )


def find_client_by_exact_name(db: Session, full_name: str) -> Client | None:
    name_key = normalized_name_key(full_name)
    if not name_key:
        return None

    matches = [
        client
        for client in db.scalars(select(Client).order_by(Client.id)).all()
        if normalized_name_key(client.full_name) == name_key
    ]
    if len(matches) > 1:
        raise ValueError(CLIENT_AMBIGUOUS_NAME_MESSAGE)
    return matches[0] if matches else None


def _ensure_unique_active_cargo_number(
    db: Session,
    cargo_number: Any,
    *,
    exclude_order_id: int | None = None,
) -> None:
    normalized_cargo_number = _clean_text(cargo_number).casefold()
    if not normalized_cargo_number:
        return

    statement = (
        select(Order)
        .where(
            Order.is_archived.is_(False),
            Order.status.in_(ACTIVE_ORDER_STATUSES),
            Order.cargo_number.is_not(None),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
    )
    if exclude_order_id is not None:
        statement = statement.where(Order.id != exclude_order_id)

    for order in db.scalars(statement).all():
        if _clean_text(order.cargo_number).casefold() == normalized_cargo_number:
            raise DuplicateCargoNumberError(order)


def _order_matches_search(order: Order, search: str) -> bool:
    search_text = search.casefold()
    text_values = (
        order.order_code,
        order.cargo_number,
        order.client_name_snapshot,
        order.client_phone_snapshot,
        order.address,
        order.courier.full_name if order.courier else None,
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


def _active_courier_exists(db: Session, courier_id: int) -> bool:
    return (
        db.scalar(
            select(User.id).where(
                User.id == courier_id,
                User.role == UserRole.COURIER,
                User.is_active.is_(True),
            )
        )
        is not None
    )


def _next_order_number(db: Session) -> int:
    current_number = db.scalar(
        select(func.max(Order.order_number)).where(Order.order_series == ORDER_SERIES)
    )
    return int(current_number or 0) + 1


def _costs_from_form(form_data: dict[str, Any]) -> dict[str, Decimal]:
    return {
        "base_delivery_cost": _money(form_data.get("base_delivery_cost")),
        "market_cube_cost": _money(form_data.get("market_cube_cost")),
        "market_loader_cost": _money(form_data.get("market_loader_cost")),
        "market_storage_cost": _money(form_data.get("market_storage_cost")),
        "market_kara_cost": _money(form_data.get("market_kara_cost")),
        "market_other_cost": _money(form_data.get("market_other_cost")),
        "courier_pay": _money(form_data.get("courier_pay")),
    }


def _resolve_status(
    courier_id: int | None,
    requested_status: str | None,
    *,
    allow_delivered: bool,
) -> OrderStatus:
    if allow_delivered and requested_status == OrderStatus.DELIVERED.value:
        return OrderStatus.DELIVERED
    if requested_status == OrderStatus.AT_COURIER.value and not courier_id:
        raise ValueError(COURIER_REQUIRED_MESSAGE)
    if courier_id:
        return OrderStatus.AT_COURIER
    return OrderStatus.IN_WORK


def _write_log(
    db: Session,
    order: Order,
    user: User,
    action: str,
    old_value: dict[str, Any] | None,
    new_value: dict[str, Any],
) -> None:
    db.add(
        OrderChangeLog(
            order_id=order.id,
            user_id=user.id,
            action=action,
            old_value=_to_json(old_value) if old_value is not None else None,
            new_value=_to_json(new_value),
        )
    )


def _order_snapshot(order: Order) -> dict[str, Any]:
    return {
        "order_code": order.order_code,
        "client_name": order.client_name_snapshot,
        "client_phone": order.client_phone_snapshot,
        "address": order.address,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "courier_id": order.courier_id,
        "status": order.status.value if isinstance(order.status, OrderStatus) else order.status,
        "base_delivery_cost": str(order.base_delivery_cost),
        "market_cube_cost": str(order.market_cube_cost),
        "market_loader_cost": str(order.market_loader_cost),
        "market_storage_cost": str(order.market_storage_cost),
        "market_kara_cost": str(order.market_kara_cost),
        "market_other_cost": str(order.market_other_cost),
        "delivery_cost": str(order.delivery_cost),
        "courier_pay": str(order.courier_pay),
        "is_archived": order.is_archived,
        "archived_at": order.archived_at.isoformat() if order.archived_at else None,
        "archived_by_id": order.archived_by_id,
    }


def _to_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _validate_order_form(form_data: dict[str, Any]) -> None:
    errors: list[str] = []

    if not normalize_client_name(form_data.get("client_name")):
        errors.append(CLIENT_NAME_REQUIRED_MESSAGE)

    phone = _clean_text(form_data.get("client_phone"))
    if not phone or normalize_phone(phone) in {"7", "8"}:
        errors.append(CLIENT_PHONE_REQUIRED_MESSAGE)
    else:
        _collect_error(errors, lambda: format_russian_phone(phone))

    if not _normalize_address(form_data.get("address")):
        errors.append(ADDRESS_REQUIRED_MESSAGE)

    _collect_error(errors, lambda: _nullable_cargo_phone(form_data.get("cargo_phone")))

    _collect_error(
        errors,
        lambda: _nullable_decimal(
            form_data.get("weight"),
            scale="0.001",
            error_message=WEIGHT_NUMBER_MESSAGE,
        ),
    )
    _collect_error(
        errors,
        lambda: _nullable_decimal(
            form_data.get("volume"),
            scale="0.001",
            error_message=VOLUME_NUMBER_MESSAGE,
        ),
    )
    _collect_error(
        errors,
        lambda: _nullable_int(
            form_data.get("places_count"),
            error_message=PLACES_INTEGER_MESSAGE,
        ),
    )

    for key in (
        "base_delivery_cost",
        "market_cube_cost",
        "market_loader_cost",
        "market_storage_cost",
        "market_kara_cost",
        "market_other_cost",
        "courier_pay",
    ):
        _collect_error(errors, lambda key=key: _money(form_data.get(key)))

    if errors:
        raise ValueError("\n".join(dict.fromkeys(errors)))


def _collect_error(errors: list[str], callback: Callable[[], Any]) -> None:
    try:
        callback()
    except ValueError as exc:
        errors.append(str(exc) or "Не удалось сохранить. Проверьте данные.")


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_address(value: Any) -> str:
    address = _clean_text(value)
    if address[:6].lower() == "москва":
        return "Москва" + address[6:]
    return address


def _nullable_text(value: Any) -> str | None:
    value = _clean_text(value)
    return value or None


def _nullable_cargo_phone(value: Any) -> str | None:
    phone = _clean_text(value)
    if not phone:
        return None
    return format_russian_phone(phone, error_message=CARGO_PHONE_INVALID_MESSAGE)


def _nullable_int(
    value: Any,
    *,
    error_message: str = "Не удалось сохранить. Проверьте данные.",
) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not value.isdigit():
        raise ValueError(error_message)
    return int(value)


def _nullable_date(value: Any) -> date | None:
    value = str(value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


def _delivery_date_from_form(form_data: dict[str, Any]) -> date:
    return _nullable_date(form_data.get("delivery_date")) or default_delivery_date()


def _nullable_decimal(
    value: Any,
    *,
    scale: str,
    error_message: str = "Не удалось сохранить. Проверьте данные.",
    min_value: Decimal | None = None,
    min_error_message: str | None = None,
) -> Decimal | None:
    value = str(value or "").replace(",", ".").strip()
    if not value:
        return None
    try:
        decimal_value = Decimal(value)
        if not decimal_value.is_finite():
            raise InvalidOperation
        if min_value is not None and decimal_value < min_value:
            raise ValueError(min_error_message or error_message)
        return decimal_value.quantize(Decimal(scale), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(error_message) from exc


def _money(value: Any) -> Decimal:
    return _nullable_decimal(
        value,
        scale="0.01",
        error_message=EXPENSE_NUMBER_MESSAGE,
        min_value=MONEY_ZERO,
        min_error_message=EXPENSE_NON_NEGATIVE_MESSAGE,
    ) or MONEY_ZERO
