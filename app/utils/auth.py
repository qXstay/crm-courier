from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import UserRole
from app.models.user import User


class LoginRequired(Exception):
    pass


class ForbiddenRole(HTTPException):
    def __init__(self, user: User):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав",
        )
        self.user = user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise LoginRequired()

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        raise LoginRequired()

    return user


def require_roles(*roles: UserRole) -> Callable[..., User]:
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise ForbiddenRole(user)
        return user

    return dependency


require_admin = require_roles(UserRole.ADMIN)
require_manager = require_roles(UserRole.MANAGER)
require_courier = require_roles(UserRole.COURIER)
