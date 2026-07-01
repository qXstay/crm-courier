from fastapi import APIRouter, Depends, status
from fastapi.responses import RedirectResponse

from app.models.enums import UserRole
from app.models.user import User
from app.utils.auth import get_current_user


router = APIRouter()


@router.get("/")
def dashboard(
    user: User = Depends(get_current_user),
):
    if user.role == UserRole.COURIER:
        return RedirectResponse("/courier", status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse("/orders", status_code=status.HTTP_303_SEE_OTHER)
