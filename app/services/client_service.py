from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.client import Client
from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.user import User

PHONE_INVALID_MESSAGE = "Проверьте телефон клиента. Формат: +7 900 000-00-00."
CLIENT_NAME_REQUIRED_MESSAGE = "Укажите ФИО клиента."
CLIENT_PHONE_REQUIRED_MESSAGE = "Укажите телефон клиента."
DUPLICATE_CLIENT_MESSAGE = "Клиент с таким телефоном уже есть."
ORG_PREFIXES = {
    "ИП",
    "ООО",
    "АО",
    "ОАО",
    "ЗАО",
    "ПАО",
    "НКО",
}


@dataclass(frozen=True)
class ClientDetail:
    client: Client
    orders: list[Order]
    total_orders_count: int
    active_orders_count: int
    in_work_orders_count: int
    at_courier_orders_count: int
    delivered_orders_count: int
    archived_orders_count: int
    orders_total: Decimal


class DuplicateClientError(ValueError):
    def __init__(self, client: Client):
        super().__init__(DUPLICATE_CLIENT_MESSAGE)
        self.client = client


def normalize_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return "".join(char for char in phone if char.isdigit())


def format_russian_phone(
    phone: str | None,
    *,
    error_message: str = PHONE_INVALID_MESSAGE,
) -> str:
    digits = _canonical_russian_phone_digits(phone)
    if digits is None:
        raise ValueError(error_message)

    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _canonical_russian_phone_digits(phone: str | None) -> str | None:
    raw_phone = str(phone or "").strip()
    if not raw_phone:
        return None

    allowed_chars = set("0123456789+() -")
    if any(char not in allowed_chars for char in raw_phone):
        return None
    if raw_phone.count("+") > 1 or ("+" in raw_phone and not raw_phone.startswith("+")):
        return None

    digits = normalize_phone(raw_phone)
    if len(digits) == 10:
        return "7" + digits
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return "7" + digits[1:]

    return None


def search_clients(db: Session, query: str | None = None) -> list[Client]:
    statement = (
        select(Client)
        .options(selectinload(Client.orders))
        .order_by(Client.created_at.desc(), Client.id.desc())
    )
    clients = list(db.scalars(statement).all())
    query = (query or "").strip()
    if not query:
        return clients

    normalized_query = normalize_phone(query)
    lowered_query = query.lower()
    clients = [
        client
        for client in clients
        if lowered_query in client.full_name.lower()
        or query in client.phone
        or (normalized_query and normalized_query in normalize_phone(client.phone))
    ]

    return clients


def get_client(db: Session, client_id: int) -> Client | None:
    return db.scalar(select(Client).where(Client.id == client_id))


def get_client_detail(db: Session, client_id: int) -> ClientDetail | None:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        return None

    orders = list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.courier))
            .where(Order.client_id == client.id, Order.is_archived.is_(False))
            .order_by(Order.delivery_date.desc().nullslast(), Order.created_at.desc(), Order.id.desc())
        ).all()
    )
    all_orders = list(
        db.scalars(
            select(Order)
            .where(Order.client_id == client.id)
            .order_by(Order.delivery_date.desc().nullslast(), Order.created_at.desc(), Order.id.desc())
        ).all()
    )
    return ClientDetail(
        client=client,
        orders=orders,
        total_orders_count=len(all_orders),
        active_orders_count=sum(1 for order in orders if order.status != OrderStatus.DELIVERED),
        in_work_orders_count=sum(1 for order in orders if order.status == OrderStatus.IN_WORK),
        at_courier_orders_count=sum(1 for order in orders if order.status == OrderStatus.AT_COURIER),
        delivered_orders_count=sum(1 for order in orders if order.status == OrderStatus.DELIVERED),
        archived_orders_count=sum(1 for order in all_orders if order.is_archived),
        orders_total=sum((order.delivery_cost or Decimal("0.00") for order in orders), Decimal("0.00")),
    )


