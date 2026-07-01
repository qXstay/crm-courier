from datetime import date
from decimal import Decimal
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.enums import CourierCashHandoverStatus, PaymentMethod, UserRole
from app.models.user import User
from app.services.courier_cash_handover_service import (
    calculate_period_money,
    confirm_handover,
    create_handover,
    list_period_handovers,
    reject_handover,
)
from app.services.order_service import create_order
from app.services.payment_service import create_quick_payments


class CourierCashHandoverServiceTest(unittest.TestCase):
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
        self.other_courier = User(
            full_name="Другой Курьер",
            email="other-courier@example.com",
            password_hash="hash",
            role=UserRole.COURIER,
            is_active=True,
        )
        self.session.add_all([self.manager, self.admin, self.courier, self.other_courier])
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_period_money_uses_only_cash_for_due_to_office(self):
        selected_date = date(2026, 6, 5)
        first = self._create_order(selected_date, base="1000", courier_pay="300")
        second = self._create_order(selected_date, base="700", courier_pay="200")
        self._create_order(selected_date, base="999", courier_pay="999", courier=self.other_courier)
        self._create_order(None, base="999", courier_pay="999")

        create_quick_payments(
            self.session,
            first.id,
            {"cash_amount": "1000", "card_amount": ""},
            self.manager,
        )
        create_quick_payments(
            self.session,
            second.id,
            {"cash_amount": "", "card_amount": "500"},
            self.manager,
        )
        confirmed = create_handover(
            self.session,
            courier=self.courier,
            amount_value="100",
            period_start=selected_date,
            period_end=selected_date,
        )
        confirm_handover(self.session, confirmed.id, self.admin)
        create_handover(
            self.session,
            courier=self.courier,
            amount_value="50",
            period_start=selected_date,
            period_end=selected_date,
        )

        money = calculate_period_money(
            self.session,
            courier_id=self.courier.id,
            period_start=selected_date,
            period_end=selected_date,
        )

        self.assertEqual(money.total_delivery, Decimal("1700.00"))
        self.assertEqual(money.paid_total, Decimal("1500.00"))
        self.assertEqual(money.cash_total, Decimal("1000.00"))
        self.assertEqual(money.courier_pay_total, Decimal("500.00"))
        self.assertEqual(money.paid_courier_pay_total, Decimal("500.00"))
        self.assertEqual(money.confirmed_total, Decimal("100.00"))
        self.assertEqual(money.pending_total, Decimal("50.00"))
        self.assertEqual(money.due_to_office, Decimal("350.00"))

    def test_period_money_counts_courier_pay_only_after_payment(self):
        selected_date = date(2026, 6, 5)
        paid = self._create_order(selected_date, base="1000", courier_pay="300")
        self._create_order(selected_date, base="700", courier_pay="200")

        create_quick_payments(
            self.session,
            paid.id,
            {"cash_amount": "1000", "card_amount": ""},
            self.manager,
        )

        money = calculate_period_money(
            self.session,
            courier_id=self.courier.id,
            period_start=selected_date,
            period_end=selected_date,
        )

        self.assertEqual(money.courier_pay_total, Decimal("500.00"))
        self.assertEqual(money.paid_courier_pay_total, Decimal("300.00"))

    def test_due_to_office_never_goes_below_zero(self):
        selected_date = date(2026, 6, 5)
        order = self._create_order(selected_date, base="200", courier_pay="300")
        create_quick_payments(
            self.session,
            order.id,
            {"cash_amount": "200", "card_amount": ""},
            self.manager,
        )

        money = calculate_period_money(
            self.session,
            courier_id=self.courier.id,
            period_start=selected_date,
            period_end=selected_date,
        )

        self.assertEqual(money.due_to_office, Decimal("0.00"))

    def test_handover_validation_and_processing_rules(self):
        selected_date = date(2026, 6, 5)

        for amount in ("", "0", "-1", "abc"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    create_handover(
                        self.session,
                        courier=self.courier,
                        amount_value=amount,
                        period_start=selected_date,
                        period_end=selected_date,
                    )

        with self.assertRaises(PermissionError):
            create_handover(
                self.session,
                courier=self.manager,
                amount_value="100",
                period_start=selected_date,
                period_end=selected_date,
            )

        handover = create_handover(
            self.session,
            courier=self.courier,
            amount_value="100",
            period_start=selected_date,
            period_end=selected_date,
        )
        confirmed = confirm_handover(self.session, handover.id, self.admin, comment="ок")

        self.assertEqual(confirmed.status, CourierCashHandoverStatus.CONFIRMED)
        self.assertEqual(confirmed.confirmed_by_id, self.admin.id)
        self.assertEqual(confirmed.comment, "ок")
        with self.assertRaisesRegex(ValueError, "Эта сдача уже обработана"):
            reject_handover(self.session, handover.id, self.admin)

    def test_rejected_handover_does_not_count_as_pending_or_confirmed(self):
        selected_date = date(2026, 6, 5)
        handover = create_handover(
            self.session,
            courier=self.courier,
            amount_value="100",
            period_start=selected_date,
            period_end=selected_date,
        )

        reject_handover(self.session, handover.id, self.admin, comment="не сошлось")
        money = calculate_period_money(
            self.session,
            courier_id=self.courier.id,
            period_start=selected_date,
            period_end=selected_date,
        )
        handovers = list_period_handovers(
            self.session,
            courier_id=self.courier.id,
            period_start=selected_date,
            period_end=selected_date,
        )

        self.assertEqual(money.confirmed_total, Decimal("0.00"))
        self.assertEqual(money.pending_total, Decimal("0.00"))
        self.assertEqual(handovers[0].status, CourierCashHandoverStatus.REJECTED)

    def _create_order(
        self,
        delivery_date: date | None,
        *,
        base: str,
        courier_pay: str,
        courier: User | None = None,
    ):
        courier = courier or self.courier
        return create_order(
            self.session,
            {
                "client_name": f"Клиент {base}",
                "client_phone": f"+7 900 111-{int(base) % 90 + 10:02d}-{int(courier_pay) % 90 + 10:02d}",
                "address": f"Москва, Тестовая, {base}",
                "delivery_date": delivery_date.isoformat() if delivery_date else "",
                "courier_id": str(courier.id),
                "base_delivery_cost": base,
                "courier_pay": courier_pay,
            },
            self.manager,
        )


if __name__ == "__main__":
    unittest.main()
