import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi import HTTPException, status
from starlette.requests import Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.enums import UserRole
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.routers import auth
from app.routers.auth import _demo_mode_enabled
from app.services.order_service import create_order
from app.templating import templates
from app.utils import seed_demo
from app.utils.security import hash_password


SHOWCASE_LOGIN_TEXT = "Открыть CRM"


def _request(method: str = "GET", path: str = "/", session: dict | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "session": session if session is not None else {},
        }
    )


def _render_response(response):
    if hasattr(response, "template") and not getattr(response, "body", b""):
        response.render(response.context)
    if not hasattr(response, "text"):
        response.text = response.body.decode(getattr(response, "charset", "utf-8"))
    return response


class DemoModeTest(unittest.TestCase):
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
        # reset_demo_data() работает через SessionLocal модуля seed_demo,
        # поэтому подменяем его на тестовую фабрику сессий.
        self._original_seed_session_local = seed_demo.SessionLocal
        seed_demo.SessionLocal = self.SessionLocal
        self._demo_mode = False
        # По умолчанию demo-mode выключен, как и в боевом/тестовом сеансе.
        self._set_demo_mode(False)

    def tearDown(self):
        app.dependency_overrides.clear()
        seed_demo.SessionLocal = self._original_seed_session_local
        templates.env.globals["demo_mode"] = bool(settings.demo_mode)
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _set_demo_mode(self, enabled: bool) -> None:
        self._demo_mode = enabled
        app.dependency_overrides[_demo_mode_enabled] = lambda: enabled
        templates.env.globals["demo_mode"] = enabled

    def test_demo_routes_and_buttons_hidden_when_demo_mode_off(self):
        response = _render_response(auth.login_form(_request(path="/login"), demo_mode=False))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SHOWCASE_LOGIN_TEXT, response.text)
        self.assertNotIn("Войти в демо", response.text)
        self.assertNotIn("Открыть кабинет курьера", response.text)
        self.assertNotIn('action="/demo-login/manager"', response.text)

        for role in ("manager", "courier"):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as raised:
                    auth._require_demo_mode(False)
                self.assertEqual(raised.exception.status_code, status.HTTP_404_NOT_FOUND)

    def test_demo_login_buttons_visible_when_demo_mode_on(self):
        self._set_demo_mode(True)

        response = _render_response(auth.login_form(_request(path="/login"), demo_mode=True))

        self.assertEqual(response.status_code, 200)
        self.assertIn(SHOWCASE_LOGIN_TEXT, response.text)
        self.assertNotIn("Войти в демо", response.text)
        self.assertIn("Открыть кабинет курьера", response.text)
        self.assertIn('action="/demo-login/manager"', response.text)
        self.assertIn('action="/demo-login/courier"', response.text)
        # Парольный вход в demo-mode свёрнут во вторичный блок.
        self.assertIn("Вход по паролю", response.text)
        # Реальный бренд и лого в интерфейсе не показываются.
        self.assertNotIn("img src", response.text)
        self.assertNotIn("courier-crm-logo", response.text)
        self.assertIn("Courier CRM", response.text)

    def test_demo_manager_login_works(self):
        self._set_demo_mode(True)
        session = {}

        with self.SessionLocal() as db:
            response = auth.demo_login("manager", _request("POST", "/demo-login/manager", session), db=db, _enabled=None)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        with self.SessionLocal() as db:
            user = db.get(User, session["user_id"])
            self.assertEqual(user.email, "manager@courier.local")
            self.assertEqual(user.role, UserRole.MANAGER)

    def test_demo_manager_login_resets_sqlite_demo_to_seed(self):
        self._set_demo_mode(True)
        seed_demo.seed_demo()
        with self.SessionLocal() as db:
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

        with self.SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(Order)).all()), 6)

        session = {}
        with (
            patch.object(auth, "settings", SimpleNamespace(demo_mode=True)),
            patch.object(auth, "is_sqlite", True),
            self.SessionLocal() as db,
        ):
            response = auth.demo_login("manager", _request("POST", "/demo-login/manager", session), db=db, _enabled=None)

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            orders = list(db.scalars(select(Order)).all())
            self.assertEqual(len(orders), 5)
            self.assertEqual(len(db.scalars(select(Payment)).all()), 3)
            self.assertNotIn("Москва, Временная улица, 99", [o.address for o in orders])
            user = db.get(User, session["user_id"])
            self.assertEqual(user.email, "manager@courier.local")

    def test_demo_courier_login_works(self):
        self._set_demo_mode(True)
        session = {}

        with self.SessionLocal() as db:
            response = auth.demo_login("courier", _request("POST", "/demo-login/courier", session), db=db, _enabled=None)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/courier")
        with self.SessionLocal() as db:
            user = db.get(User, session["user_id"])
            self.assertEqual(user.email, "courier@courier.local")
            self.assertEqual(user.role, UserRole.COURIER)

    def test_normal_login_still_works_in_demo_mode(self):
        self._set_demo_mode(True)
        session = {}

        with self.SessionLocal() as db:
            response = auth.login(
                _request("POST", "/login", session),
                email="manager@courier.local",
                password="manager123",
                db=db,
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("user_id", session)

    def test_normal_login_does_not_reset_demo_data(self):
        self._set_demo_mode(True)
        seed_demo.seed_demo()
        with self.SessionLocal() as db:
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

        session = {}
        with (
            patch.object(auth, "settings", SimpleNamespace(demo_mode=True)),
            patch.object(auth, "is_sqlite", True),
            self.SessionLocal() as db,
        ):
            response = auth.login(
                _request("POST", "/login", session),
                email="manager@courier.local",
                password="manager123",
                db=db,
            )

        self.assertEqual(response.status_code, 303)
        with self.SessionLocal() as db:
            orders = list(db.scalars(select(Order)).all())
            self.assertEqual(len(orders), 6)
            self.assertIn("Москва, Временная улица, 99", [o.address for o in orders])

    def test_normal_login_rejected_with_wrong_password(self):
        self._set_demo_mode(True)

        with self.SessionLocal() as db:
            response = auth.login(
                _request("POST", "/login"),
                email="manager@courier.local",
                password="wrong",
                db=db,
            )
        response = _render_response(response)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Неверная почта или пароль.", response.text)

    def test_demo_login_unknown_role_returns_404(self):
        self._set_demo_mode(True)

        for role in ("admin", "superuser", "manager1"):
            with self.subTest(role=role):
                with self.SessionLocal() as db, self.assertRaises(HTTPException) as raised:
                    auth.demo_login(role, _request("POST", f"/demo-login/{role}"), db=db, _enabled=None)
                self.assertEqual(raised.exception.status_code, status.HTTP_404_NOT_FOUND)

    def test_demo_banner_and_reset_ui_are_not_in_base_template(self):
        with open("app/templates/base.html", encoding="utf-8") as file:
            base_template = file.read()

        self.assertNotIn("Данные вымышленные", base_template)
        self.assertNotIn("могут сбрасываться", base_template)
        self.assertNotIn("Сбросить данные", base_template)
        self.assertNotIn('data-demo-banner', base_template)
        self.assertNotIn("Данные сброшены к исходному состоянию.", base_template)

    def test_demo_reset_unavailable_when_demo_mode_off(self):
        with self.assertRaises(HTTPException) as raised:
            auth._require_demo_mode(False)

        self.assertEqual(raised.exception.status_code, status.HTTP_404_NOT_FOUND)

    def test_demo_reset_restores_seed_data(self):
        seed_demo.seed_demo()
        # Посетитель создаёт лишнюю заявку поверх seed-набора.
        with self.SessionLocal() as db:
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
        with self.SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(Order)).all()), 6)

        seed_demo.reset_demo_data()

        with self.SessionLocal() as db:
            orders = list(db.scalars(select(Order)).all())
            self.assertEqual(len(orders), 5)
            self.assertEqual(len(db.scalars(select(Payment)).all()), 3)
            self.assertNotIn("Москва, Временная улица, 99", [o.address for o in orders])

    def _seed_users(self):
        users = [
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


if __name__ == "__main__":
    unittest.main()
