from enum import Enum


class UserRole(str, Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    COURIER = "courier"


class OrderStatus(str, Enum):
    IN_WORK = "in_work"
    AT_COURIER = "at_courier"
    DELIVERED = "delivered"


class PaymentMethod(str, Enum):
    CASH = "cash"
    TRANSFER = "transfer"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"


class PaymentDisplayStatus(str, Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    PAID = "paid"


class CourierCashHandoverStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
