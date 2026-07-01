import os
from datetime import date, timedelta
from decimal import Decimal
from threading import Lock

from sqlalchemy import delete, select

from app.database import Base, SessionLocal
from app.models.client import Client
from app.models.courier_cash_handover import CourierCashHandover
from app.models.enums import OrderStatus, PaymentMethod, UserRole
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.client_service import get_or_create_client_by_phone
from app.services.order_service import archive_order, create_order
from app.services.payment_service import create_or_update_payment
from app.utils.security import hash_password


_reset_lock = Lock()


DEFAULT_DEMO_USERS = [
    {
        "env_password": "DEMO_ADMIN_PASSWORD",
        "email": "admin@courier.local",
        "password": "admin123",
        "full_name": "Администратор",
        "phone": "+7 900 100-00-01",
        "role": UserRole.ADMIN,
    },
    {
        "env_password": "DEMO_MANAGER_PASSWORD",
        "email": "manager@courier.local",
        "password": "manager123",
        "full_name": "Мария Менеджер",
        "phone": "+7 900 100-00-02",
        "role": UserRole.MANAGER,
    },
    {
        "env_password": "DEMO_COURIER_PASSWORD",
        "email": "courier@courier.local",
        "password": "courier123",
        "full_name": "Игорь Курьер",
        "phone": "+7 900 100-00-03",
        "role": UserRole.COURIER,
    },
]


DEMO_CLIENTS = [
    ("Наталья Орлова", "+7 900 111-22-33"),
    ("Денис Павлов", "+7 900 222-33-44"),
    ("ООО Вектор", "+7 900 333-44-55"),
    ("Кофейня Север", "+7 900 444-55-66"),
]


DEMO_ORDERS = [
    {
        "legacy_order_code": "BP-0001",
        "client_name": "Наталья Орлова",
        "client_phone": "+7 900 111-22-33",
        "address": "Москва, Тверская улица, 12",
        "delivery_date_offset": 0,
        "general_note": "Документы к договору, доставка в офис.",
        "cargo_number": "DOC-24115",
        "cargo_phone": "+7 495 100-10-10",
        "client_note": "Позвонить за час.",
        "staff_note": "Проверить оплату и закрывающие документы.",
        "weight": "3.2",
        "volume": "0.3",
        "places_count": "1",
        "base_delivery_cost": "850",
        "market_cube_cost": "520",
        "market_loader_cost": "350",
        "market_storage_cost": "0",
        "market_kara_cost": "90",
        "market_other_cost": "40",
        "courier_pay": "0",
        "delivery_cost": "1850",
        "courier": False,
        "payment_amount": "1850",
        "payment_method": PaymentMethod.CASH.value,
        "archived": False,
    },
    {
        "legacy_order_code": "BP-0002",
        "client_name": "ООО Вектор",
        "client_phone": "+7 900 333-44-55",
        "address": "Химки, улица Панфилова, 8",
        "delivery_date_offset": 0,
        "general_note": "Доставка образцов до офиса клиента.",
        "cargo_number": "BOX-24116",
        "cargo_phone": "",
        "client_note": "",
        "staff_note": "Курьер назначен, клиент ждёт доставку сегодня.",
        "weight": "18.0",
        "volume": "1.2",
        "places_count": "3",
        "base_delivery_cost": "1200",
        "market_cube_cost": "620",
        "market_loader_cost": "450",
        "market_storage_cost": "0",
        "market_kara_cost": "120",
        "market_other_cost": "60",
        "courier_pay": "500",
        "delivery_cost": "2450",
        "courier": True,
        "payment_amount": "2450",
        "payment_method": PaymentMethod.TRANSFER.value,
        "archived": False,
    },
    {
        "legacy_order_code": "BP-0003",
        "client_name": "Денис Павлов",
        "client_phone": "+7 900 222-33-44",
        "address": "Москва, Ленинградский проспект, 45",
        "delivery_date_offset": 0,
        "general_note": "Доставка личных вещей до подъезда.",
        "cargo_number": "",
        "cargo_phone": "",
        "client_note": "Клиент оплатит после доставки.",
        "staff_note": "Оплату нужно получить после доставки.",
        "weight": "9.5",
        "volume": "0.7",
        "places_count": "2",
        "base_delivery_cost": "1100",
        "market_cube_cost": "380",
        "market_loader_cost": "0",
        "market_storage_cost": "120",
        "market_kara_cost": "0",
        "market_other_cost": "0",
        "courier_pay": "450",
        "delivery_cost": "1600",
        "courier": True,
        "payment_amount": None,
        "payment_method": None,
        "archived": False,
    },
    {
        "legacy_order_code": "BP-0004",
        "client_name": "Кофейня Север",
        "client_phone": "+7 900 444-55-66",
        "address": "Москва, Большая Дмитровка, 9",
        "delivery_date_offset": 0,
        "general_note": "Доставка расходников для кофейни.",
        "cargo_number": "COF-24117",
        "cargo_phone": "",
        "client_note": "",
        "staff_note": "Оплата наличными, сверить кассу после маршрута.",
        "weight": "6.0",
        "volume": "0.5",
        "places_count": "2",
        "base_delivery_cost": "900",
        "market_cube_cost": "240",
        "market_loader_cost": "180",
        "market_storage_cost": "0",
        "market_kara_cost": "30",
        "market_other_cost": "50",
        "courier_pay": "350",
        "delivery_cost": "1400",
        "courier": False,
        "payment_amount": "1400",
        "payment_method": PaymentMethod.CASH.value,
        "archived": False,
    },
    {
        "legacy_order_code": "BP-0005",
        "client_name": "ООО Вектор",
        "client_phone": "+7 900 333-44-55",
        "address": "Москва, Проспект Мира, 101",
        "delivery_date_offset": -1,
        "general_note": "Завершённая заявка для проверки архива.",
        "cargo_number": "ARC-24114",
        "cargo_phone": "",
        "client_note": "",
        "staff_note": "Архивная заявка.",
        "weight": "11.0",
        "volume": "0.9",
        "places_count": "2",
        "base_delivery_cost": "800",
        "market_cube_cost": "400",
        "market_loader_cost": "250",
        "market_storage_cost": "0",
        "market_kara_cost": "20",
        "market_other_cost": "30",
        "courier_pay": "300",
        "delivery_cost": "1500",
        "courier": False,
        "payment_amount": None,
        "payment_method": None,
        "archived": True,
    },
]


