from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Base, engine, is_sqlite
from app.models.enums import UserRole
from app.models.user import User
from app.routers import accounting, archive, auth, clients, couriers, orders, pages, payments, users
from app.templating import templates
from app.utils.auth import LoginRequired

import app.models  # noqa: F401  (регистрация всех моделей для Base.metadata.create_all)


STATIC_CACHE_CONTROL = "public, max-age=3600"


class CacheControlStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = STATIC_CACHE_CONTROL
        return response


def bootstrap_sqlite_demo(*, seed: bool | None = None) -> None:
    """SQLite demo-profile: создать таблицы и (опционально) залить seed.

    Заменяет Alembic для SQLite-витрины. На PostgreSQL не вызывается — там
    схема создаётся через SQLAlchemy, seed включается настройками demo-профиля.
    """
    Base.metadata.create_all(bind=engine)
    if seed is True:
        from app.utils.seed_demo import seed_demo

        seed_demo()
    elif seed is None and (settings.demo_mode or settings.seed_demo):
        from app.utils.seed_demo import demo_users_exist, seed_demo

        if not demo_users_exist():
            seed_demo()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup только для SQLite demo-profile. PostgreSQL использует start.sh.
    if is_sqlite:
        bootstrap_sqlite_demo()

    yield


app = FastAPI(
    title="Courier CRM",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

app.mount("/static", CacheControlStaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(archive.router)
app.include_router(orders.router)
app.include_router(clients.router)
app.include_router(couriers.router)
app.include_router(payments.router)
app.include_router(accounting.router)
app.include_router(users.router)
app.include_router(pages.router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(
        Path(__file__).parent / "static" / "img" / "favicon.svg",
        media_type="image/svg+xml",
    )


@app.exception_handler(LoginRequired)
def login_required_handler(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def http_exception_handler_html(request: Request, exc: HTTPException):
    if exc.status_code != status.HTTP_403_FORBIDDEN or not _prefers_html(request):
        return await http_exception_handler(request, exc)

    user = getattr(exc, "user", None)
    action_label = "Войти"
    action_href = "/login"
    if isinstance(user, User) and user.role == UserRole.COURIER:
        action_label = "К моим заявкам"
        action_href = "/courier"
    elif isinstance(user, User) and user.role in (UserRole.ADMIN, UserRole.MANAGER):
        action_label = "К заявкам"
        action_href = "/orders"

    return templates.TemplateResponse(
        request=request,
        name="errors/403.html",
        context={
            "user": user,
            "action_label": action_label,
            "action_href": action_href,
        },
        status_code=status.HTTP_403_FORBIDDEN,
    )


def _prefers_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or not accept or "*/*" in accept
