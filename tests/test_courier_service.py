from datetime import date, timedelta
from decimal import Decimal
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.enums import OrderStatus, UserRole
from app.models.log import OrderChangeLog
from app.models.order import Order
from app.models.user import User
from app.services.courier_service import (
    get_courier_dashboard,
    get_courier_route,
    get_route_summary,
)
from app.services.order_service import archive_order, create_order, mark_order_delivered_by_courier


class CourierServiceTest(unittest.TestCase):
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
            full_name="Игорь Курьер",
            email="courier@example.com",
            password_hash="hash",
            role=UserRole.COURIER,
            is_active=True,
        )
        self.other_courier = User(
            full_name="Анна Курьер",
            email="anna@example.com",
            password_hash="hash",
            role=UserRole.COURIER,
            is_active=True,
        )
        self.session.add_all([self.manager, self.admin, self.courier, self.other_courier])
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_dashboard_returns_working_orders_daily_summary_and_month_earnings(self):
        today = date(2026, 6, 2)
        own_today = self._create_order(
            "Иван Петров",
            "+7 900 111-22-33",
            self.courier,
            today,
            delivery_cost_parts=("100", "200"),
        )
        own_tomorrow = self._create_order(
            "Анна Соколова",
            "+7 900 222-33-44",
            self.courier,
            today + timedelta(days=1),
            delivery_cost_parts=("300", "400"),
        )
        own_without_date = self._create_order(
            "Без Даты",
            "+7 900 999-88-77",
            self.courier,
            None,
            delivery_cost_parts=("500", "600"),
        )
        delivered_today = self._create_order(
            "Доставлен Сегодня",
            "+7 900 777-88-99",
            self.courier,
            today,
            delivery_cost_parts=("700", "800"),
        )
        delivered_today.status = OrderStatus.DELIVERED
        delivered_this_month = self._create_order(
            "Доставлен В Месяце",
            "+7 900 666-77-88",
            self.courier,
            date(2026, 6, 1),
            delivery_cost_parts=("900", "1000"),
        )
        delivered_this_month.status = OrderStatus.DELIVERED
        self._create_order(
            "Чужой Клиент",
            "+7 900 333-44-55",
            self.other_courier,
            today,
            delivery_cost_parts=("500", "600"),
        )
        archived = self._create_order(
            "Архив Клиент",
            "+7 900 444-55-66",
            self.courier,
            today,
            delivery_cost_parts=("700", "800"),
        )
        archived.is_archived = True
        self.session.commit()

        dashboard = get_courier_dashboard(self.session, self.courier, today=today)

        self.assertEqual(
            [order.id for order in dashboard.working_orders],
            [own_tomorrow.id, own_today.id, own_without_date.id],
        )
        self.assertEqual([order.id for order in dashboard.delivered_orders], [delivered_today.id])
        self.assertEqual(dashboard.today_orders_count, 2)
        self.assertEqual(dashboard.delivered_today_count, 1)
        self.assertEqual(dashboard.working_today_count, 1)
        self.assertEqual(dashboard.today_delivery_total, Decimal("1800.00"))
        self.assertEqual(dashboard.today_courier_pay_total, Decimal("1000.00"))
        self.assertEqual(dashboard.delivered_today_delivery_total, Decimal("1500.00"))
        self.assertEqual(dashboard.delivered_today_courier_pay_total, Decimal("800.00"))
        self.assertEqual(dashboard.working_today_delivery_total, Decimal("300.00"))
        self.assertEqual(dashboard.working_today_courier_pay_total, Decimal("200.00"))
        self.assertEqual(dashboard.month_courier_pay_total, Decimal("1800.00"))
        self.assertEqual(dashboard.detail, "")
        self.assertEqual(dashboard.detail_orders, [])

    def test_dashboard_detail_lists_assigned_or_delivered_orders_for_selected_period(self):
        today = date(2026, 6, 8)
        in_work = self._create_order(
            "В работе",
            "+7 900 101-10-10",
            self.courier,
            today,
            delivery_cost_parts=("100", "200"),
        )
        in_work.status = OrderStatus.IN_WORK
        delivered = self._create_order(
            "Доставлен",
            "+7 900 202-20-20",
            self.courier,
            today,
            delivery_cost_parts=("300", "400"),
        )
        delivered.status = OrderStatus.DELIVERED
        other = self._create_order(
            "Чужой",
            "+7 900 303-30-30",
            self.other_courier,
            today,
            delivery_cost_parts=("500", "600"),
        )
        other.status = OrderStatus.DELIVERED
        self.session.commit()

        assigned = get_courier_dashboard(
            self.session,
            self.courier,
            today=today,
            detail="assigned",
        )
        delivered_detail = get_courier_dashboard(
            self.session,
            self.courier,
            today=today,
            detail="delivered",
        )

        self.assertEqual(assigned.detail_title, "Всего заявок назначено")
        self.assertEqual([order.id for order in assigned.detail_orders], [delivered.id, in_work.id])
        self.assertEqual(delivered_detail.detail_title, "Доставлено")
        self.assertEqual([order.id for order in delivered_detail.detail_orders], [delivered.id])
        self.assertNotIn(other.id, [order.id for order in assigned.detail_orders])

    def test_route_filters_selected_courier_by_route_date(self):
        route_date = date.today()
        first = self._create_order(
            "Иван Петров",
            "+7 900 111-22-33",
            self.courier,
            route_date,
            delivery_cost_parts=("100", "200"),
        )
        self._create_order(
            "Завтра Клиент",
            "+7 900 555-66-77",
            self.courier,
            route_date + timedelta(days=1),
            delivery_cost_parts=("300", "400"),
        )
        archived = self._create_order(
            "Архив Клиент",
            "+7 900 444-55-66",
            self.courier,
            route_date,
            delivery_cost_parts=("700", "800"),
        )
        archive_order(self.session, archived.id, self.admin)
        self._create_order(
            "Чужой Клиент",
            "+7 900 333-44-55",
            self.other_courier,
            route_date,
            delivery_cost_parts=("500", "600"),
        )

        route = get_courier_route(self.session, self.courier.id, route_date)

        self.assertIsNotNone(route.courier)
        self.assertEqual([order.id for order in route.orders], [first.id])
        self.assertEqual(route.orders_count, 1)
        self.assertEqual(route.delivery_total, Decimal("300.00"))
        self.assertEqual(route.courier_pay_total, Decimal("200.00"))

    def test_route_summary_defaults_to_today_and_filters_details(self):
        today = date(2026, 6, 8)
        in_work = self._create_order(
            "В работе",
            "+7 900 101-10-10",
            self.courier,
            today,
            delivery_cost_parts=("100", "200"),
        )
        in_work.status = OrderStatus.IN_WORK
        at_courier = self._create_order(
            "У Курьера",
            "+7 900 202-20-20",
            self.courier,
            today,
            delivery_cost_parts=("300", "400"),
        )
        at_courier.status = OrderStatus.AT_COURIER
        delivered = self._create_order(
            "Доставлен",
            "+7 900 303-30-30",
            self.other_courier,
            today,
            delivery_cost_parts=("500", "600"),
        )
        delivered.status = OrderStatus.DELIVERED
        tomorrow = self._create_order(
            "Завтра",
            "+7 900 404-40-40",
            self.other_courier,
            today + timedelta(days=1),
            delivery_cost_parts=("700", "800"),
        )
        archived = self._create_order(
            "Архив",
            "+7 900 505-50-50",
            self.courier,
            today,
            delivery_cost_parts=("900", "1000"),
        )
        archived.status = OrderStatus.DELIVERED
        self.session.commit()
        archive_order(self.session, archived.id, self.admin)

        summary = get_route_summary(self.session, today=today)

        self.assertEqual(summary.route_date, today)
        self.assertEqual(summary.courier_id, "all")
        self.assertEqual(summary.orders_count, 3)
        self.assertEqual(summary.in_work_count, 1)
        self.assertEqual(summary.at_courier_count, 1)
        self.assertEqual(summary.delivered_count, 1)
        self.assertEqual(summary.couriers_count, 2)
        self.assertEqual(summary.delivery_total, Decimal("2100.00"))
        self.assertEqual(summary.courier_pay_total, Decimal("1200.00"))
        self.assertNotIn(tomorrow.id, [order.id for order in summary.orders])
        self.assertNotIn(archived.id, [order.id for order in summary.orders])

        courier_detail = get_route_summary(self.session, route_date=today, detail="couriers")
        self.assertEqual(
            [item.courier.id for item in courier_detail.detail_couriers],
            [self.other_courier.id, self.courier.id],
        )
        self.assertEqual(courier_detail.detail_orders, [])

        all_detail = get_route_summary(self.session, route_date=today, detail="all")
        self.assertEqual([order.id for order in all_detail.detail_orders], [in_work.id, at_courier.id, delivered.id])

        in_work_detail = get_route_summary(self.session, route_date=today, detail="in_work")
        self.assertEqual([order.id for order in in_work_detail.detail_orders], [in_work.id])

        at_courier_detail = get_route_summary(self.session, route_date=today, detail="at_courier")
        self.assertEqual([order.id for order in at_courier_detail.detail_orders], [at_courier.id])

        delivered_detail = get_route_summary(self.session, route_date=today, detail="delivered")
        self.assertEqual([order.id for order in delivered_detail.detail_orders], [delivered.id])

    def test_route_summary_filters_specific_courier_and_selected_date(self):
        route_date = date(2026, 6, 8)
        selected = self._create_order(
            "Выбранный",
            "+7 900 606-60-60",
            self.courier,
            route_date,
            delivery_cost_parts=("100", "200"),
        )
        other = self._create_order(
            "Другой Курьер",
            "+7 900 707-70-70",
            self.other_courier,
            route_date,
            delivery_cost_parts=("300", "400"),
        )
        next_day = self._create_order(
            "Другая Дата",
            "+7 900 808-80-80",
            self.courier,
            route_date + timedelta(days=1),
            delivery_cost_parts=("500", "600"),
        )
        self.session.commit()

        summary = get_route_summary(
            self.session,
            route_date=route_date,
            courier_id=str(self.courier.id),
            detail="all",
        )

        self.assertEqual(summary.courier, self.courier)
        self.assertEqual(summary.courier_id, str(self.courier.id))
        self.assertEqual(summary.orders_count, 1)
        self.assertEqual([order.id for order in summary.detail_orders], [selected.id])
        self.assertNotIn(other.id, [order.id for order in summary.orders])
        self.assertNotIn(next_day.id, [order.id for order in summary.orders])

    def test_courier_marks_own_order_delivered_and_it_leaves_working_dashboard(self):
        order = self._create_order(
            "Иван Петров",
            "+7 900 111-22-33",
            self.courier,
            date.today(),
            delivery_cost_parts=("100", "200"),
        )

        delivered = mark_order_delivered_by_courier(self.session, order.id, self.courier)

        self.assertEqual(delivered.status, OrderStatus.DELIVERED)
        dashboard = get_courier_dashboard(self.session, self.courier)
        self.assertEqual(dashboard.working_orders, [])
        self.assertEqual([item.id for item in dashboard.delivered_orders], [order.id])
        logs = self.session.scalars(
            select(OrderChangeLog).order_by(OrderChangeLog.id)
        ).all()
        self.assertEqual([log.action for log in logs], ["создание", "доставлено"])
        self.assertIn('"status": "delivered"', logs[-1].new_value)

    def test_courier_cannot_mark_another_courier_order_delivered(self):
        order = self._create_order(
            "Чужой Клиент",
            "+7 900 333-44-55",
            self.other_courier,
            date.today(),
            delivery_cost_parts=("100", "200"),
        )

        with self.assertRaisesRegex(ValueError, "Заявка не найдена"):
            mark_order_delivered_by_courier(self.session, order.id, self.courier)

        self.session.refresh(order)
        self.assertEqual(order.status, OrderStatus.AT_COURIER)

    def _create_order(
        self,
        client_name: str,
        client_phone: str,
        courier: User,
        delivery_date: date | None,
        *,
        delivery_cost_parts: tuple[str, str],
    ) -> Order:
        market_cost, courier_pay = delivery_cost_parts
        order = create_order(
            self.session,
            {
                "client_name": client_name,
                "client_phone": client_phone,
                "address": f"Москва, {client_name}, 1",
                "delivery_date": delivery_date.isoformat() if delivery_date else "",
                "courier_id": str(courier.id),
                "base_delivery_cost": courier_pay,
                "market_cube_cost": market_cost,
                "courier_pay": courier_pay,
            },
            self.manager,
        )
        if delivery_date is None:
            order.delivery_date = None
            self.session.commit()
            self.session.refresh(order)
        return order


if __name__ == "__main__":
    unittest.main()
