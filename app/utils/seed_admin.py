from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.enums import UserRole
from app.models.user import User
from app.utils.security import hash_password


def create_admin() -> None:
    if not settings.admin_email or not settings.admin_password:
        raise RuntimeError("ADMIN_EMAIL and ADMIN_PASSWORD are required")

    email = settings.admin_email.strip().lower()

    with SessionLocal() as db:
        existing_user = db.scalar(select(User).where(User.email == email))
        if existing_user is not None:
            print("Админ уже существует")
            return

        admin = User(
            full_name="Администратор",
            email=email,
            password_hash=hash_password(settings.admin_password),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        db.commit()
        print("Админ создан")


if __name__ == "__main__":
    create_admin()
