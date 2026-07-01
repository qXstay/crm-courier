from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import OrderStatus, UserRole
from app.models.client import Client
from app.models.user import User
from app.services.client_service import (
    DuplicateClientError,
    create_client,
    find_possible_matches,
    get_client_detail,
    search_clients,
    update_client,
)
from app.templating import templates
from app.utils.auth import require_roles


router = APIRouter(prefix="/clients", tags=["clients"])


@router.get("")
def clients_list(
    request: Request,
    q: str = Query("", alias="q"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    clients = search_clients(db, q)
    return templates.TemplateResponse(
        request=request,
        name="clients/list.html",
        context={
            "user": user,
            "nav_section": "clients",
            "clients": clients,
            "query": q,
            "client_total_orders_count": _client_total_orders_count,
            "client_in_work_orders_count": _client_in_work_orders_count,
        },
    )


@router.get("/new")
def new_client_form(
    request: Request,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    return _form_response(
        request,
        user,
        client=None,
        values={},
        error=None,
        duplicate_client=None,
        possible_matches=[],
    )


@router.post("")
async def create_client_action(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    values = dict(await request.form())
    try:
        client = create_client(db, values, user)
    except DuplicateClientError as exc:
        return _form_response(
            request,
            user,
            client=None,
            values=values,
            error=str(exc),
            duplicate_client=exc.client,
            possible_matches=[],
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except ValueError as exc:
        return _form_response(
            request,
            user,
            client=None,
            values=values,
            error=str(exc),
            duplicate_client=None,
            possible_matches=find_possible_matches(db, values.get("full_name"), values.get("phone")),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(f"/clients/{client.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{client_id}")
def client_detail(
    client_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    detail = get_client_detail(db, client_id)
    if detail is None:
        return RedirectResponse("/clients", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="clients/detail.html",
        context={
            "user": user,
            "nav_section": "clients",
            "detail": detail,
            "client": detail.client,
            "orders": detail.orders,
        },
    )


@router.get("/{client_id}/edit")
def edit_client_form(
    client_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    detail = get_client_detail(db, client_id)
    if detail is None:
        return RedirectResponse("/clients", status_code=status.HTTP_303_SEE_OTHER)

    return _form_response(
        request,
        user,
        client=detail.client,
        values={},
        error=None,
        duplicate_client=None,
        possible_matches=[],
    )


@router.post("/{client_id}/edit")
async def edit_client_action(
    client_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    detail = get_client_detail(db, client_id)
    if detail is None:
        return RedirectResponse("/clients", status_code=status.HTTP_303_SEE_OTHER)

    values = dict(await request.form())
    try:
        client = update_client(db, client_id, values)
    except DuplicateClientError as exc:
        return _form_response(
            request,
            user,
            client=detail.client,
            values=values,
            error=str(exc),
            duplicate_client=exc.client,
            possible_matches=[],
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except ValueError as exc:
        return _form_response(
            request,
            user,
            client=detail.client,
            values=values,
            error=str(exc),
            duplicate_client=None,
            possible_matches=find_possible_matches(db, values.get("full_name"), values.get("phone")),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(f"/clients/{client.id}", status_code=status.HTTP_303_SEE_OTHER)


def _form_response(
    request: Request,
    user: User,
    *,
    client: Client | None,
    values: dict[str, str],
    error: str | None,
    duplicate_client: Client | None,
    possible_matches: list[Client],
    status_code: int = status.HTTP_200_OK,
):
    return templates.TemplateResponse(
        request=request,
        name="clients/form.html",
        context={
            "user": user,
            "nav_section": "clients",
            "client": client,
            "values": values,
            "error": error,
            "duplicate_client": duplicate_client,
            "possible_matches": possible_matches,
            "is_edit": client is not None,
        },
        status_code=status_code,
    )


def _client_total_orders_count(client: Client) -> int:
    return len(client.orders)


def _client_in_work_orders_count(client: Client) -> int:
    return sum(
        1
        for order in client.orders
        if not order.is_archived and order.status == OrderStatus.IN_WORK
    )
