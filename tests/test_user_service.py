import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.enums import UserRole
from app.models.user import User
from app.services.user_service import create_user, list_users, update_user
from app.utils.security import verify_password


class UserServiceTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()

    def tearDown(self):
        self.session.close()

    def test_create_user_hashes_password_and_normalizes_email(self):
        user = create_user(
            self.session,
            {
                "full_name": "Мария Менеджер",
                "email": " MANAGER@courier.local ",
                "phone": "+375291234567",
                "role": UserRole.MANAGER.value,
                "password": "manager123",
                "is_active": "on",
            },
        )

        self.assertEqual(user.email, "manager@courier.local")
        self.assertEqual(user.phone, "+375291234567")
        self.assertEqual(user.role, UserRole.MANAGER)
        self.assertTrue(user.is_active)
        self.assertTrue(verify_password("manager123", user.password_hash))
        self.assertNotEqual(user.password_hash, "manager123")

    def test_create_user_accepts_plain_login_without_at_sign(self):
        user = create_user(
            self.session,
            {
                "full_name": "Мария Менеджер",
                "email": " ManagerOne ",
                "phone": "+375291234567",
                "role": UserRole.MANAGER.value,
                "password": "manager123",
                "is_active": "on",
            },
        )

        self.assertEqual(user.email, "managerone")
        self.assertEqual(user.phone, "+375291234567")
        self.assertTrue(verify_password("manager123", user.password_hash))

    def test_update_user_changes_password_only_when_field_is_filled(self):
        user = create_user(
            self.session,
            {
                "full_name": "Игорь Курьер",
                "email": "courier@courier.local",
                "phone": "",
                "role": UserRole.COURIER.value,
                "password": "courier123",
                "is_active": "on",
            },
        )
        old_hash = user.password_hash

        updated = update_user(
            self.session,
            user.id,
            {
                "full_name": "Игорь Курьер",
                "phone": "+7 999 100-00-03",
                "role": UserRole.COURIER.value,
                "is_active": "",
                "password": "",
            },
        )

        self.assertEqual(updated.password_hash, old_hash)
        self.assertFalse(updated.is_active)
        self.assertEqual(updated.phone, "+79991000003")

        updated = update_user(
            self.session,
            user.id,
            {
                "full_name": "Игорь Курьер",
                "phone": "+375291234568",
                "role": UserRole.COURIER.value,
                "is_active": "on",
                "password": "newpass123",
            },
        )

        self.assertTrue(updated.is_active)
        self.assertEqual(updated.phone, "+375291234568")
        self.assertTrue(verify_password("newpass123", updated.password_hash))

    def test_user_phone_requires_plus_and_digits(self):
        with self.assertRaisesRegex(ValueError, "Формат: \\+375291234567\\."):
            create_user(
                self.session,
                {
                    "full_name": "Менеджер без плюса",
                    "email": "bad-phone@courier.local",
                    "phone": "375291234567",
                    "role": UserRole.MANAGER.value,
                    "password": "manager123",
                },
            )

    def test_list_users_orders_by_role_and_name(self):
        create_user(
            self.session,
            {
                "full_name": "Курьер",
                "email": "courier@courier.local",
                "role": UserRole.COURIER.value,
                "password": "pass123",
            },
        )
        create_user(
            self.session,
            {
                "full_name": "Админ",
                "email": "admin@courier.local",
                "role": UserRole.ADMIN.value,
                "password": "pass123",
            },
        )

        self.assertEqual([user.full_name for user in list_users(self.session)], ["Админ", "Курьер"])


if __name__ == "__main__":
    unittest.main()
