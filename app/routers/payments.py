from datetime import date
from urllib.parse import urlencode
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import PaymentDisplayStatus, PaymentMethod, UserRole
from app.models.order import Order
from app.models.user import User
from app.services.payment_service import (
    create_or_update_payment,
    delete_payment,
    empty_payment_list,
    list_payment_rows,
    payment_method_label,
    payment_status_class,
    payment_status_label,
    update_payment,
)
from app.templating import templates
from app.utils.auth import require_roles


router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("")
def payments_list(
    request: Request,
    q: str = "",
    search: str = "",
    payment_status: str = "",
    status: str = "",
    delivery_date: str = "",
    date: str = "",
    detail: str = "",
    view: str = "",
    success: str = "",
    order_id: str = "",
    focus_order_id: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    effective_q = q or search
    effective_payment_status = payment_status or status
    effective_delivery_date = delivery_date or date
    if not (effective_q or effective_payment_status or effective_delivery_date or detail or view or success or order_id or focus_order_id):
        view = "unpaid"
    return _payments_response(
        request,
        db,
        user,
        q=effective_q,
        payment_status=effective_payment_status,
        delivery_date=effective_delivery_date,
        detail=detail,
        view=view,
        success=success,
        order_id=order_id,
        focus_order_id=focus_order_id,
        error=None,
    )


@router.post("/{order_id}/pay")
async def pay_order_action(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    values = dict(await request.form())
    try:
        create_or_update_payment(db, order_id, values, user)
    except ValueError as exc:
        return _payments_response(
            request,
            db,
            user,
            q=str(values.get("q") or ""),
            payment_status=str(values.get("payment_status") or ""),
            delivery_date=str(values.get("delivery_date_filter") or ""),
            detail=str(values.get("detail") or ""),
            view=str(values.get("view") or ""),
            success="",
            order_id="",
            focus_order_id="",
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(
        _payments_url(values, success=True, order_id=order_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{payment_id}/delete")
async def delete_payment_action(
    payment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    values = dict(await request.form())
    try:
        delete_payment(db, payment_id, user)
    except ValueError as exc:
        return _payments_response(
            request,
            db,
            user,
            q=str(values.get("q") or ""),
            payment_status=str(values.get("payment_status") or ""),
            delivery_date=str(values.get("delivery_date_filter") or ""),
            detail=str(values.get("detail") or ""),
            view=str(values.get("view") or ""),
            success="",
            order_id="",
            focus_order_id="",
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    redirect_to = str(values.get("redirect_to") or "")
    if redirect_to.startswith("/orders/"):
        return RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse(_payments_url(values), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{payment_id}/edit")
async def edit_payment_action(
    payment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    values = dict(await request.form())
    redirect_to = _order_redirect(values.get("redirect_to"))
    try:
        update_payment(
            db,
            payment_id,
            values,
            user,
            order_id=_int_from_value(values.get("order_id")),
        )
    except ValueError as exc:
        if redirect_to:
            return RedirectResponse(
                f"{redirect_to}?payment_error={quote(str(exc))}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return _payments_response(
            request,
            db,
            user,
            q=str(values.get("q") or ""),
            payment_status=str(values.get("payment_status") or ""),
            delivery_date=str(values.get("delivery_date_filter") or ""),
            detail=str(values.get("detail") or ""),
            view=str(values.get("view") or ""),
            success="",
            order_id="",
            focus_order_id="",
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if redirect_to:
        return RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse(_payments_url(values), status_code=status.HTTP_303_SEE_OTHER)


def _payments_response(
    request: Request,
    db: Session,
    user: User,
    *,
    q: str,
    payment_status: str,
    delivery_date: str,
    detail: str,
    view: str,
    success: str,
    order_id: str,
    focus_order_id: str,
    error: str | None,
    status_code: int = status.HTTP_200_OK,
):
    filter_date = _parse_date(delivery_date)
    normalized_view = "unpaid" if view == "unpaid" else ""
    normalized_detail = detail if detail in {"all", "unpaid", "pending", "partial", "paid"} else ""
    has_filter = bool(q.strip() or payment_status.strip() or filter_date)
    show_results = bool(error or normalized_view == "unpaid" or has_filter)
    list_data = (
        list_payment_rows(
            db,
            search=q,
            payment_status=payment_status,
            delivery_date=filter_date,
            detail=normalized_detail,
            unpaid_all_time=normalized_view == "unpaid",
        )
        if show_results
        else empty_payment_list()
    )
    filters = {
        "q": q,
        "payment_status": payment_status,
        "delivery_date": delivery_date if filter_date else "",
        "detail": normalized_detail,
        "view": normalized_view,
    }
    return templates.TemplateResponse(
        request=request,
        name="payments/list.html",
        context={
            "user": user,
            "nav_section": "payments",
            "list_data": list_data,
            "rows": list_data.rows,
            "payment_statuses": list(PaymentDisplayStatus),
            "payment_methods": list(PaymentMethod),
            "payment_status_label": payment_status_label,
            "payment_status_class": payment_status_class,
            "payment_method_label": payment_method_label,
            "filters": filters,
            "show_results": show_results,
            "stat_links": _payment_stat_links(filters),
            "success": success == "payment",
            "success_links": _payment_success_links(db, filters, order_id),
            "focus_order_id": _int_from_value(focus_order_id),
            "error": error,
        },
        status_code=status_code,
    )


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _int_from_value(value: object) -> int | None:
    value = str(value or "").strip()
    if not value or not value.isdigit():
        return None
    return int(value)


def _order_redirect(value: object) -> str | None:
    value = str(value or "").strip()
    if value.startswith("/orders/") and "://" not in value:
        return value
    return None


def _payments_url(
    values: dict[str, object],
    *,
    success: bool = False,
    order_id: int | None = None,
) -> str:
    query = {
        "q": str(values.get("q") or values.get("search") or ""),
        "payment_status": str(values.get("payment_status") or values.get("status") or ""),
        "delivery_date": str(_first_value(values, "delivery_date_filter", "delivery_date", "date")),
        "detail": str(values.get("detail") or ""),
        "view": str(values.get("view") or ""),
    }
    if success:
        query["success"] = "payment"
    if order_id is not None:
        query["order_id"] = str(order_id)
    cleaned_query = {key: value for key, value in query.items() if value}
    if not cleaned_query:
        return "/payments"
    return f"/payments?{urlencode(cleaned_query)}"


def _payment_stat_links(filters: dict[str, str]) -> dict[str, str]:
    return {
        "all": _payments_query_url(filters, detail="all"),
        "unpaid": _payments_query_url(filters, detail="unpaid"),
        "pending": _payments_query_url(filters, detail="pending"),
        "partial": _payments_query_url(filters, detail="partial"),
        "paid": _payments_query_url(filters, detail="paid"),
    }


def _payments_query_url(filters: dict[str, str], *, detail: str) -> str:
    query = {
        "q": filters.get("q", ""),
        "payment_status": filters.get("payment_status", ""),
        "delivery_date": filters.get("delivery_date", ""),
        "view": filters.get("view", ""),
        "detail": detail,
    }
    cleaned_query = {key: value for key, value in query.items() if value}
    return f"/payments?{urlencode(cleaned_query)}"


def _payment_success_links(db: Session, filters: dict[str, str], order_id: str) -> dict[str, str]:
    parsed_order_id = _int_from_value(order_id)
    order = None
    if parsed_order_id is not None:
        order = db.get(Order, parsed_order_id)
    paid_query = {
        "q": order.order_code if order is not None and not order.is_archived else filters.get("q", ""),
        "payment_status": PaymentDisplayStatus.PAID.value,
        "detail": "paid",
        "focus_order_id": str(parsed_order_id) if parsed_order_id is not None else "",
    }
    paid_query_string = urlencode({key: value for key, value in paid_query.items() if value})
    links = {"paid": f"/payments?{paid_query_string}"}
    if parsed_order_id is not None:
        links["order"] = f"/orders/{parsed_order_id}"
    return links


def _first_value(values: dict[str, object], *keys: str) -> object:
    for key in keys:
        value = values.get(key)
        if value:
            return value
    return ""
