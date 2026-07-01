from datetime import date
import json
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import OrderStatus, PaymentMethod, UserRole
from app.models.order import Order
from app.models.user import User
from app.services.client_service import get_client
from app.services.client_service import search_clients
from app.services.order_service import (
    DuplicateCargoNumberError,
    OrderListFilters,
    archive_order,
    archive_orders_bulk,
    create_order,
    get_order,
    list_active_couriers,
    list_active_orders,
    restore_order,
    update_order,
    update_order_status,
)
from app.services.payment_service import (
    create_quick_payments,
    payment_method_label,
    payment_status_class,
    payment_status_for_order,
    payment_status_label,
)
from app.templating import templates
from app.utils.auth import require_roles
from app.utils.dates import default_delivery_date


router = APIRouter(prefix="/orders", tags=["orders"])
ARCHIVE_STATUS_ACTION = "cancelled_archive"
ORDER_SORT_OPTIONS = {"newest", "number", "delivery_date", "courier", "payment", "status"}


@router.get("")
def orders_list(
    request: Request,
    q: str = Query("", alias="q"),
    sort: str = Query("newest", alias="sort"),
    date_from: str = Query("", alias="date_from"),
    date_to: str = Query("", alias="date_to"),
    order_status: str = Query("", alias="status"),
    courier_id: str = Query("", alias="courier_id"),
    deleted: str = Query("", alias="deleted"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    sort = _normalize_sort(sort)
    date_from_value = _date_from_value(date_from)
    date_to_value = _date_from_value(date_to)
    order_status_value = _order_status_from_value(order_status)
    courier_id_value = _int_from_value(courier_id)
    bulk_deleted_count = _int_from_value(deleted) or 0
    return _orders_list_response(
        request,
        db,
        user,
        q=q,
        sort=sort,
        date_from=date_from_value,
        date_to=date_to_value,
        order_status=order_status_value,
        courier_id=courier_id_value,
        bulk_deleted_count=bulk_deleted_count,
    )


@router.post("/bulk-archive")
async def bulk_archive_orders_action(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    form = await request.form()
    order_ids = form.getlist("order_ids")
    archived_count = archive_orders_bulk(db, order_ids, user)
    location = f"/orders?deleted={archived_count}" if archived_count else "/orders"
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{order_id}/status")
async def quick_status_action(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    values = dict(await request.form())
    list_filters = _list_filters_from_form(values)
    try:
        status_value = str(values.get("status") or "")
        if status_value == ARCHIVE_STATUS_ACTION:
            archive_order(db, order_id, user)
        else:
            update_order_status(
                db,
                order_id,
                status_value,
                user,
                courier_id_value=values.get("courier_id"),
            )
    except ValueError as exc:
        return _orders_list_response(
            request,
            db,
            user,
            **list_filters,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return_url = _safe_orders_return_url(values.get("return_url"))
    return RedirectResponse(
        return_url or _orders_url(**list_filters),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{order_id}/quick-pay")
async def quick_payment_action(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    values = dict(await request.form())
    list_filters = _list_filters_from_form(values)
    try:
        create_quick_payments(db, order_id, values, user)
    except ValueError as exc:
        return _orders_list_response(
            request,
            db,
            user,
            **list_filters,
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return_url = _safe_orders_return_url(values.get("return_url"))
    return RedirectResponse(
        return_url or _orders_url(**list_filters),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/new")
def new_order_form(
    request: Request,
    client_id: int | None = Query(None, alias="client_id"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    selected_client = get_client(db, client_id) if client_id is not None else None
    if client_id is not None and selected_client is None:
        return RedirectResponse("/orders/new", status_code=status.HTTP_303_SEE_OTHER)

    values = {"delivery_date": default_delivery_date().isoformat()}
    if selected_client is not None:
        values = {
            **values,
            "client_id": str(selected_client.id),
            "client_name": selected_client.full_name,
            "client_phone": selected_client.phone,
        }

    return _form_response(
        request,
        db,
        user,
        order=None,
        values=values,
        error=None,
        duplicate_cargo_order=None,
        selected_client=selected_client,
    )


@router.post("")
async def create_order_action(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    values = dict(await request.form())
    try:
        order = create_order(db, values, user)
    except DuplicateCargoNumberError as exc:
        return _form_response(
            request,
            db,
            user,
            order=None,
            values=values,
            error=str(exc),
            duplicate_cargo_order=exc.order,
            selected_client=get_client(db, int(values["client_id"]))
            if values.get("client_id", "").isdigit()
            else None,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except ValueError as exc:
        return _form_response(
            request,
            db,
            user,
            order=None,
            values=values,
            error=str(exc),
            duplicate_cargo_order=None,
            selected_client=get_client(db, int(values["client_id"]))
            if values.get("client_id", "").isdigit()
            else None,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(f"/orders/{order.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{order_id}")
def order_detail(
    order_id: int,
    request: Request,
    return_url: str = Query("", alias="return_url"),
    payment_error: str = Query("", alias="payment_error"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    order = get_order(db, order_id)
    if order is None:
        return RedirectResponse("/orders", status_code=status.HTTP_303_SEE_OTHER)
    if order.is_archived and user.role != UserRole.ADMIN:
        return RedirectResponse("/orders", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="orders/detail.html",
        context={
            "user": user,
            "nav_section": "archive" if order.is_archived else "orders",
            "order": order,
            "logs": sorted(order.change_logs, key=lambda log: log.id, reverse=True),
            "payments": sorted(order.payments, key=lambda payment: payment.id, reverse=True),
            "payment_status": payment_status_for_order(order),
            "payment_status_label": payment_status_label,
            "payment_status_class": payment_status_class,
            "payment_method_label": payment_method_label,
            "payment_methods": list(PaymentMethod),
            "payment_error": payment_error,
            "return_url": _safe_orders_return_url(return_url),
        },
    )


@router.get("/{order_id}/edit")
def edit_order_form(
    order_id: int,
    request: Request,
    return_url: str = Query("", alias="return_url"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    order = get_order(db, order_id)
    if order is None or order.is_archived:
        return RedirectResponse("/orders", status_code=status.HTTP_303_SEE_OTHER)

    return _form_response(
        request,
        db,
        user,
        order=order,
        values={},
        error=None,
        duplicate_cargo_order=None,
        selected_client=None,
        return_url=_safe_orders_return_url(return_url),
    )


@router.post("/{order_id}/edit")
async def edit_order_action(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    values = dict(await request.form())
    return_url = _safe_orders_return_url(values.get("return_url"))
    order = get_order(db, order_id)
    if order is None or order.is_archived:
        return RedirectResponse("/orders", status_code=status.HTTP_303_SEE_OTHER)

    try:
        updated_order = update_order(db, order_id, values, user)
    except DuplicateCargoNumberError as exc:
        return _form_response(
            request,
            db,
            user,
            order=order,
            values=values,
            error=str(exc),
            duplicate_cargo_order=exc.order,
            selected_client=None,
            return_url=return_url,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except ValueError as exc:
        return _form_response(
            request,
            db,
            user,
            order=order,
            values=values,
            error=str(exc),
            duplicate_cargo_order=None,
            selected_client=None,
            return_url=return_url,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(
        return_url or f"/orders/{updated_order.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{order_id}/archive")
def archive_order_action(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    try:
        archive_order(db, order_id, user)
    except ValueError:
        pass
    return RedirectResponse("/orders", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{order_id}/restore")
def restore_order_action(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN)),
):
    try:
        restore_order(db, order_id, user)
    except DuplicateCargoNumberError as exc:
        return RedirectResponse(
            f"/archive?duplicate_order_id={exc.order.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except ValueError:
        pass
    return RedirectResponse("/orders", status_code=status.HTTP_303_SEE_OTHER)


def _form_response(
    request: Request,
    db: Session,
    user: User,
    *,
    order: Order | None,
    values: dict[str, str],
    error: str | None,
    duplicate_cargo_order: Order | None,
    selected_client,
    return_url: str | None = None,
    status_code: int = status.HTTP_200_OK,
):
    couriers = list_active_couriers(db)
    clients = search_clients(db)
    return templates.TemplateResponse(
        request=request,
        name="orders/form.html",
        context={
            "user": user,
            "nav_section": "orders",
            "order": order,
            "values": values,
            "couriers": couriers,
            "client_suggestions_json": json.dumps(
                [
                    {
                        "id": client.id,
                        "name": client.full_name,
                        "phone": client.phone,
                    }
                    for client in clients
                ],
                ensure_ascii=False,
            ),
            "statuses": list(OrderStatus),
            "error": error,
            "duplicate_cargo_order": duplicate_cargo_order,
            "is_edit": order is not None,
            "selected_client": selected_client,
            "return_url": _safe_orders_return_url(return_url),
        },
        status_code=status_code,
    )


def _orders_list_response(
    request: Request,
    db: Session,
    user: User,
    *,
    q: str,
    sort: str = "newest",
    date_from: date | None = None,
    date_to: date | None = None,
    order_status: OrderStatus | None = None,
    courier_id: int | None = None,
    bulk_deleted_count: int = 0,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
):
    filters = OrderListFilters(
        search=q,
        delivery_date_from=date_from,
        delivery_date_to=date_to,
        status=order_status,
        courier_id=courier_id,
    )
    orders = list_active_orders(db, filters=filters)
    couriers = list_active_couriers(db)
    active_orders_count = sum(1 for order in orders if order.status != OrderStatus.DELIVERED)
    filter_context = {
        "q": q.strip(),
        "sort": _normalize_sort(sort),
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "status": order_status.value if order_status else "",
        "courier_id": str(courier_id) if courier_id is not None else "",
    }
    filters_active = any(
        (
            filter_context["q"],
            filter_context["sort"] != "newest",
            filter_context["date_from"],
            filter_context["date_to"],
            filter_context["status"],
            filter_context["courier_id"],
        )
    )
    return_url = _orders_url(
        q=q,
        sort=filter_context["sort"],
        date_from=date_from,
        date_to=date_to,
        order_status=order_status,
        courier_id=courier_id,
    )
    return templates.TemplateResponse(
        request=request,
        name="orders/list.html",
        context={
            "user": user,
            "nav_section": "orders",
            "orders": orders,
            "couriers": couriers,
            "statuses": list(OrderStatus),
            "archive_status_action": ARCHIVE_STATUS_ACTION,
            "active_orders_count": active_orders_count,
            "total_orders_count": len(orders),
            "payment_status_for_order": payment_status_for_order,
            "payment_status_label": payment_status_label,
            "payment_status_class": payment_status_class,
            "filters": filter_context,
            "filters_active": filters_active,
            "return_url": return_url,
            "return_url_param": urlencode({"return_url": return_url}),
            "bulk_deleted_count": bulk_deleted_count,
            "error": error,
        },
        status_code=status_code,
    )


def _orders_url(
    *,
    q: str,
    sort: str = "newest",
    date_from: date | None = None,
    date_to: date | None = None,
    order_status: OrderStatus | None = None,
    courier_id: int | None = None,
) -> str:
    params = {
        "q": q.strip(),
        "sort": _normalize_sort(sort),
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "status": order_status.value if order_status else "",
        "courier_id": str(courier_id) if courier_id is not None else "",
    }
    params = {key: value for key, value in params.items() if value}
    if params.get("sort") == "newest":
        params.pop("sort")
    if not params:
        return "/orders"
    return f"/orders?{urlencode(params)}"


def _list_filters_from_form(values: dict[str, str]) -> dict[str, object]:
    return {
        "q": str(values.get("q") or ""),
        "sort": _normalize_sort(str(values.get("sort") or "newest")),
        "date_from": _date_from_value(values.get("date_from")),
        "date_to": _date_from_value(values.get("date_to")),
        "order_status": _order_status_from_value(values.get("filter_status")),
        "courier_id": _int_from_value(values.get("filter_courier_id")),
    }


def _normalize_sort(sort: str) -> str:
    return sort if sort in ORDER_SORT_OPTIONS else "newest"


def _date_from_value(value: object) -> date | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _order_status_from_value(value: object) -> OrderStatus | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return OrderStatus(value)
    except ValueError:
        return None


def _int_from_value(value: object) -> int | None:
    value = str(value or "").strip()
    if not value or not value.isdigit():
        return None
    return int(value)


def _safe_orders_return_url(value: object) -> str | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None

    parsed = urlsplit(raw_value)
    if parsed.scheme or parsed.netloc:
        return None
    if parsed.path != "/orders":
        return None

    return urlunsplit(("", "", parsed.path, parsed.query, ""))
