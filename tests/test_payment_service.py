from decimal import Decimal
from datetime import date, datetime, timezone
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.enums import PaymentDisplayStatus, PaymentMethod, PaymentStatus, UserRole
from app.models.log import OrderChangeLog
from app.models.payment import Payment
from app.models.user import User
from app.services.order_service import archive_order, create_order
from app.services.payment_service import (
    create_or_update_payment,
    create_quick_payments,
    delete_payment,
    list_payment_rows,
    payment_status_for_order,
    payment_status_label,
    update_payment,
)
from app.utils.auth import require_roles


class PaymentServiceTest(unittest.TestCase):
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

    def test_create_payment_defaults_to_order_delivery_cost_and_writes_log(self):
        order = self._create_order()

        payment = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        self.assertEqual(payment.order_id, order.id)
        self.assertEqual(payment.amount, Decimal("1350.50"))
        self.assertEqual(payment.method, PaymentMethod.CASH)
        self.assertEqual(payment.status, PaymentStatus.PAID)
        self.assertEqual(payment_status_for_order(order), PaymentDisplayStatus.PAID)

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "оплата"])
        self.assertIn("1350.50", logs[-1].new_value)
        self.assertIn("Наличными", logs[-1].new_value)
        self.assertIn("Менеджер", logs[-1].new_value)

    def test_partial_payments_are_allowed_until_remaining_amount(self):
        order = self._create_order()

        first = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "500", "method": PaymentMethod.CASH.value},
            self.manager,
        )
        self.assertEqual(payment_status_for_order(order), PaymentDisplayStatus.PARTIAL)
        self.assertEqual(payment_status_label(PaymentDisplayStatus.PARTIAL), "Частично")

        partial_rows = list_payment_rows(self.session, payment_status=PaymentDisplayStatus.PARTIAL.value)
        self.assertEqual([row.order.id for row in partial_rows.rows], [order.id])
        self.assertEqual(partial_rows.partial_count, 1)

        second = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "850.50", "method": PaymentMethod.TRANSFER.value},
            self.admin,
        )

        payments = self.session.scalars(select(Payment).order_by(Payment.id)).all()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(payments), 2)
        self.assertEqual([payment.amount for payment in payments], [Decimal("500.00"), Decimal("850.50")])
        self.assertEqual(second.method, PaymentMethod.TRANSFER)
        self.assertEqual(second.status, PaymentStatus.PAID)
        self.assertEqual(payment_status_for_order(order), PaymentDisplayStatus.PAID)

    def test_payment_cannot_exceed_remaining_amount_or_repeat_full_payment(self):
        order = self._create_order()

        with self.assertRaisesRegex(ValueError, "Сумма оплаты больше остатка по заявке\\."):
            create_or_update_payment(
                self.session,
                order.id,
                {"amount": "1350.51", "method": PaymentMethod.CASH.value},
                self.manager,
            )

        create_or_update_payment(
            self.session,
            order.id,
            {"amount": "1350.50", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        with self.assertRaisesRegex(ValueError, "Заявка уже оплачена\\."):
            create_or_update_payment(
                self.session,
                order.id,
                {"amount": "1", "method": PaymentMethod.CASH.value},
                self.manager,
            )

    def test_payment_rejects_zero_and_negative_amounts(self):
        order = self._create_order()

        with self.assertRaisesRegex(ValueError, "Укажите сумму оплаты\\."):
            create_or_update_payment(
                self.session,
                order.id,
                {"amount": "0", "method": PaymentMethod.CASH.value},
                self.manager,
            )

        with self.assertRaisesRegex(ValueError, "Сумма оплаты не может быть отрицательной\\."):
            create_or_update_payment(
                self.session,
                order.id,
                {"amount": "-1", "method": PaymentMethod.CASH.value},
                self.manager,
            )

        self.assertEqual(self.session.scalars(select(Payment)).all(), [])

    def test_quick_payment_can_split_cash_and_card_before_limit_check(self):
        order = self._create_order()

        payments = create_quick_payments(
            self.session,
            order.id,
            {"cash_amount": "400", "card_amount": "500.50"},
            self.manager,
        )

        self.assertEqual(len(payments), 2)
        self.assertEqual(
            [(payment.amount, payment.method) for payment in payments],
            [
                (Decimal("400.00"), PaymentMethod.CASH),
                (Decimal("500.50"), PaymentMethod.TRANSFER),
            ],
        )
        self.assertEqual(payment_status_for_order(order), PaymentDisplayStatus.PARTIAL)

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "оплата", "оплата"])
        self.assertIn("Наличными", logs[-2].new_value)
        self.assertIn("Карта", logs[-1].new_value)

    def test_quick_payment_rejects_empty_sum_and_total_overpayment_without_partial_write(self):
        order = self._create_order()

        with self.assertRaisesRegex(ValueError, "Укажите сумму оплаты\\."):
            create_quick_payments(
                self.session,
                order.id,
                {"cash_amount": "", "card_amount": ""},
                self.manager,
            )

        with self.assertRaisesRegex(ValueError, "Сумма оплаты больше остатка по заявке\\."):
            create_quick_payments(
                self.session,
                order.id,
                {"cash_amount": "1000", "card_amount": "400"},
                self.manager,
            )

        self.assertEqual(self.session.scalars(select(Payment)).all(), [])

    def test_payment_status_is_pending_without_payment(self):
        order = self._create_order()

        self.assertEqual(payment_status_for_order(order), PaymentDisplayStatus.PENDING)

    def test_pending_payment_status_label_is_not_paid(self):
        self.assertEqual(payment_status_label(PaymentStatus.PENDING), "Не оплачено")

    def test_admin_deletes_payment_and_writes_log(self):
        order = self._create_order()
        payment = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "1350.50", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        delete_payment(self.session, payment.id, self.admin)

        self.session.refresh(order)
        self.assertIsNone(self.session.get(Payment, payment.id))
        self.assertEqual(payment_status_for_order(order), PaymentDisplayStatus.PENDING)

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual(
            [log.action for log in logs],
            ["создание", "оплата", "удаление оплаты"],
        )
        self.assertIn("1350.50", logs[-1].old_value)
        self.assertIn("Админ", logs[-1].new_value)

    def test_admin_updates_payment_amount_and_method_without_overpayment(self):
        order = self._create_order()
        first = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "500", "method": PaymentMethod.CASH.value},
            self.manager,
        )
        second = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "300", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        updated = update_payment(
            self.session,
            first.id,
            {"amount": "700.50", "method": PaymentMethod.TRANSFER.value},
            self.admin,
            order_id=order.id,
        )

        self.assertEqual(updated.amount, Decimal("700.50"))
        self.assertEqual(updated.method, PaymentMethod.TRANSFER)
        self.session.refresh(order)
        self.assertEqual(payment_status_for_order(order), PaymentDisplayStatus.PARTIAL)

        rows = list_payment_rows(self.session, payment_status=PaymentDisplayStatus.PARTIAL.value)
        self.assertEqual([row.order.id for row in rows.rows], [order.id])
        self.assertEqual(rows.rows[0].paid_amount, Decimal("1000.50"))
        self.assertEqual(rows.rows[0].remaining_amount, Decimal("350.00"))
        self.assertEqual(self.session.get(Payment, second.id).amount, Decimal("300.00"))

        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual(
            [log.action for log in logs],
            ["создание", "оплата", "оплата", "изменение оплаты"],
        )
        self.assertIn("500.00", logs[-1].old_value)
        self.assertIn("Наличными", logs[-1].old_value)
        self.assertIn("700.50", logs[-1].new_value)
        self.assertIn("Карта", logs[-1].new_value)
        self.assertIn("Админ", logs[-1].new_value)

    def test_admin_update_payment_rejects_overpayment_and_wrong_order(self):
        order = self._create_order()
        other = self._create_order(client_phone="+7 900 222-33-44")
        first = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "500", "method": PaymentMethod.CASH.value},
            self.manager,
        )
        create_or_update_payment(
            self.session,
            order.id,
            {"amount": "700", "method": PaymentMethod.TRANSFER.value},
            self.manager,
        )

        with self.assertRaisesRegex(ValueError, "Сумма оплаты больше остатка по заявке\\."):
            update_payment(
                self.session,
                first.id,
                {"amount": "700", "method": PaymentMethod.CASH.value},
                self.admin,
                order_id=order.id,
            )

        with self.assertRaisesRegex(ValueError, "Оплата не относится к этой заявке\\."):
            update_payment(
                self.session,
                first.id,
                {"amount": "500", "method": PaymentMethod.CASH.value},
                self.admin,
                order_id=other.id,
            )

        self.session.refresh(first)
        self.assertEqual(first.amount, Decimal("500.00"))

    def test_admin_update_payment_rejects_zero_or_manager(self):
        order = self._create_order()
        payment = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "500", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        with self.assertRaisesRegex(ValueError, "Укажите сумму оплаты\\."):
            update_payment(
                self.session,
                payment.id,
                {"amount": "0", "method": PaymentMethod.CASH.value},
                self.admin,
                order_id=order.id,
            )

        with self.assertRaises(PermissionError):
            update_payment(
                self.session,
                payment.id,
                {"amount": "400", "method": PaymentMethod.TRANSFER.value},
                self.manager,
                order_id=order.id,
            )

        self.session.refresh(payment)
        self.assertEqual(payment.amount, Decimal("500.00"))
        self.assertEqual(payment.method, PaymentMethod.CASH)

    def test_manager_is_forbidden_by_payment_delete_role_helper(self):
        dependency = require_roles(UserRole.ADMIN)

        with self.assertRaises(HTTPException) as raised:
            dependency(user=self.manager)

        self.assertEqual(raised.exception.status_code, 403)

    def test_archived_orders_are_not_listed_for_payment(self):
        active = self._create_order(client_phone="+7 900 111-22-33")
        archived = self._create_order(client_phone="+7 900 222-33-44")
        archive_order(self.session, archived.id, self.admin)

        payment_rows = list_payment_rows(self.session)

        self.assertEqual([row.order.id for row in payment_rows.rows], [active.id])

    def test_payment_summary_detail_and_unpaid_all_time_rows(self):
        old_pending = self._create_order(
            client_phone="+7 900 100-00-01",
            delivery_date="2026-06-01",
        )
        paid = self._create_order(
            client_phone="+7 900 100-00-02",
            delivery_date="2026-06-02",
        )
        partial = self._create_order(
            client_phone="+7 900 100-00-03",
            delivery_date="2026-06-02",
        )
        new_pending = self._create_order(
            client_phone="+7 900 100-00-04",
            delivery_date="2026-06-03",
        )
        create_or_update_payment(
            self.session,
            paid.id,
            {"amount": "", "method": PaymentMethod.CASH.value},
            self.manager,
        )
        create_or_update_payment(
            self.session,
            partial.id,
            {"amount": "500", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        date_rows = list_payment_rows(self.session, delivery_date=date(2026, 6, 2))

        self.assertEqual(date_rows.shown_count, 2)
        self.assertEqual(date_rows.total_count, 2)
        self.assertEqual(date_rows.pending_count, 0)
        self.assertEqual(date_rows.partial_count, 1)
        self.assertEqual(date_rows.paid_count, 1)
        self.assertEqual(date_rows.delivery_cost_total, Decimal("2701.00"))
        self.assertEqual(date_rows.courier_pay_total, Decimal("600.00"))

        partial_detail = list_payment_rows(
            self.session,
            delivery_date=date(2026, 6, 2),
            detail=PaymentDisplayStatus.PARTIAL.value,
        )

        self.assertEqual(partial_detail.total_count, 2)
        self.assertEqual(partial_detail.shown_count, 1)
        self.assertEqual([row.order.id for row in partial_detail.rows], [partial.id])

        unpaid_detail = list_payment_rows(
            self.session,
            delivery_date=date(2026, 6, 2),
            detail="unpaid",
        )

        self.assertEqual(unpaid_detail.total_count, 2)
        self.assertEqual(unpaid_detail.shown_count, 1)
        self.assertEqual([row.order.id for row in unpaid_detail.rows], [partial.id])

        unpaid_rows = list_payment_rows(self.session, unpaid_all_time=True)

        self.assertEqual(unpaid_rows.total_count, 2)
        self.assertEqual(unpaid_rows.pending_count, 2)
        self.assertEqual([row.order.id for row in unpaid_rows.rows], [new_pending.id, old_pending.id])

    def test_paid_list_orders_latest_payment_first(self):
        older = self._create_order(
            client_phone="+7 900 200-00-01",
            delivery_date="2026-06-15",
        )
        newer = self._create_order(
            client_phone="+7 900 200-00-02",
            delivery_date="2026-06-01",
        )
        older_payment = create_or_update_payment(
            self.session,
            older.id,
            {"amount": "", "method": PaymentMethod.CASH.value},
            self.manager,
        )
        newer_payment = create_or_update_payment(
            self.session,
            newer.id,
            {"amount": "", "method": PaymentMethod.CASH.value},
            self.manager,
        )
        older_payment.paid_at = datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
        newer_payment.paid_at = datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
        self.session.commit()

        paid_rows = list_payment_rows(
            self.session,
            payment_status=PaymentDisplayStatus.PAID.value,
            detail=PaymentDisplayStatus.PAID.value,
        )

        self.assertEqual([row.order.id for row in paid_rows.rows], [newer.id, older.id])

    def test_courier_is_forbidden_by_payments_role_helper(self):
        dependency = require_roles(UserRole.ADMIN, UserRole.MANAGER)

        with self.assertRaises(HTTPException) as raised:
            dependency(user=self.courier)

        self.assertEqual(raised.exception.status_code, 403)

    def test_courier_cannot_create_payment_in_service(self):
        order = self._create_order()

        with self.assertRaises(PermissionError):
            create_or_update_payment(
                self.session,
                order.id,
                {"amount": "1350.50", "method": PaymentMethod.CASH.value},
                self.courier,
            )

        self.assertEqual(self.session.scalars(select(Payment)).all(), [])

    def test_manager_cannot_delete_payment_in_service(self):
        order = self._create_order()
        payment = create_or_update_payment(
            self.session,
            order.id,
            {"amount": "1350.50", "method": PaymentMethod.CASH.value},
            self.manager,
        )

        with self.assertRaises(PermissionError):
            delete_payment(self.session, payment.id, self.manager)

        self.assertIsNotNone(self.session.get(Payment, payment.id))

    def _create_order(
        self,
        *,
        client_phone: str = "+7 900 111-22-33",
        delivery_date: str = "",
    ):
        return create_order(
            self.session,
            {
                "client_name": "Иван Петров",
                "client_phone": client_phone,
                "address": "Москва, Тверская, 1",
                "delivery_date": delivery_date,
                "base_delivery_cost": "1000",
                "market_cube_cost": "100.10",
                "market_loader_cost": "200",
                "market_storage_cost": "",
                "market_other_cost": "50.40",
                "courier_pay": "300",
            },
            self.manager,
        )


if __name__ == "__main__":
    unittest.main()
