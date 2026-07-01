import os
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.client import Client
from app.models.courier_cash_handover import CourierCashHandover
from app.models.enums import CourierCashHandoverStatus, OrderStatus, PaymentMethod, UserRole
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.order_service import archive_order, create_order
from app.services.payment_service import create_or_update_payment
from app.utils.dates import crm_today
from app.utils.security import hash_password


class PageAccessTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(self.engine)
        self._seed_users()

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_root_without_session_redirects_to_login(self):
        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_public_fastapi_docs_are_disabled(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)

                self.assertEqual(response.status_code, 404)

    def test_favicon_alias_and_static_asset_are_available(self):
        for path in ("/favicon.ico", "/static/img/favicon.svg"):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/svg+xml")

    def test_root_for_admin_redirects_to_orders(self):
        self._login("admin@courier.local", "admin123")

        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/orders")

    def test_root_for_manager_redirects_to_orders(self):
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/orders")

    def test_root_for_courier_redirects_to_courier_dashboard(self):
        self._login("courier@courier.local", "courier123")

        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/courier")

    def test_courier_dashboard_is_courier_only(self):
        self._login("admin@courier.local", "admin123")

        response = self.client.get("/courier", headers={"accept": "text/html"})

        self.assertEqual(response.status_code, 403)

        self.client.post("/logout", follow_redirects=False)
        self._login("courier@courier.local", "courier123")

        response = self.client.get("/courier")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Мои заявки", response.text)

    def test_forbidden_browser_request_returns_html_page_not_json(self):
        self._login("courier@courier.local", "courier123")

        response = self.client.get(
            "/payments",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Недостаточно прав", response.text)
        self.assertIn("Этот раздел недоступен для вашей роли.", response.text)
        self.assertIn('href="/courier"', response.text)
        self.assertNotIn('{"detail"', response.text)

    def test_payments_page_hides_list_until_filter_or_unpaid_view(self):
        order_id, order_code = self._seed_courier_order()
        today = date.today().isoformat()
        self._login("admin@courier.local", "admin123")

        response = self.client.get("/payments")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Неоплаченные заявки", response.text)
        self.assertNotIn("Выберите дату, статус или поиск.", response.text)
        self.assertIn(f'data-payment-id="{order_id}"', response.text)
        self.assertIn("Показано заявок", response.text)

        response = self.client.get(f"/payments?delivery_date={today}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(order_code, response.text)
        self.assertIn("Всего в списке", response.text)
        self.assertIn("Выручка", response.text)
        self.assertIn("ЗП курьера", response.text)
        self.assertIn(f'href="/payments?delivery_date={today}&amp;detail=unpaid"', response.text)

        response = self.client.get("/payments?view=unpaid")

        self.assertEqual(response.status_code, 200)
        self.assertIn(order_code, response.text)
        self.assertIn('name="view" value="unpaid"', response.text)
        self.assertIn("Открыт список неоплаченных заявок.", response.text)
        self.assertIn("Оплаченные заявки скрыты в этом списке", response.text)
        self.assertNotIn("<span>Частично</span>", response.text)
        self.assertIn("Показано заявок", response.text)

        response = self.client.get("/payments?view=unpaid&detail=paid")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(f'data-payment-id="{order_id}"', response.text)
        self.assertIn("Заявок по фильтрам нет.", response.text)

    def test_payments_success_alert_after_payment_preserves_filters(self):
        order_id, order_code = self._seed_courier_order()
        today = date.today().isoformat()
        self._login("admin@courier.local", "admin123")

        response = self.client.post(
            f"/payments/{order_id}/pay",
            data={
                "q": "",
                "payment_status": "",
                "delivery_date_filter": today,
                "detail": "",
                "view": "unpaid",
                "amount": "1000",
                "method": PaymentMethod.CASH.value,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/payments?delivery_date={today}&view=unpaid&success=payment&order_id={order_id}",
        )

        response = self.client.get(response.headers["location"])

        self.assertEqual(response.status_code, 200)
        self.assertIn("Оплата проведена. Заявка ушла из неоплаченных.", response.text)
        self.assertIn("Показать оплаченные", response.text)
        self.assertIn("Открыть заявку", response.text)
        self.assertIn(
            f'href="/payments?q={order_code}&amp;payment_status=paid&amp;detail=paid&amp;focus_order_id={order_id}"',
            response.text,
        )
        self.assertIn(f'href="/orders/{order_id}"', response.text)
        self.assertNotIn(f'data-payment-id="{order_id}"', response.text)

        response = self.client.get(f"/payments?q={order_code}&payment_status=paid&detail=paid&focus_order_id={order_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Открыт список оплаченных заявок.", response.text)
        self.assertIn(order_code, response.text)
        self.assertIn("payment-focus-row", response.text)

    def test_payments_delete_button_is_admin_only_and_preserves_filters(self):
        order_id, _ = self._seed_courier_order()
        today = date.today().isoformat()
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            create_or_update_payment(
                db,
                order_id,
                {"amount": "500", "method": PaymentMethod.CASH.value},
                manager,
            )

        self._login("manager@courier.local", "manager123")
        response = self.client.get(f"/payments?delivery_date={today}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(">Удалить</button>", response.text)

        self.client.post("/logout", follow_redirects=False)
        self._login("admin@courier.local", "admin123")
        response = self.client.get(f"/payments?delivery_date={today}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(">Удалить</button>", response.text)
        self.assertIn(f'name="delivery_date_filter" value="{today}"', response.text)

    def test_order_detail_shows_payments_readonly_for_manager_and_admin_delete(self):
        order_id, _ = self._seed_courier_order()
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            cash = create_or_update_payment(
                db,
                order_id,
                {"amount": "400", "method": PaymentMethod.CASH.value},
                manager,
            )
            card = create_or_update_payment(
                db,
                order_id,
                {"amount": "600", "method": PaymentMethod.TRANSFER.value},
                manager,
            )
            cash_id = cash.id

        self._login("manager@courier.local", "manager123")
        response = self.client.get(f"/orders/{order_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Оплаты", response.text)
        self.assertIn("400 ₽", response.text)
        self.assertIn("600 ₽", response.text)
        self.assertIn("Способ: Наличными", response.text)
        self.assertIn("Способ: Карта", response.text)
        self.assertIn("Дата оплаты:", response.text)
        self.assertNotIn(f'action="/payments/{cash_id}/delete"', response.text)
        self.assertNotIn(f'action="/payments/{cash_id}/edit"', response.text)

        response = self.client.post(
            f"/payments/{cash_id}/delete",
            data={"redirect_to": f"/orders/{order_id}"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            f"/payments/{cash_id}/edit",
            data={
                "order_id": str(order_id),
                "redirect_to": f"/orders/{order_id}",
                "amount": "300",
                "method": PaymentMethod.TRANSFER.value,
            },
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

        self.client.post("/logout", follow_redirects=False)
        self._login("admin@courier.local", "admin123")
        response = self.client.get(f"/orders/{order_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'action="/payments/{cash_id}/delete"', response.text)
        self.assertIn(f'action="/payments/{cash_id}/edit"', response.text)
        self.assertIn(f'name="order_id" value="{order_id}"', response.text)
        self.assertIn("Изменить", response.text)
        self.assertIn(">Удалить</button>", response.text)

        response = self.client.post(
            f"/payments/{cash_id}/edit",
            data={
                "order_id": str(order_id),
                "redirect_to": f"/orders/{order_id}",
                "amount": "300",
                "method": PaymentMethod.TRANSFER.value,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/orders/{order_id}")
        with self.SessionLocal() as db:
            payment = db.get(Payment, cash_id)
            self.assertEqual(payment.amount, Decimal("300.00"))
            self.assertEqual(payment.method, PaymentMethod.TRANSFER)
            logs = db.query(OrderChangeLog).filter_by(order_id=order_id).order_by(OrderChangeLog.id).all()
            self.assertEqual(logs[-1].action, "изменение оплаты")
            self.assertIn("400.00", logs[-1].old_value)
            self.assertIn("300.00", logs[-1].new_value)

        response = self.client.get(f"/orders/{order_id}")
        self.assertIn("изменение оплаты", response.text)
        self.assertIn("400 ₽ Наличными -&gt; 300 ₽ Карта", response.text)

        response = self.client.post(
            f"/payments/{cash_id}/edit",
            data={
                "order_id": str(order_id),
                "redirect_to": f"/orders/{order_id}",
                "amount": "100000",
                "method": PaymentMethod.TRANSFER.value,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("payment_error=", response.headers["location"])
        with self.SessionLocal() as db:
            payment = db.get(Payment, cash_id)
            self.assertEqual(payment.amount, Decimal("300.00"))

    def test_payments_zero_amount_order_has_no_pay_form(self):
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            order = create_order(
                db,
                {
                    "client_name": "Нулевая Оплата",
                    "client_phone": "+7 900 555-00-00",
                    "address": "Москва, Тестовая, 1",
                    "delivery_date": crm_today().isoformat(),
                },
                manager,
            )
            order_code = order.order_code
            order_id = order.id

        self._login("manager@courier.local", "manager123")
        response = self.client.get(f"/payments?q={order_code}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(order_code, response.text)
        self.assertIn("Нет суммы к оплате", response.text)
        self.assertNotIn(f'action="/payments/{order_id}/pay"', response.text)

    def test_users_section_is_admin_only(self):
        self._login("admin@courier.local", "admin123")

        response = self.client.get("/users")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Сотрудники", response.text)
        self.assertIn("Создать сотрудника", response.text)

        self.client.post("/logout", follow_redirects=False)
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/users", headers={"accept": "text/html"})

        self.assertEqual(response.status_code, 403)

    def test_admin_creates_plain_login_user_and_sees_success_message(self):
        self._login("admin@courier.local", "admin123")

        response = self.client.get("/users/new")

        self.assertEqual(response.status_code, 200)
        self.assertIn('label for="email">Логин *', response.text)
        self.assertIn('name="email" type="text"', response.text)
        self.assertIn('placeholder="manager1"', response.text)
        self.assertIn('data-password-toggle="password"', response.text)

        response = self.client.post(
            "/users",
            data={
                "full_name": "QA Plain Manager",
                "email": "manager_plain",
                "phone": "+375291234567",
                "role": UserRole.MANAGER.value,
                "password": "plain123",
                "is_active": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/users?success=created")

        response = self.client.get("/users?success=created")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Сотрудник создан", response.text)
        self.assertIn("manager_plain", response.text)
        self.assertIn("+375291234567", response.text)

        self.client.post("/logout", follow_redirects=False)
        self._login("manager_plain", "plain123")
        response = self.client.get("/users", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 403)

    def test_admin_edit_user_redirects_to_users_with_saved_message(self):
        self._login("admin@courier.local", "admin123")
        with self.SessionLocal() as db:
            user = User(
                full_name="Редактируемый",
                email="edit_plain",
                phone="+375291234567",
                password_hash=hash_password("plain123"),
                role=UserRole.COURIER,
                is_active=True,
            )
            db.add(user)
            db.commit()
            user_id = user.id

        response = self.client.post(
            f"/users/{user_id}/edit",
            data={
                "full_name": "Редактируемый Курьер",
                "phone": "+7 999 100-00-03",
                "role": UserRole.COURIER.value,
                "password": "",
                "is_active": "on",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/users?success=saved")

        response = self.client.get("/users?success=saved")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Сотрудник сохранён", response.text)
        self.assertIn("+79991000003", response.text)

    def test_manager_can_create_order_from_client_card_without_retyping_client(self):
        self._login("manager@courier.local", "manager123")
        client_id = self._seed_client()

        response = self.client.get(f"/orders/new?client_id={client_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Заявка для клиента", response.text)
        self.assertIn("ООО Вектор", response.text)
        self.assertIn("Звонить заранее", response.text)
        self.assertIn(f'name="client_id" value="{client_id}"', response.text)

        response = self.client.post(
            "/orders",
            data={
                "client_id": str(client_id),
                "client_name": "ООО Вектор",
                "client_phone": "+7 900 444-55-66",
                "address": "Химки, улица Панфилова, 8",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            order = db.query(Order).one()
            self.assertEqual(order.client_id, client_id)
            self.assertEqual(order.client_name_snapshot, "ООО Вектор")
            self.assertEqual(order.client_phone_snapshot, "+7 900 444-55-66")

        self.client.post("/logout", follow_redirects=False)
        self._login("admin@courier.local", "admin123")
        response = self.client.get(f"/orders/new?client_id={client_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Заявка для клиента", response.text)

    def test_order_form_hides_costs_on_create_and_keeps_them_on_edit(self):
        self._login("manager@courier.local", "manager123")

        with patch("app.routers.orders.default_delivery_date", return_value=date(2026, 6, 10)):
            response = self.client.get("/orders/new")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Стоимость и расходы", response.text)
        self.assertNotIn('id="base_delivery_cost"', response.text)
        self.assertNotIn("Проверим клиента по телефону и ФИО", response.text)
        self.assertIn("Заполните данные клиента, доставки и груза.", response.text)
        self.assertNotIn("Заполните данные клиента, доставки, груза и расходов.", response.text)
        self.assertIn("Примечание к заявке", response.text)
        self.assertNotIn(">Примечание от клиента</label>", response.text)
        self.assertNotIn(">Примечание для сотрудников</label>", response.text)
        self.assertIn('id="delivery-date" name="delivery_date" type="date" value="2026-06-10"', response.text)
        self.assertIn('value="in_work" selected', response.text)
        self.assertIn('data-courier-field hidden', response.text)
        self.assertIn('data-client-suggestions', response.text)

        response = self.client.post(
            "/orders",
            data={
                "client_name": "Заявка Без Стоимости",
                "client_phone": "+7 900 555-44-33",
                "address": "Москва, Тестовая улица, 5",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            order = db.query(Order).one()
            order_id = order.id
            self.assertEqual(str(order.delivery_cost), "0.00")

        response = self.client.get(f"/orders/{order_id}/edit")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Стоимость и расходы", response.text)
        self.assertIn("Заполните данные клиента, доставки, груза и расходов.", response.text)
        self.assertIn('id="base_delivery_cost"', response.text)
        self.assertIn('data-order-form-mode="edit"', response.text)
        self.assertIn('data-courier-field', response.text)
        self.assertNotIn('data-courier-field hidden', response.text)

    def test_order_form_contains_client_suggestions_for_existing_clients(self):
        client_id = self._seed_client()
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/orders/new")

        self.assertEqual(response.status_code, 200)
        self.assertIn("+7 900 444-55-66", response.text)
        self.assertIn(f'\\"id\\": {client_id}', response.text)

    def test_duplicate_cargo_number_error_links_existing_order(self):
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            existing = create_order(
                db,
                {
                    "client_name": "Первый Клиент",
                    "client_phone": "+7 900 111-22-33",
                    "address": "Москва, Ленина, 1",
                    "cargo_number": "334e65k33ep5h",
                },
                manager,
            )
            existing_id = existing.id
            existing_code = existing.order_code

        self._login("manager@courier.local", "manager123")
        response = self.client.post(
            "/orders",
            data={
                "client_name": "Второй Клиент",
                "client_phone": "+7 900 222-33-44",
                "address": "Москва, Ленина, 2",
                "cargo_number": "334e65k33ep5h",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Заявка с таким номером груза уже есть:", response.text)
        self.assertIn(f'href="/orders/{existing_id}"', response.text)
        self.assertIn(existing_code, response.text)

    def test_courier_can_mark_own_order_delivered_but_cannot_pay(self):
        order_id, order_code = self._seed_courier_order(delivery_date=crm_today())
        self._login("courier@courier.local", "courier123")

        response = self.client.get("/courier")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Всего рабочих дней", response.text)
        self.assertIn("Всего заявок назначено", response.text)
        self.assertIn(order_code, response.text)
        self.assertIn('name="mode" data-courier-mode>', response.text)
        self.assertNotIn("Заявки на сегодня", response.text)
        self.assertNotIn(">Сегодня</a>", response.text)
        self.assertIn('data-courier-filter-actions hidden', response.text)
        self.assertIn(f'action="/courier/orders/{order_id}/delivered"', response.text)
        self.assertIn(">Доставлено</button>", response.text)

        response = self.client.get(f"/courier/orders/{order_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'action="/courier/orders/{order_id}/delivered"', response.text)
        self.assertIn(">Доставлено</button>", response.text)

        response = self.client.post(
            f"/payments/{order_id}/pay",
            data={"amount": "100", "method": PaymentMethod.CASH.value},
            headers={"accept": "text/html"},
        )

        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            f"/courier/orders/{order_id}/delivered",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/courier/orders/{order_id}")
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertEqual(order.status, OrderStatus.DELIVERED)
            logs = db.query(OrderChangeLog).filter_by(order_id=order_id).order_by(OrderChangeLog.id).all()
            self.assertEqual([log.action for log in logs], ["создание", "доставлено"])

        response = self.client.get("/courier")
        self.assertIn(order_code, response.text)
        self.assertIn("Итого в кассу", response.text)

        response = self.client.get("/courier?detail=assigned")
        self.assertIn("<h2>Всего заявок назначено</h2>", response.text)
        self.assertIn(order_code, response.text)
        self.assertNotIn(f'action="/courier/orders/{order_id}/delivered"', response.text)

    def test_courier_delivered_fetch_stays_on_order_card(self):
        order_id, _ = self._seed_courier_order(delivery_date=crm_today())
        self._login("courier@courier.local", "courier123")

        response = self.client.post(
            f"/courier/orders/{order_id}/delivered",
            headers={"x-requested-with": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "status": "delivered",
                "status_label": "Доставлено",
                "status_class": "status-done",
            },
        )
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_courier_handover_goes_to_admin_accounting_flow(self):
        self._seed_courier_order()
        self._login("courier@courier.local", "courier123")

        response = self.client.get("/courier")

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/courier/handovers"', response.text)
        self.assertIn(">Выдано</button>", response.text)

        response = self.client.post(
            "/courier/handovers",
            data={
                "mode": "today",
                "period_start": date.today().isoformat(),
                "period_end": date.today().isoformat(),
                "amount": "100",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/courier?mode=date&date_from={date.today().isoformat()}")
        with self.SessionLocal() as db:
            handover = db.query(CourierCashHandover).one()
            handover_id = handover.id
            self.assertEqual(handover.amount, Decimal("100.00"))
            self.assertEqual(handover.status, CourierCashHandoverStatus.PENDING)

        self.client.post("/logout", follow_redirects=False)
        self._login("admin@courier.local", "admin123")
        response = self.client.get("/accounting/handovers")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Сдачи курьеров", response.text)
        self.assertIn("На проверке", response.text)
        self.assertIn(f'action="/accounting/handovers/{handover_id}/confirm"', response.text)
        self.assertIn(f'action="/accounting/handovers/{handover_id}/reject"', response.text)

        response = self.client.post(
            f"/accounting/handovers/{handover_id}/confirm",
            data={"comment": "ок"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            handover = db.get(CourierCashHandover, handover_id)
            self.assertEqual(handover.status, CourierCashHandoverStatus.CONFIRMED)
            self.assertEqual(handover.comment, "ок")

    def test_courier_period_mode_hides_today_cash_controls_even_for_today_dates(self):
        self._seed_courier_order()
        self._login("courier@courier.local", "courier123")

        today = date.today().isoformat()
        response = self.client.get(f"/courier?mode=period&date_from={today}&date_to={today}")

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="period" selected', response.text)
        self.assertIn('data-courier-date-to-field', response.text)
        self.assertIn(">Показать</button>", response.text)
        self.assertIn("ЗП", response.text)
        self.assertIn("После оплаты", response.text)
        self.assertIn("Всего рабочих дней", response.text)
        self.assertNotIn("Общая стоимость", response.text)
        self.assertNotIn("К выдаче", response.text)
        self.assertNotIn("Стоимость для клиента", response.text)
        self.assertNotIn('action="/courier/handovers"', response.text)
        self.assertNotIn(">Выдано</button>", response.text)

    def test_manager_cannot_confirm_handover_or_see_accounting_handovers(self):
        with self.SessionLocal() as db:
            courier = db.query(User).filter_by(email="courier@courier.local").one()
            handover = CourierCashHandover(
                courier_id=courier.id,
                period_start=date.today(),
                period_end=date.today(),
                amount=Decimal("100.00"),
                status=CourierCashHandoverStatus.PENDING,
                created_by_id=courier.id,
            )
            db.add(handover)
            db.commit()
            handover_id = handover.id

        self._login("manager@courier.local", "manager123")

        response = self.client.get("/accounting/handovers", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            f"/accounting/handovers/{handover_id}/confirm",
            data={"comment": "нет"},
            headers={"accept": "text/html"},
        )
        self.assertEqual(response.status_code, 403)

    def test_manager_route_hides_money_admin_route_shows_it(self):
        _, order_code = self._seed_courier_order()
        with self.SessionLocal() as db:
            courier = db.query(User).filter_by(email="courier@courier.local").one()
            route_url = f"/couriers/{courier.id}/route?date={date.today().isoformat()}"
            detail_url = f"{route_url}&detail=all"

        self._login("manager@courier.local", "manager123")

        response = self.client.get(route_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Маршрут", response.text)
        self.assertIn("Все заявки", response.text)
        self.assertIn('name="date"', response.text)
        self.assertIn('name="courier_id"', response.text)
        self.assertLess(response.text.index('name="date"'), response.text.index('name="courier_id"'))
        self.assertNotIn("Выручка", response.text)
        self.assertNotIn("ЗП курьера", response.text)
        self.assertNotIn(">Выручка</th>", response.text)
        self.assertNotIn(">ЗП курьера</th>", response.text)

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Все заявки", response.text)
        self.assertIn(order_code, response.text)
        self.assertNotIn(">Выручка</th>", response.text)
        self.assertNotIn(">ЗП курьера</th>", response.text)

        response = self.client.get("/couriers")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Маршрут", response.text)
        self.assertIn("Все</option>", response.text)
        self.assertIn("Курьеры с заявками", response.text)

        self.client.post("/logout", follow_redirects=False)
        self._login("admin@courier.local", "admin123")

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Выручка", response.text)
        self.assertIn("ЗП курьера", response.text)
        self.assertIn(">Выручка</th>", response.text)
        self.assertIn(">ЗП курьера</th>", response.text)

        response = self.client.get(f"/couriers/{courier.id}/route?date_from={date.today().isoformat()}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Маршрут", response.text)
        self.assertIn("Все заявки", response.text)

    def test_route_default_date_uses_crm_today_helper_and_explicit_date_wins(self):
        default_date = date(2026, 6, 9)
        explicit_date = date(2026, 6, 8)
        with self.SessionLocal() as db:
            courier = db.query(User).filter_by(email="courier@courier.local").one()
            courier_id = courier.id

        self._login("manager@courier.local", "manager123")

        with patch("app.routers.couriers.crm_today", return_value=default_date) as crm_today:
            response = self.client.get("/couriers")
            route_response = self.client.get(f"/couriers/{courier_id}/route")
            explicit_response = self.client.get(f"/couriers/{courier_id}/route?date={explicit_date.isoformat()}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'name="date" type="date" value="{default_date.isoformat()}"', response.text)
        self.assertEqual(route_response.status_code, 200)
        self.assertIn(f'name="date" type="date" value="{default_date.isoformat()}"', route_response.text)
        self.assertEqual(explicit_response.status_code, 200)
        self.assertIn(f'name="date" type="date" value="{explicit_date.isoformat()}"', explicit_response.text)
        self.assertEqual(crm_today.call_count, 2)

    def test_long_cargo_number_is_rendered_in_manager_and_courier_cards(self):
        cargo_number = "QA-COURIER-443700-LONG-CARGO-NUMBER"
        self._seed_courier_order(cargo_number=cargo_number, delivery_date=crm_today())
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/orders")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'<span class="cargo-pill">{cargo_number}</span>', response.text)

        self.client.post("/logout", follow_redirects=False)
        self._login("courier@courier.local", "courier123")
        response = self.client.get("/courier?detail=assigned")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f'<span class="cargo-pill cargo-pill-strong">{cargo_number}</span>',
            response.text,
        )

    def test_orders_list_has_pickup_copy_button_with_cargo_fields(self):
        cargo_number = "6182-2270-0519-1"
        _, order_code = self._seed_courier_order(
            cargo_number=cargo_number,
            cargo_phone="+7 936 302-96-72",
            places_count="1",
        )
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/orders")

        self.assertEqual(response.status_code, 200)
        self.assertIn(">Копировать</button>", response.text)
        self.assertNotIn(">Для забора</button>", response.text)
        self.assertIn("data-pickup-copy", response.text)
        self.assertIn("Скопировано", response.text)
        expected_text = (
            f"{order_code} 1 номер груза {cargo_number} "
            "номер карго +7 936 302-96-72"
        )
        self.assertIn(f'data-pickup-copy-text="{expected_text}"', response.text)

    def test_manager_sees_quick_payment_only_while_order_is_not_paid(self):
        order_id, order_code = self._seed_courier_order()
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/orders")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<select id="order-sort" name="sort" data-order-sort>', response.text)
        self.assertIn('<option value="delivery_date" >По дате доставки</option>', response.text)
        self.assertIn(">Дата доставки с</label>", response.text)
        self.assertIn(">Дата доставки по</label>", response.text)
        self.assertIn('data-order-payment="pending"', response.text)
        self.assertIn(f'action="/orders/{order_id}/status"', response.text)
        self.assertIn(f'action="/orders/{order_id}/quick-pay"', response.text)
        self.assertIn(">Наличные</label>", response.text)
        self.assertIn(">Карта</label>", response.text)

        response = self.client.post(
            f"/orders/{order_id}/quick-pay",
            data={"cash_amount": "400", "card_amount": ""},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)

        response = self.client.get("/orders")
        self.assertIn('data-order-payment="partial"', response.text)
        self.assertIn(">Частично</span>", response.text)
        self.assertIn(f'action="/orders/{order_id}/quick-pay"', response.text)

        response = self.client.post(
            f"/orders/{order_id}/quick-pay",
            data={"cash_amount": "", "card_amount": "600"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)

        response = self.client.get("/orders")
        self.assertIn('data-order-payment="paid"', response.text)
        self.assertNotIn(f'action="/orders/{order_id}/quick-pay"', response.text)

        response = self.client.post(
            f"/orders/{order_id}/quick-pay",
            data={"cash_amount": "1", "card_amount": ""},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Заявка уже оплачена.", response.text)

    def test_manager_orders_filter_uses_query_params_and_preserves_them_in_quick_forms(self):
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            courier = db.query(User).filter_by(email="courier@courier.local").one()
            target = create_order(
                db,
                {
                    "client_name": "Анна Соколова",
                    "client_phone": "+7 900 222-33-44",
                    "address": "Москва, Ленина, 5",
                    "delivery_date": "2026-06-02",
                    "courier_id": str(courier.id),
                    "base_delivery_cost": "1000",
                },
                manager,
            )
            other = create_order(
                db,
                {
                    "client_name": "Иван Петров",
                    "client_phone": "+7 900 111-22-33",
                    "address": "Москва, Тверская, 1",
                    "delivery_date": "2026-06-01",
                },
                manager,
            )
            target_id = target.id
            target_code = target.order_code
            other_code = other.order_code
            courier_id = courier.id

        self._login("manager@courier.local", "manager123")

        response = self.client.get(
            "/orders",
            params={
                "q": "Анна",
                "sort": "courier",
                "date_from": "2026-06-02",
                "date_to": "2026-06-02",
                "status": OrderStatus.AT_COURIER.value,
                "courier_id": str(courier_id),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(target_code, response.text)
        self.assertNotIn(other_code, response.text)
        self.assertIn('value="2026-06-02"', response.text)
        self.assertIn('value="at_courier" selected', response.text)
        self.assertIn(f'value="{courier_id}" selected', response.text)
        self.assertIn('data-order-courier-filter-field ', response.text)
        self.assertNotIn('data-order-courier-filter-field hidden', response.text)
        self.assertIn('name="filter_status" value="at_courier"', response.text)
        self.assertIn(f'name="filter_courier_id" value="{courier_id}"', response.text)
        self.assertIn("return_url=%2Forders%3Fq%3D%25D0%2590%25D0%25BD%25D0%25BD%25D0%25B0", response.text)

        response = self.client.post(
            f"/orders/{target_id}/quick-pay",
            data={
                "q": "Анна",
                "sort": "courier",
                "date_from": "2026-06-02",
                "date_to": "2026-06-02",
                "filter_status": OrderStatus.AT_COURIER.value,
                "filter_courier_id": str(courier_id),
                "return_url": (
                    f"/orders?q=%D0%90%D0%BD%D0%BD%D0%B0&sort=courier&"
                    f"date_from=2026-06-02&date_to=2026-06-02&"
                    f"status=at_courier&courier_id={courier_id}"
                ),
                "cash_amount": "100",
                "card_amount": "",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("q=%D0%90%D0%BD%D0%BD%D0%B0", response.headers["location"])
        self.assertIn("sort=courier", response.headers["location"])
        self.assertIn("date_from=2026-06-02", response.headers["location"])
        self.assertIn("date_to=2026-06-02", response.headers["location"])
        self.assertIn("status=at_courier", response.headers["location"])
        self.assertIn(f"courier_id={courier_id}", response.headers["location"])

        response = self.client.get(
            f"/orders/{target_id}",
            params={
                "return_url": (
                    f"/orders?q=%D0%90%D0%BD%D0%BD%D0%B0&sort=courier&"
                    f"date_from=2026-06-02&date_to=2026-06-02&"
                    f"status=at_courier&courier_id={courier_id}"
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            (
                f'href="/orders?q=%D0%90%D0%BD%D0%BD%D0%B0&amp;sort=courier&amp;'
                f'date_from=2026-06-02&amp;date_to=2026-06-02&amp;'
                f'status=at_courier&amp;courier_id={courier_id}"'
            ),
            response.text,
        )

        response = self.client.get(
            f"/orders/{target_id}",
            params={"return_url": "https://example.com/orders"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/orders"', response.text)
        self.assertNotIn("https://example.com", response.text)

        response = self.client.get(
            "/orders",
            params={
                "status": OrderStatus.IN_WORK.value,
                "courier_id": str(courier_id),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="in_work" selected', response.text)
        self.assertIn('data-order-courier-filter-field hidden', response.text)

    def test_manager_orders_empty_query_params_render_html_not_json_error(self):
        self._seed_courier_order()
        self._login("manager@courier.local", "manager123")

        response = self.client.get(
            "/orders",
            params={
                "q": "",
                "sort": "newest",
                "date_from": "",
                "date_to": "",
                "status": "",
                "courier_id": "",
            },
            headers={"accept": "text/html"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Заявки", response.text)
        self.assertIn("Найдено заявок:", response.text)
        self.assertNotIn('{"detail"', response.text)

    def test_manager_orders_bad_query_params_are_ignored_on_ui_route(self):
        self._seed_courier_order()
        self._login("manager@courier.local", "manager123")

        response = self.client.get(
            "/orders",
            params={
                "date_from": "bad-date",
                "date_to": "2026-",
                "status": "bad-status",
                "courier_id": "not-a-number",
            },
            headers={"accept": "text/html"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Заявки", response.text)
        self.assertNotIn('{"detail"', response.text)

    def test_manager_can_change_status_from_orders_list(self):
        order_id, _ = self._seed_courier_order()
        self._login("manager@courier.local", "manager123")

        response = self.client.post(
            f"/orders/{order_id}/status",
            data={"status": OrderStatus.DELIVERED.value},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertEqual(order.status, OrderStatus.DELIVERED)
            logs = db.query(OrderChangeLog).filter_by(order_id=order_id).order_by(OrderChangeLog.id).all()
            self.assertEqual([log.action for log in logs], ["создание", "статус"])

    def test_manager_must_choose_courier_for_at_courier_from_orders_list(self):
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            courier = db.query(User).filter_by(email="courier@courier.local").one()
            order = create_order(
                db,
                {
                    "client_name": "Анна Соколова",
                    "client_phone": "+7 900 222-33-44",
                    "address": "Москва, Ленина, 5",
                },
                manager,
            )
            order_id = order.id
            courier_id = courier.id

        self._login("manager@courier.local", "manager123")

        response = self.client.post(
            f"/orders/{order_id}/status",
            data={"status": OrderStatus.AT_COURIER.value},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Выберите курьера", response.text)

        response = self.client.post(
            f"/orders/{order_id}/status",
            data={"status": OrderStatus.AT_COURIER.value, "courier_id": str(courier_id)},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertEqual(order.status, OrderStatus.AT_COURIER)
            self.assertEqual(order.courier_id, courier_id)

    def test_manager_can_cancel_order_from_status_but_cannot_restore_it(self):
        order_id, _ = self._seed_courier_order()
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/orders")

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="cancelled_archive"', response.text)
        self.assertNotIn(f'action="/orders/{order_id}/archive"', response.text)

        response = self.client.post(
            f"/orders/{order_id}/status",
            data={"status": "cancelled_archive"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/orders")
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertTrue(order.is_archived)
            logs = db.query(OrderChangeLog).filter_by(order_id=order_id).order_by(OrderChangeLog.id).all()
            self.assertEqual([log.action for log in logs], ["создание", "архив"])

        response = self.client.post(
            f"/orders/{order_id}/restore",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)

        self.client.post("/logout", follow_redirects=False)
        self._login("admin@courier.local", "admin123")
        response = self.client.get("/archive")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'action="/archive/{order_id}/restore"', response.text)

    def test_archive_restore_duplicate_cargo_error_links_active_order(self):
        with self.SessionLocal() as db:
            admin = db.query(User).filter_by(email="admin@courier.local").one()
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            archived = create_order(
                db,
                {
                    "client_name": "Архивный Клиент",
                    "client_phone": "+7 900 111-22-33",
                    "address": "Москва, Архивная, 1",
                    "cargo_number": "RESTORE-UI-100",
                },
                manager,
            )
            archive_order(db, archived.id, admin)
            active = create_order(
                db,
                {
                    "client_name": "Активный Клиент",
                    "client_phone": "+7 900 222-33-44",
                    "address": "Москва, Активная, 2",
                    "cargo_number": " restore-ui-100 ",
                },
                manager,
            )
            archived_id = archived.id
            active_id = active.id
            active_code = active.order_code

        self._login("admin@courier.local", "admin123")
        response = self.client.post(
            f"/archive/{archived_id}/restore",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Не удалось восстановить. Заявка с таким номером груза уже есть:", response.text)
        self.assertIn(f'href="/orders/{active_id}"', response.text)
        self.assertIn(active_code, response.text)
        with self.SessionLocal() as db:
            archived_order = db.get(Order, archived_id)
            self.assertTrue(archived_order.is_archived)

    def test_order_detail_uses_cancel_button_and_note_summary(self):
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            client = Client(
                full_name="Клиент С Заметкой",
                phone="+7 900 555-66-77",
                notes="Конфликтный клиент",
                created_by_id=manager.id,
            )
            db.add(client)
            db.flush()
            order = create_order(
                db,
                {
                    "client_id": str(client.id),
                    "client_name": client.full_name,
                    "client_phone": client.phone,
                    "address": "Москва, Ленина, 1",
                    "general_note": "Доставить после 18:00",
                },
                manager,
            )
            order_id = order.id

        self._login("manager@courier.local", "manager123")
        response = self.client.get(f"/orders/{order_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(">Отменить заявку</button>", response.text)
        self.assertNotIn(">В архив</button>", response.text)
        self.assertIn('data-cancel-order-modal', response.text)
        self.assertIn("Отменить заявку?", response.text)
        self.assertIn("Заявка попадёт в архив. Восстановить сможет админ.", response.text)
        self.assertIn("Не отменять", response.text)
        self.assertNotIn("window.confirm", response.text)
        self.assertIn('class="order-notes-summary"', response.text)
        self.assertIn("Заметка клиента", response.text)
        self.assertIn("Конфликтный клиент", response.text)
        self.assertEqual(response.text.count("Конфликтный клиент"), 1)
        self.assertIn("Примечание к заявке", response.text)
        self.assertIn("Доставить после 18:00", response.text)
        self.assertNotIn("Нет примечания", response.text)
        self.assertIn("Менеджер", response.text)

    def test_order_detail_does_not_render_empty_note_summary(self):
        order_id, _ = self._seed_courier_order()
        self._login("manager@courier.local", "manager123")

        response = self.client.get(f"/orders/{order_id}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('class="order-notes-summary"', response.text)
        self.assertNotIn("Нет примечания", response.text)

    def test_kara_cost_is_available_across_order_pages_and_accounting(self):
        self._login("manager@courier.local", "manager123")

        response = self.client.get("/orders/new")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Стоимость и расходы", response.text)

        response = self.client.post(
            "/orders",
            data={
                "client_name": "Тест Кара",
                "client_phone": "+7 999 111-22-33",
                "address": "Москва, Тестовая улица, 1",
                "delivery_date": "2026-06-01",
                "base_delivery_cost": "1000",
                "market_cube_cost": "100",
                "market_loader_cost": "200",
                "market_storage_cost": "300",
                "market_kara_cost": "400",
                "market_other_cost": "500",
                "courier_pay": "600",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            order = db.query(Order).one()
            order_id = order.id
            self.assertEqual(str(order.market_kara_cost), "400.00")
            self.assertEqual(str(order.delivery_cost), "2500.00")
            self.assertEqual(str(order.courier_pay), "600.00")

        response = self.client.get(f"/orders/{order_id}/edit")
        self.assertEqual(response.status_code, 200)
        cost_labels = [
            ("base_delivery_cost", "Базовая доставка"),
            ("market_cube_cost", "Куб"),
            ("market_loader_cost", "Грузчик"),
            ("market_storage_cost", "Хранение"),
            ("market_kara_cost", "Кара"),
            ("market_other_cost", "Прочие"),
            ("courier_pay", "Курьер"),
        ]
        label_positions = [
            response.text.index(f'for="{field_name}">{label}</label>')
            for field_name, label in cost_labels
        ]
        self.assertEqual(label_positions, sorted(label_positions))

        response = self.client.get(f"/orders/{order_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<span>Кара</span>", response.text)

        response = self.client.get(f"/orders/{order_id}/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="market_kara_cost"', response.text)
        self.assertIn('name="market_kara_cost"', response.text)
        self.assertIn('value="400.00"', response.text)

        response = self.client.post(
            f"/orders/{order_id}/edit",
            data={
                "client_name": "Тест Кара",
                "client_phone": "+7 999 111-22-33",
                "address": "Москва, Тестовая улица, 1",
                "delivery_date": "2026-06-01",
                "base_delivery_cost": "1000",
                "market_cube_cost": "100",
                "market_loader_cost": "200",
                "market_storage_cost": "300",
                "market_kara_cost": "450",
                "market_other_cost": "500",
                "courier_pay": "600",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertEqual(str(order.market_kara_cost), "450.00")
            self.assertEqual(str(order.delivery_cost), "2550.00")

        self.client.post("/logout", follow_redirects=False)
        self._login("admin@courier.local", "admin123")
        response = self.client.get("/accounting?period=day&report_date=2026-06-01")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<span class="expense-name">Кара</span>', response.text)
        self.assertIn('<span class="expense-value">450 ₽</span>', response.text)

    def test_admin_can_bulk_archive_orders_and_they_leave_orders_and_enter_archive(self):
        first_id, first_code = self._seed_courier_order()
        second_id, _ = self._seed_courier_order()
        self._login("admin@courier.local", "admin123")

        response = self.client.post(
            "/orders/bulk-archive",
            data={"order_ids": [str(first_id), str(second_id)]},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/orders?deleted=2")
        with self.SessionLocal() as db:
            self.assertTrue(db.get(Order, first_id).is_archived)
            self.assertTrue(db.get(Order, second_id).is_archived)

        orders_page = self.client.get("/orders")
        self.assertEqual(orders_page.status_code, 200)
        self.assertNotIn(first_code, orders_page.text)

        archive_page = self.client.get("/archive")
        self.assertEqual(archive_page.status_code, 200)
        self.assertIn(first_code, archive_page.text)

    def test_manager_and_courier_get_403_on_bulk_archive(self):
        order_id, _ = self._seed_courier_order()
        self._login("manager@courier.local", "manager123")
        response = self.client.post(
            "/orders/bulk-archive",
            data={"order_ids": [str(order_id)]},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)

        self.client.post("/logout", follow_redirects=False)
        self._login("courier@courier.local", "courier123")
        response = self.client.post(
            "/orders/bulk-archive",
            data={"order_ids": [str(order_id)]},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)
        with self.SessionLocal() as db:
            self.assertFalse(db.get(Order, order_id).is_archived)

    def test_bulk_archive_empty_list_redirects_without_error(self):
        self._login("admin@courier.local", "admin123")
        response = self.client.post("/orders/bulk-archive", data={}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/orders")

    def test_bulk_archive_keeps_payments_and_logs(self):
        order_id, _ = self._seed_courier_order()
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            create_or_update_payment(db, order_id, {"amount": "1000", "method": "cash"}, manager)
        self._login("admin@courier.local", "admin123")

        response = self.client.post(
            "/orders/bulk-archive",
            data={"order_ids": [str(order_id)]},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            self.assertTrue(db.get(Order, order_id).is_archived)
            self.assertEqual(db.query(Payment).filter_by(order_id=order_id).count(), 1)
            actions = [
                log.action
                for log in db.query(OrderChangeLog)
                .filter_by(order_id=order_id)
                .order_by(OrderChangeLog.id)
                .all()
            ]
        self.assertIn("создание", actions)
        self.assertIn("оплата", actions)
        self.assertIn("архив", actions)

    def test_bulk_archived_order_excluded_from_accounting(self):
        order_id, order_code = self._seed_courier_order(delivery_date=date.today())
        self._login("admin@courier.local", "admin123")
        self.client.post(
            "/orders/bulk-archive",
            data={"order_ids": [str(order_id)]},
            follow_redirects=False,
        )

        report = self.client.get(f"/accounting?period=day&report_date={date.today().isoformat()}")
        self.assertEqual(report.status_code, 200)
        self.assertNotIn(order_code, report.text)

    def _seed_users(self):
        users = [
            ("Администратор", "admin@courier.local", "admin123", UserRole.ADMIN),
            ("Менеджер", "manager@courier.local", "manager123", UserRole.MANAGER),
            ("Курьер", "courier@courier.local", "courier123", UserRole.COURIER),
        ]
        with self.SessionLocal() as db:
            for full_name, email, password, role in users:
                db.add(
                    User(
                        full_name=full_name,
                        phone=None,
                        email=email,
                        password_hash=hash_password(password),
                        role=role,
                    )
                )
            db.commit()

    def _login(self, email: str, password: str):
        response = self.client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def _seed_client(self) -> int:
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            client = Client(
                full_name="ООО Вектор",
                phone="+7 900 444-55-66",
                notes="Звонить заранее",
                created_by_id=manager.id,
            )
            db.add(client)
            db.commit()
            return client.id

    def _seed_courier_order(
        self,
        *,
        cargo_number: str | None = None,
        cargo_phone: str | None = None,
        places_count: str | None = None,
        delivery_date: date | None = None,
    ) -> tuple[int, str]:
        with self.SessionLocal() as db:
            manager = db.query(User).filter_by(email="manager@courier.local").one()
            courier = db.query(User).filter_by(email="courier@courier.local").one()
            order = create_order(
                db,
                {
                    "client_name": "Иван Петров",
                    "client_phone": "+7 900 111-22-33",
                    "address": "Москва, Тверская, 1",
                    "delivery_date": (delivery_date or date.today()).isoformat(),
                    "courier_id": str(courier.id),
                    "cargo_number": cargo_number or "",
                    "cargo_phone": cargo_phone or "",
                    "places_count": places_count or "",
                    "base_delivery_cost": "1000",
                    "courier_pay": "300",
                },
                manager,
            )
            return order.id, order.order_code


if __name__ == "__main__":
    unittest.main()
