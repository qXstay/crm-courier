from datetime import date, datetime
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.client import Client
from app.models.enums import OrderStatus, UserRole
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.user import User
from app.services.order_service import (
    DuplicateCargoNumberError,
    OrderListFilters,
    archive_order,
    archive_orders_bulk,
    create_order,
    list_active_orders,
    mark_order_at_courier_by_courier,
    restore_order,
    update_order_status,
    update_order,
)
from app.templating import order_number
from app.utils.dates import CRM_TIMEZONE, default_delivery_date


class OrderServiceTest(unittest.TestCase):
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

    def test_create_order_reuses_client_by_phone_and_calculates_status_and_cost(self):
        first = create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Тверская, 1",
                "base_delivery_cost": "1000",
                "market_cube_cost": "100.10",
                "market_loader_cost": "200",
                "market_storage_cost": "",
                "market_kara_cost": "25",
                "market_other_cost": "50.40",
                "courier_pay": "300",
            },
            self.manager,
        )
        second = create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": "+7 (900) 111-22-33",
                "address": "Москва, Арбат, 2",
                "courier_id": str(self.courier.id),
            },
            self.manager,
        )

        self.assertEqual(first.order_code, "BP-0001")
        self.assertEqual(second.order_code, "BP-0002")
        self.assertEqual(second.client_id, first.client_id)
        self.assertEqual(second.status, OrderStatus.AT_COURIER)
        self.assertEqual(first.base_delivery_cost, Decimal("1000.00"))
        self.assertEqual(first.delivery_cost, Decimal("1375.50"))
        self.assertEqual(first.market_kara_cost, Decimal("25.00"))
        self.assertEqual(first.courier_pay, Decimal("300.00"))
        self.assertNotEqual(first.delivery_cost, Decimal("1675.50"))
        self.assertEqual(first.client_name_snapshot, "Иван Петров")
        self.assertEqual(first.client_phone_snapshot, "+7 900 111-22-33")

        logs = self.session.scalars(select(OrderChangeLog)).all()
        self.assertEqual([log.action for log in logs], ["создание", "создание"])

    def test_default_delivery_date_uses_crm_time_cutoff(self):
        before_cutoff = datetime(2026, 6, 9, 15, 0, tzinfo=CRM_TIMEZONE)
        after_cutoff = datetime(2026, 6, 9, 15, 1, tzinfo=CRM_TIMEZONE)

        self.assertEqual(default_delivery_date(before_cutoff), date(2026, 6, 9))
        self.assertEqual(default_delivery_date(after_cutoff), date(2026, 6, 10))

    def test_create_order_uses_default_delivery_date_when_empty(self):
        with patch(
            "app.services.order_service.default_delivery_date",
            return_value=date(2026, 6, 10),
        ):
            order = create_order(
                self.session,
                {
                    "client_name": "Авто Дата",
                    "client_phone": "+7 900 123-45-67",
                    "address": "Москва, Ленина, 1",
                },
                self.manager,
            )

        self.assertEqual(order.delivery_date, date(2026, 6, 10))

    def test_create_order_can_use_selected_client_from_client_card(self):
        client = Client(
            full_name="ООО Вектор",
            phone="+7 900 444-55-66",
            notes="Звонить заранее",
            created_by_id=self.manager.id,
        )
        self.session.add(client)
        self.session.commit()

        order = create_order(
            self.session,
            {
                "client_id": str(client.id),
                "client_name": "другой клиент",
                "client_phone": "+7 900 000-00-00",
                "address": "Химки, улица Панфилова, 8",
            },
            self.manager,
        )

        self.assertEqual(order.client_id, client.id)
        self.assertEqual(order.client_name_snapshot, "ООО Вектор")
        self.assertEqual(order.client_phone_snapshot, "+7 900 444-55-66")

    def test_create_order_links_by_exact_name_when_phone_is_new(self):
        existing = Client(
            full_name="Иванов Иван",
            phone="+7 900 111-22-33",
            notes="Постоянный клиент",
            created_by_id=self.manager.id,
        )
        self.session.add(existing)
        self.session.commit()

        order = create_order(
            self.session,
            {
                "client_name": "иванов иван",
                "client_phone": "+7 900 999-88-77",
                "address": "Москва, Тверская, 1",
            },
            self.manager,
        )

        self.assertEqual(order.client_id, existing.id)
        self.assertEqual(order.client_name_snapshot, "Иванов Иван")
        self.assertEqual(order.client_phone_snapshot, "+7 900 999-88-77")

    def test_create_order_does_not_link_ambiguous_name_silently(self):
        self.session.add_all(
            [
                Client(
                    full_name="Анна Соколова",
                    phone="+7 900 111-22-33",
                    created_by_id=self.manager.id,
                ),
                Client(
                    full_name="анна соколова",
                    phone="+7 900 222-33-44",
                    created_by_id=self.manager.id,
                ),
            ]
        )
        self.session.commit()

        with self.assertRaisesRegex(ValueError, "Нашли несколько клиентов с таким ФИО"):
            create_order(
                self.session,
                {
                    "client_name": "АННА СОКОЛОВА",
                    "client_phone": "+7 900 333-44-55",
                    "address": "Москва, Ленина, 5",
                },
                self.manager,
            )

    def test_create_order_normalizes_russian_phone_and_address(self):
        order = create_order(
            self.session,
            {
                "client_name": "Павел Тестов",
                "client_phone": "89995554433",
                "address": "  москва,   улица Ленина,   22  ",
            },
            self.manager,
        )

        self.assertEqual(order.client_phone_snapshot, "+7 999 555-44-33")
        self.assertEqual(order.client.phone, "+7 999 555-44-33")
        self.assertEqual(order.address, "Москва, улица Ленина, 22")
        self.assertEqual(order.status, OrderStatus.IN_WORK)

    def test_create_order_accepts_decimal_commas_and_short_phone_input(self):
        order = create_order(
            self.session,
            {
                "client_name": "Павел Тестов",
                "client_phone": "9959959595",
                "address": "Москва, улица Ленина, 22",
                "weight": "0,5",
                "volume": ".5",
                "places_count": "2",
                "base_delivery_cost": "100",
                "market_cube_cost": "10,50",
                "market_loader_cost": "20.25",
                "market_storage_cost": "",
                "market_kara_cost": "5",
                "market_other_cost": "0",
                "courier_pay": ".5",
            },
            self.manager,
        )

        self.assertEqual(order.client_phone_snapshot, "+7 995 995-95-95")
        self.assertEqual(order.weight, Decimal("0.500"))
        self.assertEqual(order.volume, Decimal("0.500"))
        self.assertEqual(order.places_count, 2)
        self.assertEqual(order.delivery_cost, Decimal("135.75"))
        self.assertEqual(order.courier_pay, Decimal("0.50"))

    def test_create_order_normalizes_optional_cargo_phone(self):
        cases = [
            ("", None),
            ("9959959595", "+7 995 995-95-95"),
            ("89959959595", "+7 995 995-95-95"),
            ("79959959595", "+7 995 995-95-95"),
            ("+79959959595", "+7 995 995-95-95"),
            ("+7 995 995-95-95", "+7 995 995-95-95"),
        ]

        for cargo_phone, expected in cases:
            with self.subTest(cargo_phone=cargo_phone):
                order = create_order(
                    self.session,
                    {
                        "client_name": "Павел Тестов",
                        "client_phone": "+7 995 995-95-95",
                        "address": "Москва, улица Ленина, 22",
                        "cargo_phone": cargo_phone,
                    },
                    self.manager,
                )

                self.assertEqual(order.cargo_phone, expected)

    def test_create_order_rejects_invalid_cargo_phone_with_specific_message(self):
        bad_phones = ["телефон", "995", "899599595950"]

        for cargo_phone in bad_phones:
            with self.subTest(cargo_phone=cargo_phone):
                with self.assertRaisesRegex(
                    ValueError,
                    "Проверьте телефон карго\\. Формат: \\+7 900 000-00-00\\.",
                ):
                    create_order(
                        self.session,
                        {
                            "client_name": "Павел Тестов",
                            "client_phone": "+7 995 995-95-95",
                            "address": "Москва, улица Ленина, 22",
                            "cargo_phone": cargo_phone,
                        },
                        self.manager,
                    )

    def test_create_order_rejects_active_duplicate_cargo_number(self):
        first = create_order(
            self.session,
            {
                "client_name": "Первый Клиент",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Ленина, 1",
                "cargo_number": "334e65k33ep5h",
            },
            self.manager,
        )

        with self.assertRaises(DuplicateCargoNumberError) as raised:
            create_order(
                self.session,
                {
                    "client_name": "Второй Клиент",
                    "client_phone": "+7 900 222-33-44",
                    "address": "Москва, Ленина, 2",
                    "cargo_number": " 334E65K33EP5H ",
                },
                self.manager,
            )

        self.assertEqual(raised.exception.order.id, first.id)
        self.assertIn(first.order_code, str(raised.exception))

    def test_create_order_allows_archived_duplicate_cargo_number(self):
        archived = create_order(
            self.session,
            {
                "client_name": "Первый Клиент",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Ленина, 1",
                "cargo_number": "ARCH-100",
            },
            self.manager,
        )
        archive_order(self.session, archived.id, self.manager)

        active = create_order(
            self.session,
            {
                "client_name": "Второй Клиент",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 2",
                "cargo_number": "ARCH-100",
            },
            self.manager,
        )

        self.assertFalse(active.is_archived)
        self.assertEqual(active.cargo_number, "ARCH-100")

    def test_update_order_allows_same_cargo_number_on_current_order(self):
        order = create_order(
            self.session,
            {
                "client_name": "Первый Клиент",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Ленина, 1",
                "cargo_number": "SELF-100",
            },
            self.manager,
        )

        updated = update_order(
            self.session,
            order.id,
            {
                "client_name": "Первый Клиент",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Ленина, 5",
                "cargo_number": "SELF-100",
            },
            self.manager,
        )

        self.assertEqual(updated.cargo_number, "SELF-100")
        self.assertEqual(updated.address, "Москва, Ленина, 5")

    def test_update_order_rejects_other_active_duplicate_cargo_number(self):
        first = create_order(
            self.session,
            {
                "client_name": "Первый Клиент",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Ленина, 1",
                "cargo_number": "DUP-200",
            },
            self.manager,
        )
        second = create_order(
            self.session,
            {
                "client_name": "Второй Клиент",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 2",
                "cargo_number": "OTHER-200",
            },
            self.manager,
        )

        with self.assertRaises(DuplicateCargoNumberError) as raised:
            update_order(
                self.session,
                second.id,
                {
                    "client_name": "Второй Клиент",
                    "client_phone": "+7 900 222-33-44",
                    "address": "Москва, Ленина, 2",
                    "cargo_number": "DUP-200",
                },
                self.manager,
            )

        self.assertEqual(raised.exception.order.id, first.id)

    def test_order_number_formats_decimal_values_for_display(self):
        self.assertEqual(order_number(Decimal("0.500")), "0,5")
        self.assertEqual(order_number(Decimal("12.000")), "12")
        self.assertEqual(order_number(None), "Не указан")

    def test_create_order_rejects_bad_phone_and_missing_required_fields(self):
        with self.assertRaisesRegex(
            ValueError,
            "Проверьте телефон клиента\\. Формат: \\+7 900 000-00-00\\.",
        ):
            create_order(
                self.session,
                {
                    "client_name": "Павел Тестов",
                    "client_phone": "телефон",
                    "address": "Москва, улица Ленина, 22",
                },
                self.manager,
            )

        with self.assertRaisesRegex(
            ValueError,
            "Проверьте телефон клиента\\. Формат: \\+7 900 000-00-00\\.",
        ):
            create_order(
                self.session,
                {
                    "client_name": "Павел Тестов",
                    "client_phone": "+375291234567",
                    "address": "Москва, улица Ленина, 22",
                },
                self.manager,
            )

        with self.assertRaisesRegex(
            ValueError,
            "Укажите ФИО клиента\\.\nУкажите телефон клиента\\.\nУкажите адрес доставки\\.",
        ):
            create_order(
                self.session,
                {
                    "client_name": "",
                    "client_phone": "",
                    "address": "",
                },
                self.manager,
            )

    def test_create_order_rejects_invalid_numbers_with_specific_messages(self):
        base_form = {
            "client_name": "Павел Тестов",
            "client_phone": "+7 995 995-95-95",
            "address": "Москва, улица Ленина, 22",
        }

        cases = [
            ({"weight": "1кг"}, "Вес должен быть числом\\."),
            ({"volume": "a"}, "Объём должен быть числом\\."),
            ({"places_count": "1.5"}, "Кол-во мест должно быть целым числом\\."),
            ({"market_cube_cost": "-1"}, "Расходы не могут быть отрицательными\\."),
            ({"market_kara_cost": "-1"}, "Расходы не могут быть отрицательными\\."),
            ({"courier_pay": "a"}, "Расходы должны быть числом\\."),
        ]

        for patch, message in cases:
            with self.subTest(patch=patch):
                with self.assertRaisesRegex(ValueError, message):
                    create_order(self.session, {**base_form, **patch}, self.manager)

    def test_update_order_can_set_delivered_and_writes_log(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        updated = update_order(
            self.session,
            order.id,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 7",
                "status": OrderStatus.DELIVERED.value,
                "courier_id": "",
                "base_delivery_cost": "100",
                "market_cube_cost": "10",
                "market_loader_cost": "20",
                "market_storage_cost": "30",
                "market_kara_cost": "50",
                "market_other_cost": "40",
                "courier_pay": "50",
            },
            self.manager,
        )

        self.assertEqual(updated.address, "Москва, Ленина, 7")
        self.assertEqual(updated.status, OrderStatus.DELIVERED)
        self.assertEqual(updated.delivery_cost, Decimal("250.00"))
        self.assertEqual(updated.market_kara_cost, Decimal("50.00"))
        self.assertEqual(updated.courier_pay, Decimal("50.00"))

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "редактирование"])
        self.assertIn("Москва, Ленина, 7", logs[-1].new_value)
        self.assertIn('"market_kara_cost": "50.00"', logs[-1].new_value)

    def test_manager_can_update_order_status_from_list_and_writes_log(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        updated = update_order_status(
            self.session,
            order.id,
            OrderStatus.DELIVERED.value,
            self.manager,
        )

        self.assertEqual(updated.status, OrderStatus.DELIVERED)
        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "статус"])
        self.assertIn('"status": "delivered"', logs[-1].new_value)

    def test_status_at_courier_requires_courier(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        with self.assertRaisesRegex(ValueError, "Выберите курьера"):
            update_order_status(
                self.session,
                order.id,
                OrderStatus.AT_COURIER.value,
                self.manager,
            )

        self.session.refresh(order)
        self.assertEqual(order.status, OrderStatus.IN_WORK)
        self.assertIsNone(order.courier_id)

        with self.assertRaisesRegex(ValueError, "Выберите курьера"):
            create_order(
                self.session,
                {
                    "client_name": "Новая Заявка",
                    "client_phone": "+7 900 444-55-66",
                    "address": "Москва, Ленина, 7",
                    "status": OrderStatus.AT_COURIER.value,
                    "courier_id": "",
                },
                self.manager,
            )

    def test_status_at_courier_assigns_courier_and_writes_log(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        updated = update_order_status(
            self.session,
            order.id,
            OrderStatus.AT_COURIER.value,
            self.manager,
            courier_id_value=str(self.courier.id),
        )

        self.assertEqual(updated.status, OrderStatus.AT_COURIER)
        self.assertEqual(updated.courier_id, self.courier.id)
        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "статус"])
        self.assertIn('"courier_id": %s' % self.courier.id, logs[-1].new_value)

    def test_status_in_work_clears_courier_assignment(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
                "courier_id": str(self.courier.id),
            },
            self.manager,
        )

        updated = update_order_status(
            self.session,
            order.id,
            OrderStatus.IN_WORK.value,
            self.manager,
        )

        self.assertEqual(updated.status, OrderStatus.IN_WORK)
        self.assertIsNone(updated.courier_id)

    def test_courier_cannot_update_order_status_from_list(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        with self.assertRaises(PermissionError):
            update_order_status(
                self.session,
                order.id,
                OrderStatus.DELIVERED.value,
                self.courier,
            )

    def test_courier_can_mark_own_in_work_order_at_courier(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )
        order.courier_id = self.courier.id
        self.session.commit()

        updated = mark_order_at_courier_by_courier(self.session, order.id, self.courier)

        self.assertEqual(updated.status, OrderStatus.AT_COURIER)
        logs = self.session.scalars(select(OrderChangeLog).order_by(OrderChangeLog.id)).all()
        self.assertEqual([log.action for log in logs], ["создание", "у курьера"])

    def test_admin_can_archive_order_and_writes_log(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        archived = archive_order(self.session, order.id, self.admin)

        self.assertTrue(archived.is_archived)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(archived.archived_by_id, self.admin.id)

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "архив"])

    def test_archived_order_is_hidden_from_active_list(self):
        active = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )
        archived = create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Тверская, 1",
            },
            self.manager,
        )
        archive_order(self.session, archived.id, self.admin)

        self.assertEqual([order.id for order in list_active_orders(self.session)], [active.id])

    def test_list_active_orders_searches_by_code_client_phone_cargo_address_and_courier_without_archive(self):
        ivan = create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Тверская, 1",
            },
            self.manager,
        )
        anna = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )
        vector = create_order(
            self.session,
            {
                "client_name": "ООО Вектор",
                "client_phone": "+7 900 444-55-66",
                "address": "Химки, улица Панфилова, 8",
                "cargo_number": "QA74-611155-MAIN",
                "courier_id": str(self.courier.id),
            },
            self.manager,
        )
        archived_ivan = create_order(
            self.session,
            {
                "client_name": "Иван Архивный",
                "client_phone": "+7 900 333-44-55",
                "address": "Москва, Арбат, 2",
                "cargo_number": "ARCH-CARGO-100",
            },
            self.manager,
        )
        archive_order(self.session, archived_ivan.id, self.admin)

        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="BP-0002"),
                )
            ],
            [anna.id],
        )
        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="Иван"),
                )
            ],
            [ivan.id],
        )
        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="вектор"),
                )
            ],
            [vector.id],
        )
        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="9001112233"),
                )
            ],
            [ivan.id],
        )
        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="QA74-611155-MAIN"),
                )
            ],
            [vector.id],
        )
        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="611155-main"),
                )
            ],
            [vector.id],
        )
        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="панфилова"),
                )
            ],
            [vector.id],
        )
        self.assertEqual(
            [
                order.id
                for order in list_active_orders(
                    self.session,
                    filters=OrderListFilters(search="Курьер"),
                )
            ],
            [vector.id],
        )
        self.assertEqual(
            list_active_orders(
                self.session,
                filters=OrderListFilters(search="ARCH-CARGO-100"),
            ),
            [],
        )

    def test_list_active_orders_filters_by_delivery_date_status_courier_and_search(self):
        first = create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": "+7 900 111-22-33",
                "address": "Москва, Тверская, 1",
                "delivery_date": "2026-06-01",
            },
            self.manager,
        )
        second = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
                "delivery_date": "2026-06-02",
                "courier_id": str(self.courier.id),
            },
            self.manager,
        )
        delivered = create_order(
            self.session,
            {
                "client_name": "ООО Вектор",
                "client_phone": "+7 900 444-55-66",
                "address": "Химки, улица Панфилова, 8",
                "delivery_date": "2026-06-03",
            },
            self.manager,
        )
        update_order_status(self.session, delivered.id, OrderStatus.DELIVERED.value, self.manager)
        archived = create_order(
            self.session,
            {
                "client_name": "Архивный клиент",
                "client_phone": "+7 900 555-66-77",
                "address": "Москва, Арбат, 2",
                "delivery_date": "2026-06-03",
                "courier_id": str(self.courier.id),
            },
            self.manager,
        )
        archive_order(self.session, archived.id, self.admin)

        def ids(filters: OrderListFilters) -> set[int]:
            return {order.id for order in list_active_orders(self.session, filters=filters)}

        self.assertEqual(ids(OrderListFilters(delivery_date_from=date(2026, 6, 2))), {second.id, delivered.id})
        self.assertEqual(ids(OrderListFilters(delivery_date_to=date(2026, 6, 2))), {first.id, second.id})
        self.assertEqual(
            ids(OrderListFilters(delivery_date_from=date(2026, 6, 2), delivery_date_to=date(2026, 6, 2))),
            {second.id},
        )
        self.assertEqual(ids(OrderListFilters(status=OrderStatus.DELIVERED)), {delivered.id})
        self.assertEqual(ids(OrderListFilters(courier_id=self.courier.id)), {second.id})
        self.assertEqual(
            ids(
                OrderListFilters(
                    search="Анна",
                    delivery_date_from=date(2026, 6, 2),
                    status=OrderStatus.AT_COURIER,
                    courier_id=self.courier.id,
                )
            ),
            {second.id},
        )

    def test_manager_can_archive_order_and_writes_log(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        archived = archive_order(self.session, order.id, self.manager)

        self.assertTrue(archived.is_archived)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(archived.archived_by_id, self.manager.id)

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "архив"])

    def test_courier_cannot_archive_order(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )

        with self.assertRaises(PermissionError):
            archive_order(self.session, order.id, self.courier)

        self.session.refresh(order)
        self.assertFalse(order.is_archived)

    def test_admin_can_restore_order_and_writes_log(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )
        archived = archive_order(self.session, order.id, self.admin)

        restored = restore_order(self.session, archived.id, self.admin)

        self.assertFalse(restored.is_archived)
        self.assertIsNone(restored.archived_at)
        self.assertIsNone(restored.archived_by_id)

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual(
            [log.action for log in logs],
            ["создание", "архив", "восстановление"],
        )

    def test_restore_order_allows_empty_cargo_number(self):
        archived = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
                "cargo_number": "",
            },
            self.manager,
        )
        archive_order(self.session, archived.id, self.admin)
        create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": "+7 900 333-44-55",
                "address": "Москва, Арбат, 2",
                "cargo_number": "",
            },
            self.manager,
        )

        restored = restore_order(self.session, archived.id, self.admin)

        self.assertFalse(restored.is_archived)

    def test_restore_order_rejects_active_duplicate_cargo_number(self):
        archived = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
                "cargo_number": "RESTORE-100",
            },
            self.manager,
        )
        archive_order(self.session, archived.id, self.admin)
        active = create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": "+7 900 333-44-55",
                "address": "Москва, Арбат, 2",
                "cargo_number": " restore-100 ",
            },
            self.manager,
        )

        with self.assertRaises(DuplicateCargoNumberError) as raised:
            restore_order(self.session, archived.id, self.admin)

        self.assertEqual(raised.exception.order.id, active.id)
        self.session.refresh(archived)
        self.assertTrue(archived.is_archived)

    def test_restored_order_is_visible_in_active_list(self):
        order = create_order(
            self.session,
            {
                "client_name": "Анна Соколова",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 5",
            },
            self.manager,
        )
        archive_order(self.session, order.id, self.admin)
        restore_order(self.session, order.id, self.admin)

        self.assertEqual([active.id for active in list_active_orders(self.session)], [order.id])

    def test_admin_can_bulk_archive_orders_and_writes_log(self):
        first = create_order(
            self.session,
            {"client_name": "Один", "client_phone": "+7 900 111-22-33", "address": "Москва, 1"},
            self.manager,
        )
        second = create_order(
            self.session,
            {"client_name": "Два", "client_phone": "+7 900 222-33-44", "address": "Москва, 2"},
            self.manager,
        )

        archived = archive_orders_bulk(self.session, [first.id, second.id], self.admin)

        self.assertEqual(archived, 2)
        self.assertTrue(self.session.get(Order, first.id).is_archived)
        self.assertTrue(self.session.get(Order, second.id).is_archived)
        archive_logs = self.session.scalars(
            select(OrderChangeLog).where(OrderChangeLog.action == "архив")
        ).all()
        self.assertEqual({log.order_id for log in archive_logs}, {first.id, second.id})

    def test_manager_and_courier_cannot_bulk_archive(self):
        order = create_order(
            self.session,
            {"client_name": "Три", "client_phone": "+7 900 333-33-44", "address": "Москва, 3"},
            self.manager,
        )
        with self.assertRaises(PermissionError):
            archive_orders_bulk(self.session, [order.id], self.manager)
        with self.assertRaises(PermissionError):
            archive_orders_bulk(self.session, [order.id], self.courier)
        self.assertFalse(self.session.get(Order, order.id).is_archived)

    def test_bulk_archive_empty_list_returns_zero_without_db_changes(self):
        self.assertEqual(archive_orders_bulk(self.session, [], self.admin), 0)
        self.assertEqual(archive_orders_bulk(self.session, None, self.admin), 0)

    def test_bulk_archive_skips_archived_unknown_and_invalid_ids(self):
        active = create_order(
            self.session,
            {"client_name": "Актив", "client_phone": "+7 900 111-22-33", "address": "Москва, 1"},
            self.manager,
        )
        already = create_order(
            self.session,
            {"client_name": "Уже", "client_phone": "+7 900 222-33-44", "address": "Москва, 2"},
            self.manager,
        )
        archive_order(self.session, already.id, self.admin)

        archived = archive_orders_bulk(
            self.session,
            [active.id, already.id, 999999, "not-a-number", None],
            self.admin,
        )

        self.assertEqual(archived, 1)
        self.assertTrue(self.session.get(Order, active.id).is_archived)
        already_archive_logs = self.session.scalars(
            select(OrderChangeLog).where(
                OrderChangeLog.order_id == already.id,
                OrderChangeLog.action == "архив",
            )
        ).all()
        # already-archived order keeps exactly one archive log (no duplicate)
        self.assertEqual(len(already_archive_logs), 1)


if __name__ == "__main__":
    unittest.main()
