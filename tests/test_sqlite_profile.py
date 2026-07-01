import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from app.database import Base, SessionLocal, engine, is_sqlite, is_sqlite_database
import app.main as main_module
from app.main import bootstrap_sqlite_demo
from app.models.client import Client
from app.models.order import Order
from app.models.user import User
from app.services.order_service import create_order


class SqliteProfileTest(unittest.TestCase):
    def setUp(self):
        # Тесты работают на общем модульном in-memory движке (StaticPool).
        # Чистим таблицы, чтобы порядок тестов не влиял на счётчики.
        Base.metadata.drop_all(engine)

    def test_url_classification(self):
        self.assertTrue(is_sqlite_database("sqlite:///./demo.db"))
        self.assertTrue(is_sqlite_database("sqlite:////data/demo.db"))
        self.assertTrue(is_sqlite_database("sqlite:///:memory:"))
        self.assertTrue(is_sqlite_database("sqlite://"))
        self.assertFalse(is_sqlite_database("postgresql+psycopg://u:p@h/db"))

    def test_configured_engine_is_sqlite_with_in_memory_pool(self):
        # В тестовом окружении DATABASE_URL=sqlite:// (in-memory).
        self.assertTrue(is_sqlite)
        # In-memory SQLite должен идти через StaticPool, иначе пул расколет базу.
        self.assertIsInstance(engine.pool, StaticPool)

    def test_bootstrap_creates_tables_and_seeds_on_sqlite(self):
        bootstrap_sqlite_demo(seed=True)

        with SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(Order)).all()), 5)

    def test_bootstrap_without_seed_only_creates_tables(self):
        bootstrap_sqlite_demo(seed=False)

        with SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(Order)).all()), 0)

    def test_bootstrap_is_idempotent(self):
        bootstrap_sqlite_demo(seed=True)
        bootstrap_sqlite_demo(seed=True)

        with SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(Order)).all()), 5)

    def test_demo_mode_bootstrap_seeds_empty_sqlite_database(self):
        with patch.object(main_module, "settings", SimpleNamespace(seed_demo=False, demo_mode=True)):
            bootstrap_sqlite_demo()

        with SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(User)).all()), 3)
            self.assertEqual(len(db.scalars(select(Order)).all()), 5)

    def test_demo_mode_bootstrap_does_not_duplicate_existing_seed(self):
        with patch.object(main_module, "settings", SimpleNamespace(seed_demo=False, demo_mode=True)):
            bootstrap_sqlite_demo()
            bootstrap_sqlite_demo()

        with SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(User)).all()), 3)
            self.assertEqual(len(db.scalars(select(Client)).all()), 4)
            self.assertEqual(len(db.scalars(select(Order)).all()), 5)

    def test_demo_mode_bootstrap_does_not_delete_existing_data(self):
        with patch.object(main_module, "settings", SimpleNamespace(seed_demo=False, demo_mode=True)):
            bootstrap_sqlite_demo()

            with SessionLocal() as db:
                manager = db.scalar(select(User).where(User.email == "manager@courier.local"))
                create_order(
                    db,
                    {
                        "client_name": "Временный Клиент",
                        "client_phone": "+7 900 999-00-00",
                        "address": "Москва, Временная улица, 99",
                    },
                    manager,
                )

            bootstrap_sqlite_demo()

        with SessionLocal() as db:
            orders = list(db.scalars(select(Order)).all())
            self.assertEqual(len(orders), 6)
            self.assertIn("Москва, Временная улица, 99", [order.address for order in orders])


if __name__ == "__main__":
    unittest.main()