def seed_demo() -> None:
    ensure_sqlite_schema()
    with SessionLocal() as db:
        users = {user.email: user for user in db.scalars(select(User)).all()}
        created_users = []

        for user_data in DEFAULT_DEMO_USERS:
            if user_data["email"] in users:
                continue

            password = os.getenv(user_data["env_password"], user_data["password"])
            user = User(
                email=user_data["email"],
                full_name=user_data["full_name"],
                phone=user_data["phone"],
                role=user_data["role"],
                password_hash=hash_password(password),
                is_active=True,
            )
            db.add(user)
            db.flush()
            users[user.email] = user
            created_users.append(user.email)

        manager = users["manager@courier.local"]
        courier = users["courier@courier.local"]

        clients_before = len(db.scalars(select(Client)).all())
        clients = {
            phone: _ensure_demo_client(db, full_name, phone, manager)
            for full_name, phone in DEMO_CLIENTS
        }
        db.commit()

        today = date.today()
        for spec in DEMO_ORDERS:
            order = _ensure_demo_order(
                db,
                spec,
                clients,
                manager,
                users["admin@courier.local"],
                courier,
                today,
            )
            _sync_demo_payment(db, order, spec, manager)

        clients_after = len(db.scalars(select(Client)).all())
        print("Demo seed выполнен")
        print(f"Пользователи созданы: {len(created_users)}")
        print(f"Клиенты добавлены: {clients_after - clients_before}")
        print("Локальные demo-входы:")
        for user_data in DEFAULT_DEMO_USERS:
            password = os.getenv(user_data["env_password"], user_data["password"])
            print(f"- {user_data['email']} / {password}")


def ensure_sqlite_schema() -> None:
    with SessionLocal() as db:
        bind = db.get_bind()
        if bind.dialect.name == "sqlite":
            Base.metadata.create_all(bind=bind)


def demo_users_exist() -> bool:
    with SessionLocal() as db:
        return all(
            db.scalar(select(User).where(User.email == user_data["email"])) is not None
            for user_data in DEFAULT_DEMO_USERS
        )


def _ensure_demo_client(db, full_name: str, phone: str, manager: User) -> Client:
    client = get_or_create_client_by_phone(
        db,
        full_name=full_name,
        phone=phone,
        created_by=manager,
    )
    client.full_name = full_name
    client.phone = phone
    return client


