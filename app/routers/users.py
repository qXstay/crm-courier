from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.services.user_service import (
    create_user,
    get_user,
    list_users,
    role_label,
    update_user,
)
from app.templating import templates
from app.utils.auth import require_roles


router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
def users_list(
    request: Request,
    success: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    success_message = {
        "created": "Сотрудник создан",
        "saved": "Сотрудник сохранён",
    }.get(success)
    return templates.TemplateResponse(
        request=request,
        name="users/list.html",
        context={
            "user": user,
            "nav_section": "users",
            "users": list_users(db),
            "role_label": role_label,
            "success_message": success_message,
        },
    )


@router.get("/new")
def new_user_form(
    request: Request,
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    return _form_response(request, user, edited_user=None, values={}, error=None)


@router.post("")
async def create_user_action(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    values = dict(await request.form())
    try:
        created_user = create_user(db, values)
    except ValueError as exc:
        return _form_response(
            request,
            user,
            edited_user=None,
            values=values,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/users?success=created", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{user_id}/edit")
def edit_user_form(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    edited_user = get_user(db, user_id)
    if edited_user is None:
        return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)

    return _form_response(request, user, edited_user=edited_user, values={}, error=None)


@router.post("/{user_id}/edit")
async def edit_user_action(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    values = dict(await request.form())
    edited_user = get_user(db, user_id)
    if edited_user is None:
        return RedirectResponse("/users", status_code=status.HTTP_303_SEE_OTHER)

    try:
        update_user(db, user_id, values)
    except ValueError as exc:
        return _form_response(
            request,
            user,
            edited_user=edited_user,
            values=values,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/users?success=saved", status_code=status.HTTP_303_SEE_OTHER)


def _form_response(
    request: Request,
    user: User,
    *,
    edited_user: User | None,
    values: dict[str, str],
    error: str | None,
    status_code: int = status.HTTP_200_OK,
):
    return templates.TemplateResponse(
        request=request,
        name="users/form.html",
        context={
            "user": user,
            "nav_section": "users",
            "edited_user": edited_user,
            "values": values,
            "roles": list(UserRole),
            "role_label": role_label,
            "error": error,
            "is_edit": edited_user is not None,
        },
        status_code=status_code,
    )
