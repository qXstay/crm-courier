from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.services.order_service import (
    DuplicateCargoNumberError,
    get_order,
    list_archived_orders,
    restore_order,
)
from app.templating import templates
from app.utils.auth import require_roles


router = APIRouter(prefix="/archive", tags=["archive"])


@router.get("")
def archive_list(
    request: Request,
    duplicate_order_id: int | None = Query(None, alias="duplicate_order_id"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    duplicate_order = get_order(db, duplicate_order_id) if duplicate_order_id else None
    return _archive_response(
        request,
        db,
        user,
        duplicate_cargo_order=duplicate_order,
    )


def _archive_response(
    request: Request,
    db: Session,
    user: User,
    *,
    duplicate_cargo_order=None,
    status_code: int = status.HTTP_200_OK,
):
    orders = list_archived_orders(db)
    return templates.TemplateResponse(
        request=request,
        name="archive/list.html",
        context={
            "user": user,
            "nav_section": "archive",
            "orders": orders,
            "archived_count": len(orders),
            "duplicate_cargo_order": duplicate_cargo_order,
        },
        status_code=status_code,
    )


@router.post("/{order_id}/restore")
def restore_from_archive_action(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    try:
        restore_order(db, order_id, user)
    except DuplicateCargoNumberError as exc:
        return _archive_response(
            request,
            db,
            user,
            duplicate_cargo_order=exc.order,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except ValueError:
        pass
    return RedirectResponse("/orders", status_code=303)