def _ensure_demo_order(
    db,
    spec: dict,
    clients: dict[str, Client],
    manager: User,
    admin: User,
    courier: User,
    today: date,
) -> Order:
    order = _find_demo_order(db, spec)
    if order is None:
        order = create_order(db, _order_form(spec, courier, today), manager)

    client = clients[spec["client_phone"]]
    order.client_id = client.id
    order.client_name_snapshot = spec["client_name"]
    order.client_phone_snapshot = spec["client_phone"]
    order.address = spec["address"]
    order.delivery_date = today + timedelta(days=spec["delivery_date_offset"])
    order.general_note = spec["general_note"]
    order.cargo_number = _nullable_text(spec["cargo_number"])
    order.cargo_phone = _nullable_text(spec["cargo_phone"])
    order.client_note = _nullable_text(spec["client_note"])
    order.staff_note = _nullable_text(spec["staff_note"])
    order.weight = _nullable_decimal(spec["weight"])
    order.volume = _nullable_decimal(spec["volume"])
    order.places_count = int(spec["places_count"])
    order.courier_id = courier.id if spec["courier"] else None
    order.status = OrderStatus.AT_COURIER if spec["courier"] else OrderStatus.IN_WORK
    order.base_delivery_cost = _money(spec["base_delivery_cost"])
    order.market_cube_cost = _money(spec["market_cube_cost"])
    order.market_loader_cost = _money(spec["market_loader_cost"])
    order.market_storage_cost = _money(spec["market_storage_cost"])
    order.market_kara_cost = _money(spec["market_kara_cost"])
    order.market_other_cost = _money(spec["market_other_cost"])
    order.courier_pay = _money(spec["courier_pay"])
    order.delivery_cost = _money(spec["delivery_cost"])

    if spec["archived"]:
        db.commit()
        if not order.is_archived:
            order = archive_order(db, order.id, admin)
    else:
        order.is_archived = False
        order.archived_at = None
        order.archived_by_id = None
        db.commit()
        db.refresh(order)

    return order


def _find_demo_order(db, spec: dict) -> Order | None:
    order = db.scalar(
        select(Order)
        .where(Order.address == spec["address"])
        .order_by(Order.id)
        .limit(1)
    )
    if order is not None:
        return order

    legacy_order_code = spec.get("legacy_order_code")
    if not legacy_order_code:
        return None
    return db.scalar(
        select(Order)
        .where(Order.order_code == legacy_order_code)
        .order_by(Order.id)
        .limit(1)
    )


def _order_form(spec: dict, courier: User, today: date) -> dict[str, str]:
    return {
        "client_name": spec["client_name"],
        "client_phone": spec["client_phone"],
        "address": spec["address"],
        "delivery_date": (today + timedelta(days=spec["delivery_date_offset"])).isoformat(),
        "general_note": spec["general_note"],
        "cargo_number": spec["cargo_number"],
        "cargo_phone": spec["cargo_phone"],
        "client_note": spec["client_note"],
        "staff_note": spec["staff_note"],
        "weight": spec["weight"],
        "volume": spec["volume"],
        "places_count": spec["places_count"],
        "courier_id": str(courier.id) if spec["courier"] else "",
        "base_delivery_cost": spec["base_delivery_cost"],
        "market_cube_cost": spec["market_cube_cost"],
        "market_loader_cost": spec["market_loader_cost"],
        "market_storage_cost": spec["market_storage_cost"],
        "market_kara_cost": spec["market_kara_cost"],
        "market_other_cost": spec["market_other_cost"],
        "courier_pay": spec["courier_pay"],
    }


def _sync_demo_payment(db, order: Order, spec: dict, manager: User) -> None:
    for payment in db.scalars(select(Payment).where(Payment.order_id == order.id)).all():
        db.delete(payment)
    db.flush()

    if spec["archived"] or spec["payment_amount"] is None:
        db.commit()
        return

    create_or_update_payment(
        db,
        order.id,
        {
            "amount": spec["payment_amount"],
            "method": spec["payment_method"],
        },
        manager,
    )
    db.commit()


def _nullable_text(value: str | None) -> str | None:
    return value or None


def _nullable_decimal(value: str | None) -> Decimal | None:
    return _money(value) if value else None


def _money(value: str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def reset_demo_data() -> None:
    """Сброс демо-данных к исходному состоянию. Вызывать только при DEMO_MODE.

    Удаляем изменяемые demo-данные (логи, оплаты, сдачи курьеров, заявки,
    клиентов) и пересоздаём canonical seed через seed_demo(). Пользователей
    не трогаем: demo-пользователи нужны для активной сессии посетителя.
    Порядок удаления уважает внешние ключи (child-first), каскадов в схеме нет.
    """
    with _reset_lock:
        with SessionLocal() as db:
            db.execute(delete(OrderChangeLog))
            db.execute(delete(Payment))
            db.execute(delete(CourierCashHandover))
            db.execute(delete(Order))
            db.execute(delete(Client))
            db.commit()
        seed_demo()


if __name__ == "__main__":
    seed_demo()
