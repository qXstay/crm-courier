from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SQLEnum, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.log import OrderChangeLog
    from app.models.order import Order
    from app.models.payment import Payment


def enum_values(enum_class: type[UserRole]) -> list[str]:
    return [item.value for item in enum_class]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(30), index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SQLEnum(UserRole, values_callable=enum_values, name="user_role"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    created_clients: Mapped[list["Client"]] = relationship(back_populates="created_by")
    courier_orders: Mapped[list["Order"]] = relationship(
        back_populates="courier",
        foreign_keys="Order.courier_id",
    )
    created_orders: Mapped[list["Order"]] = relationship(
        back_populates="created_by",
        foreign_keys="Order.created_by_id",
    )
    archived_orders: Mapped[list["Order"]] = relationship(
        back_populates="archived_by",
        foreign_keys="Order.archived_by_id",
    )
    created_payments: Mapped[list["Payment"]] = relationship(back_populates="created_by")
    order_change_logs: Mapped[list["OrderChangeLog"]] = relationship(
        back_populates="user"
    )
