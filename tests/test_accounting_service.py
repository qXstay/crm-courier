from datetime import date
from decimal import Decimal
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.enums import PaymentDisplayStatus, PaymentMethod, PaymentStatus, UserRole
from app.models.user import User
from app.services.accounting_service import accounting_payment_status_label, get_accounting_report
from app.services.order_service import archive_order, create_order
from app.services.payment_service import create_or_update_payment
from app.utils.auth import require_roles


class AccountingServiceTest(unittest.TestCase):
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

    def test_day_report_calculates_totals_for_selected_delivery_date(self):
        selected_date = date(2026, 5, 28)
        first = self._create_order(
            selected_date,
            client_name="Иван Петров",
            client_phone="+7 900 111-22-33",
            cube="100",
            loader="50",
            storage="25",
            kara="15",
            other="10",
            base="200",
            courier_pay="200",
        )
        second = self._create_order(
            selected_date,
            client_name="Анна Соколова",
            client_phone="+7 900 222-33-44",
            cube="300",
            base="150",
            loader="0",
            storage="0",
            other="0",
            courier_pay="150",
        )
        self._create_order(
            date(2026, 5, 29),
            client_name="Другой День",
            client_phone="+7 900 333-44-55",
            cube="1000",
            base="1000",
            courier_pay="1000",
        )
        create_or_update_payment(
            self.session,
            first.id,
            {"amount": "300", "method": PaymentMethod.CASH.value},
            self.manager,
        )
        create_or_update_payment(
            self.session,
            second.id,
            {"amount": "450", "method": PaymentMethod.TRANSFER.value},
            self.manager,
        )

        report = get_accounting_report(self.session, period="day", selected_date=selected_date)

        self.assertEqual([row.order.id for row in report.rows], [second.id, first.id])
        self.assertEqual(report.summary.orders_count, 2)
        self.assertEqual(report.summary.orders_total, Decimal("850.00"))
        self.assertEqual(report.summary.paid_total, Decimal("750.00"))
        self.assertEqual(report.summary.pending_total, Decimal("100.00"))
        self.assertEqual(report.summary.market_expenses_total, Decimal("500.00"))
        self.assertEqual(report.summary.courier_pay_total, Decimal("350.00"))
        self.assertEqual(report.summary.profit_total, Decimal("-100.00"))
        self.assertEqual(report.expenses.kara, Decimal("15.00"))

    def test_month_report_uses_whole_month_and_excludes_archived_orders(self):
        selected_date = date(2026, 5, 15)
        may_first = self._create_order(
            date(2026, 5, 1),
            client_name="Первый Май",
            client_phone="+7 900 111-22-33",
            cube="100",
            base="50",
            courier_pay="50",
        )
        may_last = self._create_order(
            date(2026, 5, 31),
            client_name="Последний Май",
            client_phone="+7 900 222-33-44",
            loader="200",
            base="100",
            courier_pay="100",
        )
        archived = self._create_order(
            date(2026, 5, 20),
            client_name="Архив Май",
            client_phone="+7 900 333-44-55",
            storage="999",
            base="999",
            courier_pay="999",
        )
        archive_order(self.session, archived.id, self.admin)
        self._create_order(
            date(2026, 6, 1),
            client_name="Июнь",
            client_phone="+7 900 444-55-66",
            cube="500",
            base="500",
            courier_pay="500",
        )
        create_or_update_payment(
            self.session,
            may_first.id,
            {"amount": "150", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        report = get_accounting_report(self.session, period="month", selected_date=selected_date)

        self.assertEqual([row.order.id for row in report.rows], [may_last.id, may_first.id])
        self.assertEqual(report.summary.orders_count, 2)
        self.assertEqual(report.summary.orders_total, Decimal("450.00"))
        self.assertEqual(report.summary.paid_total, Decimal("150.00"))
        self.assertEqual(report.summary.pending_total, Decimal("300.00"))
        self.assertEqual(report.summary.market_expenses_total, Decimal("300.00"))
        self.assertEqual(report.summary.courier_pay_total, Decimal("150.00"))
        self.assertEqual(report.summary.profit_total, Decimal("-300.00"))

    def test_orders_without_delivery_date_do_not_enter_period(self):
        dated = self._create_order(
            date(2026, 5, 28),
            client_name="С датой",
            client_phone="+7 900 111-22-33",
            cube="100",
            base="100",
            courier_pay="100",
        )
        no_date = self._create_order(
            None,
            client_name="Без даты",
            client_phone="+7 900 222-33-44",
            cube="999",
            base="999",
            courier_pay="999",
        )
        create_or_update_payment(
            self.session,
            no_date.id,
            {"amount": "1998", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        report = get_accounting_report(self.session, period="day", selected_date=date(2026, 5, 28))

        self.assertEqual([row.order.id for row in report.rows], [dated.id])
        self.assertEqual(report.summary.orders_total, Decimal("200.00"))

    def test_pending_payment_status_label_is_not_paid(self):
        self.assertEqual(accounting_payment_status_label(PaymentStatus.PENDING), "Не оплачено")

    def test_partial_payment_is_shown_as_partial(self):
        order = self._create_order(
            date(2026, 5, 28),
            client_name="Частичная Оплата",
            client_phone="+7 900 111-22-33",
            base="200",
            courier_pay="100",
        )
        create_or_update_payment(
            self.session,
            order.id,
            {"amount": "50", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        report = get_accounting_report(self.session, period="day", selected_date=date(2026, 5, 28))

        self.assertEqual(report.rows[0].payment_status, PaymentDisplayStatus.PARTIAL)
        self.assertEqual(accounting_payment_status_label(report.rows[0].payment_status), "Частично")
        self.assertEqual(report.rows[0].pending_amount, Decimal("150.00"))

    def test_profit_subtracts_market_expenses_and_courier_pay(self):
        order = self._create_order(
            date(2026, 5, 28),
            client_name="Оплаченная",
            client_phone="+7 900 111-22-33",
            base="100",
            cube="100",
            courier_pay="100",
        )
        create_or_update_payment(
            self.session,
            order.id,
            {"amount": "200", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        report = get_accounting_report(self.session, period="day", selected_date=date(2026, 5, 28))

        self.assertEqual(report.rows[0].pending_amount, Decimal("0.00"))
        self.assertEqual(report.summary.pending_total, Decimal("0.00"))
        self.assertEqual(report.summary.profit_total, Decimal("0.00"))

    def test_manager_and_courier_are_forbidden_by_accounting_role_helper(self):
        dependency = require_roles(UserRole.ADMIN)

        for user in (self.manager, self.courier):
            with self.subTest(role=user.role.value):
                with self.assertRaises(HTTPException) as raised:
                    dependency(user=user)

                self.assertEqual(raised.exception.status_code, 403)

    def _create_order(
        self,
        delivery_date: date | None,
        *,
        client_name: str,
        client_phone: str,
        cube: str = "",
        loader: str = "",
        storage: str = "",
        kara: str = "",
        other: str = "",
        base: str = "",
        courier_pay: str = "",
    ):
        return create_order(
            self.session,
            {
                "client_name": client_name,
                "client_phone": client_phone,
                "address": f"Москва, {client_name}, 1",
                "delivery_date": delivery_date.isoformat() if delivery_date else "",
                "base_delivery_cost": base,
                "market_cube_cost": cube,
                "market_loader_cost": loader,
                "market_storage_cost": storage,
                "market_kara_cost": kara,
                "market_other_cost": other,
                "courier_pay": courier_pay,
            },
            self.manager,
        )


if __name__ == "__main__":
    unittest.main()