def create_client(db: Session, form_data: dict[str, Any], user: User) -> Client:
    full_name = normalize_client_name(form_data.get("full_name"))
    phone = _phone_from_form(form_data)
    if not full_name:
        raise ValueError(CLIENT_NAME_REQUIRED_MESSAGE)

    existing_client = find_client_by_phone(db, phone)
    if existing_client is not None:
        raise DuplicateClientError(existing_client)

    client = Client(
        full_name=full_name,
        phone=phone,
        notes=_nullable_text(form_data.get("notes")),
        created_by_id=user.id,
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


def update_client(db: Session, client_id: int, form_data: dict[str, Any]) -> Client:
    client = get_client(db, client_id)
    if client is None:
        raise ValueError("Клиент не найден")

    full_name = normalize_client_name(form_data.get("full_name"))
    phone = _phone_from_form(form_data)
    if not full_name:
        raise ValueError(CLIENT_NAME_REQUIRED_MESSAGE)

    existing_client = find_client_by_phone(db, phone)
    if existing_client is not None and existing_client.id != client.id:
        raise DuplicateClientError(existing_client)

    client.full_name = full_name
    client.phone = phone
    client.notes = _nullable_text(form_data.get("notes"))
    db.commit()
    db.refresh(client)
    return client


def find_client_by_phone(db: Session, phone: str) -> Client | None:
    normalized_phone = _canonical_russian_phone_digits(phone)
    if not normalized_phone:
        return None

    clients = db.scalars(select(Client).order_by(Client.id)).all()
    for client in clients:
        if _canonical_russian_phone_digits(client.phone) == normalized_phone:
            return client

    return None


def get_or_create_client_by_phone(
    db: Session,
    *,
    full_name: str,
    phone: str,
    created_by: User | None = None,
) -> Client:
    formatted_phone = format_russian_phone(phone)
    existing_client = find_client_by_phone(db, phone)
    if existing_client is not None:
        return existing_client

    client = Client(
        full_name=normalize_client_name(full_name),
        phone=formatted_phone,
        created_by_id=created_by.id if created_by else None,
    )
    db.add(client)
    db.flush()
    return client


def normalize_client_name(value: Any) -> str:
    value = _clean_text(value)
    if not value:
        return ""

    return " ".join(_normalize_name_word(word) for word in value.split())


def normalized_name_key(value: Any) -> str:
    return normalize_client_name(value).casefold()


def find_possible_matches(
    db: Session,
    full_name: str | None,
    phone: str | None,
    limit: int = 5,
) -> list[Client]:
    name_query = (full_name or "").strip().lower()
    phone_query = normalize_phone(phone)

    clients = db.scalars(select(Client).order_by(Client.id.desc())).all()
    matches: list[Client] = []

    for client in clients:
        client_name = client.full_name.lower()
        client_phone = normalize_phone(client.phone)
        name_match = bool(name_query) and (
            name_query in client_name or client_name in name_query
        )
        phone_match = bool(phone_query) and (
            phone_query in client_phone or client_phone in phone_query
        )
        if name_match or phone_match:
            matches.append(client)
        if len(matches) >= limit:
            break

    return matches


def _phone_from_form(form_data: dict[str, Any]) -> str:
    raw_phone = _clean_text(form_data.get("phone"))
    if not raw_phone or normalize_phone(raw_phone) in {"7", "8"}:
        raise ValueError(CLIENT_PHONE_REQUIRED_MESSAGE)
    return format_russian_phone(raw_phone)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_name_word(word: str) -> str:
    if not word:
        return word

    upper_word = word.upper()
    if upper_word in ORG_PREFIXES:
        return upper_word

    return "-".join(_capitalize_name_part(part) for part in word.split("-"))


def _capitalize_name_part(part: str) -> str:
    if not part:
        return part

    upper_part = part.upper()
    if upper_part in ORG_PREFIXES:
        return upper_part

    return part[:1].upper() + part[1:].lower()


def _nullable_text(value: Any) -> str | None:
    value = _clean_text(value)
    return value or None
