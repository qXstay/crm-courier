import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.enums import OrderStatus, UserRole
from app.models.user import User
from app.services.client_service import (
    DuplicateClientError,
    create_client,
    find_possible_matches,
    format_russian_phone,
    get_client_detail,
    get_or_create_client_by_phone,
    normalize_client_name,
    search_clients,
    update_client,
)
from app.services.order_service import archive_order, create_order
from app.utils.auth import require_roles


class ClientServiceTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.manager = User(
            full_name="Менеджер",
            email="manager@example.com",
            password_hash="hash",
            role=UserRole.MANAGER,
            is_active=True,
        )
        self.admin = User(
            full_name="Админ",
            email="admin@example.com",
            password_hash="hash",
            role=UserRole.ADMIN,
            is_active=True,
        )
        self.courier = User(
            full_name="Курьер",
            email="courier@example.com",
            password_hash="hash",
            role=UserRole.COURIER,
            is_active=True,
        )
        self.session.add_all([self.manager, self.admin, self.courier])
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_get_or_create_client_reuses_matching_phone(self):
        created = get_or_create_client_by_phone(
            self.session,
            full_name="Иван Петров",
            phone="89001112233",
            created_by=self.manager,
        )
        reused = get_or_create_client_by_phone(
            self.session,
            full_name="Иван П.",
            phone="+7 (900) 111-22-33",
            created_by=self.manager,
        )

        self.assertEqual(reused.id, created.id)
        self.assertEqual(reused.full_name, "Иван Петров")
        self.assertEqual(reused.phone, "+7 900 111-22-33")

    def test_format_russian_phone_accepts_manager_input_and_paste_formats(self):
        phones = [
            "9959959595",
            "89959959595",
            "79959959595",
            "+79959959595",
            "+7 995 995-95-95",
        ]

        self.assertEqual(
            [format_russian_phone(phone) for phone in phones],
            ["+7 995 995-95-95"] * len(phones),
        )

    def test_format_russian_phone_rejects_bad_values(self):
        bad_phones = ["телефон", "995", "899599595950", "+375291234567"]

        for phone in bad_phones:
            with self.subTest(phone=phone):
                with self.assertRaisesRegex(
                    ValueError,
                    "Проверьте телефон клиента\\. Формат: \\+7 900 000-00-00\\.",
                ):
                    format_russian_phone(phone)

    def test_search_and_possible_matches_use_name_or_phone(self):
        get_or_create_client_by_phone(
            self.session,
            full_name="Мария Белова",
            phone="+7 900 555-44-33",
            created_by=self.manager,
        )

        by_name = search_clients(self.session, "белова")
        by_phone = search_clients(self.session, "555")
        matches = find_possible_matches(self.session, "Мария Б.", "44-33")

        self.assertEqual([client.full_name for client in by_name], ["Мария Белова"])
        self.assertEqual([client.full_name for client in by_phone], ["Мария Белова"])
        self.assertEqual([client.full_name for client in matches], ["Мария Белова"])

    def test_create_client_normalizes_phone_and_saves_notes(self):
        client = create_client(
            self.session,
            {
                "full_name": "  павел   тестов  ",
                "phone": "9959959595",
                "notes": "  Вход со двора  ",
            },
            self.manager,
        )

        self.assertEqual(client.full_name, "Павел Тестов")
        self.assertEqual(client.phone, "+7 995 995-95-95")
        self.assertEqual(client.notes, "Вход со двора")
        self.assertEqual(client.created_by_id, self.manager.id)

    def test_normalize_client_name_preserves_organization_prefixes(self):
        cases = {
            "иванов иван": "Иванов Иван",
            "ооо виктор": "ООО Виктор",
            "ООО Вектор": "ООО Вектор",
            "ип иванов": "ИП Иванов",
            "анна-мария соколова": "Анна-Мария Соколова",
        }

        for raw_name, expected in cases.items():
            with self.subTest(raw_name=raw_name):
                self.assertEqual(normalize_client_name(raw_name), expected)

    def test_create_client_blocks_duplicate_phone_with_existing_client(self):
        existing = create_client(
            self.session,
            {"full_name": "Иван Петров", "phone": "89951112233", "notes": ""},
            self.manager,
        )

        with self.assertRaises(DuplicateClientError) as raised:
            create_client(
                self.session,
                {"full_name": "Иван Новый", "phone": "+7 (995) 111-22-33", "notes": ""},
                self.manager,
            )

        self.assertEqual(str(raised.exception), "Клиент с таким телефоном уже есть.")
        self.assertEqual(raised.exception.client.id, existing.id)

    def test_update_client_does_not_change_order_snapshot(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )
        client = order.client

        updated = update_client(
            self.session,
            client.id,
            {
                "full_name": "Анна Новая",
                "phone": "9003334455",
                "notes": "Новый телефон",
            },
        )

        self.assertEqual(updated.full_name, "Анна Новая")
        self.assertEqual(updated.phone, "+7 900 333-44-55")
        self.session.refresh(order)
        self.assertEqual(order.client_name_snapshot, "Анна Соколова")
        self.assertEqual(order.client_phone_snapshot, "+7 900 222-33-44")

    def test_client_detail_returns_only_active_client_orders_and_summary(self):
        first = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
                "market_cube_cost": "100",
                "courier_pay": "200",
            },
            self.manager,
        )
        delivered = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 7",
                "status": OrderStatus.DELIVERED.value,
                "market_cube_cost": "50",
                "courier_pay": "50",
            },
            self.manager,
        )
        delivered.status = OrderStatus.DELIVERED
        archived = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 9",
                "market_cube_cost": "999",
                "courier_pay": "999",
            },
            self.manager,
        )
        archive_order(self.session, archived.id, self.admin)

        detail = get_client_detail(self.session, first.client_id)

        self.assertIsNotNone(detail)
        self.assertEqual([order.id for order in detail.orders], [delivered.id, first.id])
        self.assertEqual(detail.total_orders_count, 3)
        self.assertEqual(detail.active_orders_count, 1)
        self.assertEqual(detail.in_work_orders_count, 1)
        self.assertEqual(detail.at_courier_orders_count, 0)
        self.assertEqual(detail.delivered_orders_count, 1)
        self.assertEqual(detail.archived_orders_count, 1)
        self.assertEqual(detail.orders_total, first.delivery_cost + delivered.delivery_cost)

    def test_courier_is_forbidden_by_clients_role_helper(self):
        dependency = require_roles(UserRole.ADMIN, UserRole.MANAGER)

        with self.assertRaises(HTTPException) as raised:
            dependency(user=self.courier)

        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
