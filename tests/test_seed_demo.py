from datetime import date
from decimal import Decimal
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.enums import PaymentStatus, UserRole
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.accounting_service import get_accounting_report
from app.utils import seed_demo


class DemoSeedTest(unittest.TestCase):
    def setUp(self):
        self.original_session_local = seed_demo.SessionLocal
        self.engine = create_engine("sqlite:///:memory:")
        self.SessionLocal = sessionmaker(bind=self.engine)
        seed_demo.SessionLocal = self.SessionLocal

    def tearDown(self):
        seed_demo.SessionLocal = self.original_session_local
        self.engine.dispose()

    def test_seed_is_repeatable_and_keeps_demo_dataset_showcase_ready(self):
        seed_demo.seed_demo()
        with self.SessionLocal() as db:
            first_counts = self._counts(db)

        seed_demo.seed_demo()
        with self.SessionLocal() as db:
            second_counts = self._counts(db)
            active_orders = db.scalars(
                select(Order).where(Order.is_archived.is_(False)).order_by(Order.order_code)
            ).all()
            archived_orders = db.scalars(
                select(Order).where(Order.is_archived.is_(True))
            ).all()
            courier = db.scalar(
                select(User).where(
                    User.email == "courier@courier.local",
                    User.role == UserRole.COURIER,
                )
            )
            paid_payments = db.scalars(
                select(Payment).where(Payment.status == PaymentStatus.PAID)
            ).all()
            report = get_accounting_report(db, period="day", selected_date=date.today())

        self.assertEqual(second_counts, first_counts)
        self.assertEqual(second_counts["orders"], 5)
        self.assertEqual(second_counts["payments"], 3)
        self.assertEqual(len(active_orders), 4)
        self.assertEqual(len(archived_orders), 1)
        self.assertEqual(
            sum(1 for order in active_orders if order.courier_id == courier.id),
            2,
        )
        self.assertEqual(len(paid_payments), 3)
        self.assertEqual(report.summary.orders_count, 4)
        self.assertEqual(report.summary.orders_total, Decimal("7300.00"))
        self.assertEqual(report.summary.paid_total, Decimal("5700.00"))
        self.assertEqual(report.summary.pending_total, Decimal("1600.00"))
        self.assertEqual(report.summary.market_expenses_total, Decimal("3250.00"))
        self.assertEqual(report.summary.courier_pay_total, Decimal("1300.00"))
        self.assertEqual(report.summary.profit_total, Decimal("1150.00"))
        self.assertEqual(report.expenses.kara, Decimal("240.00"))

    def test_seed_creates_tables_on_empty_sqlite_database(self):
        seed_demo.seed_demo()

        with self.SessionLocal() as db:
            self.assertEqual(self._counts(db)["users"], 3)

    def _counts(self, db):
        return {
            "users": len(db.scalars(select(User)).all()),
            "orders": len(db.scalars(select(Order)).all()),
            "payments": len(db.scalars(select(Payment)).all()),
        }


if __name__ == "__main__":
    unittest.main()
