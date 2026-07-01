from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi import status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import CourierCashHandoverStatus, UserRole
from app.models.user import User
from app.services.accounting_service import (
    accounting_payment_status_class,
    accounting_payment_status_label,
    accounting_period_caption,
    accounting_period_label,
    get_accounting_report,
)
from app.services.courier_cash_handover_service import (
    confirm_handover,
    handover_status_class,
    handover_status_label,
    list_handovers,
    reject_handover,
)
from app.services.courier_service import list_active_couriers
from app.templating import templates
from app.utils.auth import require_roles


router = APIRouter(prefix="/accounting", tags=["accounting"])


@router.get("")
def accounting_list(
    request: Request,
    period: str = "day",
    report_date: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    selected_date = _parse_date(report_date) or date.today()
    report = get_accounting_report(
        db,
        period=period,
        selected_date=selected_date,
    )
    return templates.TemplateResponse(
        request=request,
        name="accounting/list.html",
        context={
            "user": user,
            "nav_section": "accounting",
            "report": report,
            "rows": report.rows,
            "filters": {
                "period": report.period,
                "report_date": selected_date.isoformat(),
            },
            "period_options": ("day", "month"),
            "period_label": accounting_period_label,
            "period_caption": accounting_period_caption,
            "payment_status_label": accounting_payment_status_label,
            "payment_status_class": accounting_payment_status_class,
        },
    )


@router.get("/handovers")
def accounting_handovers(
    request: Request,
    handover_status: str = "",
    courier_id: str = "",
    date_from: str = "",
    date_to: str = "",
    error: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    return _handovers_response(
        request,
        db,
        user,
        handover_status=handover_status,
        courier_id=courier_id,
        date_from=date_from,
        date_to=date_to,
        error=error or None,
    )


@router.post("/handovers/{handover_id}/confirm")
async def confirm_handover_action(
    handover_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    values = dict(await request.form())
    try:
        confirm_handover(db, handover_id, user, comment=values.get("comment"))
    except (PermissionError, ValueError) as exc:
        return _handovers_response_from_form(request, db, user, values, str(exc))
    return RedirectResponse(_handovers_url(values), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/handovers/{handover_id}/reject")
async def reject_handover_action(
    handover_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    values = dict(await request.form())
    try:
        reject_handover(db, handover_id, user, comment=values.get("comment"))
    except (PermissionError, ValueError) as exc:
        return _handovers_response_from_form(request, db, user, values, str(exc))
    return RedirectResponse(_handovers_url(values), status_code=status.HTTP_303_SEE_OTHER)


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _handover_status_from_value(value: object) -> CourierCashHandoverStatus | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return CourierCashHandoverStatus(value)
    except ValueError:
        return None


def _int_from_value(value: object) -> int | None:
    value = str(value or "").strip()
    if not value or not value.isdigit():
        return None
    return int(value)


def _handovers_response(
    request: Request,
    db: Session,
    user: User,
    *,
    handover_status: str,
    courier_id: str,
    date_from: str,
    date_to: str,
    error: str | None,
    status_code: int = status.HTTP_200_OK,
):
    parsed_status = _handover_status_from_value(handover_status)
    parsed_courier_id = _int_from_value(courier_id)
    parsed_date_from = _parse_date(date_from)
    parsed_date_to = _parse_date(date_to)
    list_data = list_handovers(
        db,
        status=parsed_status,
        courier_id=parsed_courier_id,
        period_start=parsed_date_from,
        period_end=parsed_date_to,
    )
    return templates.TemplateResponse(
        request=request,
        name="accounting/handovers.html",
        context={
            "user": user,
            "nav_section": "accounting",
            "list_data": list_data,
            "handovers": list_data.rows,
            "couriers": list_active_couriers(db),
            "statuses": list(CourierCashHandoverStatus),
            "handover_status_label": handover_status_label,
            "handover_status_class": handover_status_class,
            "filters": {
                "handover_status": parsed_status.value if parsed_status else "",
                "courier_id": str(parsed_courier_id) if parsed_courier_id is not None else "",
                "date_from": parsed_date_from.isoformat() if parsed_date_from else "",
                "date_to": parsed_date_to.isoformat() if parsed_date_to else "",
            },
            "error": error,
        },
        status_code=status_code,
    )


def _handovers_response_from_form(
    request: Request,
    db: Session,
    user: User,
    values: dict[str, object],
    error: str,
):
    return _handovers_response(
        request,
        db,
        user,
        handover_status=str(values.get("handover_status") or ""),
        courier_id=str(values.get("courier_id") or ""),
        date_from=str(values.get("date_from") or ""),
        date_to=str(values.get("date_to") or ""),
        error=error,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _handovers_url(values: dict[str, object]) -> str:
    params = {
        "handover_status": str(values.get("handover_status") or ""),
        "courier_id": str(values.get("courier_id") or ""),
        "date_from": str(values.get("date_from") or ""),
        "date_to": str(values.get("date_to") or ""),
    }
    params = {key: value for key, value in params.items() if value}
    if not params:
        return "/accounting/handovers"
    from urllib.parse import urlencode

    return f"/accounting/handovers?{urlencode(params)}"
