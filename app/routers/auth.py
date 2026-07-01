from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, is_sqlite
from app.models.enums import UserRole
from app.models.user import User
from app.templating import templates
from app.utils.security import verify_password


router = APIRouter()


# Demo-входы без пароля. Берём уже существующих demo-пользователей из seed.
# Admin сюда намеренно не добавлен: тогда сотрудники, архив и бухгалтерия
# остаются недоступны demo-посетителю по ролям.
DEMO_ACCOUNTS = {
    "manager": "manager@courier.local",
    "courier": "courier@courier.local",
}


def _demo_mode_enabled() -> bool:
    """Включён ли demo-mode. Переопределяется в тестах через dependency_overrides."""
    return bool(settings.demo_mode)


def _require_demo_mode(enabled: bool = Depends(_demo_mode_enabled)) -> None:
    if not enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.get("/login")
def login_form(
    request: Request,
    demo_mode: bool = Depends(_demo_mode_enabled),
):
    return templates.TemplateResponse(
        request=request,
        name="auth/login.html",
        context={"error": None, "email": "", "demo_mode": demo_mode},
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))

    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="auth/login.html",
            context={
                "error": "Неверная почта или пароль.",
                "email": email,
                "demo_mode": settings.demo_mode,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    request.session.clear()
    request.session["user_id"] = user.id

    if user.role == UserRole.COURIER:
        return RedirectResponse("/courier", status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/demo-login/{role}")
def demo_login(
    role: str,
    request: Request,
    db: Session = Depends(get_db),
    _enabled: None = Depends(_require_demo_mode),
):
    email = DEMO_ACCOUNTS.get(role)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    _reset_sqlite_demo_before_login(db)

    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        # Demo-пользователи ещё не созданы. В demo-mode показываем понятную ошибку.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo-пользователи не найдены. Запустите наполнение демо-данными.",
        )

    request.session.clear()
    request.session["user_id"] = user.id

    if user.role == UserRole.COURIER:
        return RedirectResponse("/courier", status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)




def _reset_sqlite_demo_before_login(db: Session) -> None:
    if not settings.demo_mode or not is_sqlite:
        return

    from app.utils.seed_demo import reset_demo_data

    db.close()
    reset_demo_data()
