from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, Numeric
from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import OrderStatus
from app.models.user import enum_values

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.log import OrderChangeLog
    from app.models.payment import Payment
    from app.models.user import User


class Order(Base):
    """Delivery order."""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_order_code", "order_code", unique=True),
        Index("ix_orders_status", "status"),
        Index("ix_orders_courier_id", "courier_id"),
        Index("ix_orders_client_id", "client_id"),
        Index("ix_orders_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_series: Mapped[str] = mapped_column(String(10), nullable=False)
    order_number: Mapped[int] = mapped_column(Integer, nullable=False)
    order_code: Mapped[str] = mapped_column(String(30), nullable=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    client_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    client_phone_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    delivery_date: Mapped[date | None] = mapped_column(Date)
    general_note: Mapped[str | None] = mapped_column(Text)
    cargo_number: Mapped[str | None] = mapped_column(String(100))
    cargo_phone: Mapped[str | None] = mapped_column(String(30))
    client_note: Mapped[str | None] = mapped_column(Text)
    staff_note: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    places_count: Mapped[int | None] = mapped_column(Integer)
    courier_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    courier_pay: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    base_delivery_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    market_cube_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    market_loader_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    market_storage_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    market_kara_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    market_other_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    delivery_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default="0",
    )
    status: Mapped[OrderStatus] = mapped_column(
        SQLEnum(OrderStatus, values_callable=enum_values, name="order_status"),
        nullable=False,
        default=OrderStatus.IN_WORK,
        server_default=OrderStatus.IN_WORK.value,
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    client: Mapped["Client"] = relationship(back_populates="orders")
    courier: Mapped["User | None"] = relationship(
        back_populates="courier_orders",
        foreign_keys=[courier_id],
    )
    created_by: Mapped["User | None"] = relationship(
        back_populates="created_orders",
        foreign_keys=[created_by_id],
    )
    archived_by: Mapped["User | None"] = relationship(
        back_populates="archived_orders",
        foreign_keys=[archived_by_id],
    )
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")
    change_logs: Mapped[list["OrderChangeLog"]] = relationship(back_populates="order")
