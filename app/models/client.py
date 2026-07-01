from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.user import User


class Client(Base):
    """Client profile.

    Business rule: when creating a client, matching by full name and phone should
    show a warning but must not block creation.
    """

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    created_by: Mapped["User | None"] = relationship(back_populates="created_clients")
    orders: Mapped[list["Order"]] = relationship(back_populates="client")
