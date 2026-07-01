from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum as SQLEnum, ForeignKey, Index, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import CourierCashHandoverStatus
from app.models.user import enum_values

if TYPE_CHECKING:
    from app.models.user import User


class CourierCashHandover(Base):
    """Money handed over by a courier for a date or period."""

    __tablename__ = "courier_cash_handovers"
    __table_args__ = (
        Index("ix_courier_cash_handovers_courier_id", "courier_id"),
        Index("ix_courier_cash_handovers_status", "status"),
        Index("ix_courier_cash_handovers_period", "period_start", "period_end"),
        Index("ix_courier_cash_handovers_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    courier_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[CourierCashHandoverStatus] = mapped_column(
        SQLEnum(
            CourierCashHandoverStatus,
            values_callable=enum_values,
            name="courier_cash_handover_status",
        ),
        nullable=False,
        default=CourierCashHandoverStatus.PENDING,
        server_default=CourierCashHandoverStatus.PENDING.value,
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)

    courier: Mapped["User"] = relationship(foreign_keys=[courier_id])
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])
    confirmed_by: Mapped["User | None"] = relationship(foreign_keys=[confirmed_by_id])
