import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import UserRole
from app.models.user import User
from app.utils.security import hash_password


FULL_NAME_REQUIRED_MESSAGE = "Укажите ФИО сотрудника."
EMAIL_REQUIRED_MESSAGE = "Укажите логин или email."
PASSWORD_REQUIRED_MESSAGE = "Укажите пароль."
ROLE_REQUIRED_MESSAGE = "Выберите роль."
EMAIL_DUPLICATE_MESSAGE = "Сотрудник с таким логином уже есть."
PHONE_INVALID_MESSAGE = "Проверьте телефон. Формат: +375291234567."
EMPLOYEE_PHONE_PATTERN = re.compile(r"^\+[0-9() -]+$")


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.full_name, User.id)).all())


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def create_user(db: Session, form_data: dict[str, Any]) -> User:
    full_name = _required_text(form_data.get("full_name"), FULL_NAME_REQUIRED_MESSAGE)
    email = _email_from_form(form_data)
    password = _required_text(form_data.get("password"), PASSWORD_REQUIRED_MESSAGE)
    role = _role_from_form(form_data.get("role"))

    _ensure_unique_email(db, email)

    user = User(
        full_name=full_name,
        email=email,
        phone=_optional_phone(form_data.get("phone")),
        role=role,
        is_active=_bool_from_form(form_data.get("is_active")),
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user(db: Session, user_id: int, form_data: dict[str, Any]) -> User:
    user = get_user(db, user_id)
    if user is None:
        raise ValueError("Сотрудник не найден.")

    user.full_name = _required_text(form_data.get("full_name"), FULL_NAME_REQUIRED_MESSAGE)
    user.phone = _optional_phone(form_data.get("phone"))
    user.role = _role_from_form(form_data.get("role"))
    user.is_active = _bool_from_form(form_data.get("is_active"))

    password = _clean_text(form_data.get("password"))
    if password:
        user.password_hash = hash_password(password)

    db.commit()
    db.refresh(user)
    return user


def role_label(role: UserRole | str | None) -> str:
    value = role.value if isinstance(role, UserRole) else role
    return {
        UserRole.ADMIN.value: "Админ",
        UserRole.MANAGER.value: "Менеджер",
        UserRole.COURIER.value: "Курьер",
    }.get(value or "", "Не указана")


def _ensure_unique_email(db: Session, email: str) -> None:
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise ValueError(EMAIL_DUPLICATE_MESSAGE)


def _email_from_form(form_data: dict[str, Any]) -> str:
    return _required_text(form_data.get("email"), EMAIL_REQUIRED_MESSAGE).lower()


def _role_from_form(value: Any) -> UserRole:
    try:
        return UserRole(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(ROLE_REQUIRED_MESSAGE) from exc


def _optional_phone(value: Any) -> str | None:
    value = _clean_text(value)
    if not value:
        return None
    if not EMPLOYEE_PHONE_PATTERN.fullmatch(value):
        raise ValueError(PHONE_INVALID_MESSAGE)
    digits = "".join(char for char in value if char.isdigit())
    if not digits:
        raise ValueError(PHONE_INVALID_MESSAGE)
    return f"+{digits}"


def _bool_from_form(value: Any) -> bool:
    return str(value or "").lower() in {"1", "true", "on", "yes"}


def _required_text(value: Any, message: str) -> str:
    value = _clean_text(value)
    if not value:
        raise ValueError(message)
    return value


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())
