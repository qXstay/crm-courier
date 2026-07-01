from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.services.courier_service import (
    get_courier_dashboard,
    get_courier_route,
    get_route_summary,
    get_order_for_courier,
    list_active_couriers,
)
from app.services.courier_cash_handover_service import create_handover
from app.services.order_service import mark_order_at_courier_by_courier, mark_order_delivered_by_courier
from app.templating import templates
from app.utils.auth import require_roles
from app.utils.dates import crm_today


router = APIRouter(tags=["couriers"])


@router.get("/courier")
def courier_dashboard(
    request: Request,
    mode: str = Query("date"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    delivered_date: date | None = Query(None, alias="date"),
    detail: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.COURIER)),
):
    return _courier_dashboard_response(
        request,
        db,
        user,
        mode=mode,
        date_from=date_from or delivered_date,
        date_to=date_to,
        detail=detail or "assigned",
        error=None,
    )


@router.post("/courier/handovers")
async def courier_handover_action(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.COURIER)),
):
    values = dict(await request.form())
    period_start = _parse_date(values.get("period_start")) or crm_today()
    period_end = _parse_date(values.get("period_end")) or period_start
    try:
        create_handover(
            db,
            courier=user,
            amount_value=values.get("amount"),
            period_start=period_start,
            period_end=period_end,
        )
    except (PermissionError, ValueError) as exc:
        return _courier_dashboard_response(
            request,
            db,
            user,
            mode=str(values.get("mode") or "date"),
            date_from=period_start,
            date_to=period_end,
            detail="",
            error=str(exc),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(
        _courier_url(
            mode=str(values.get("mode") or "date"),
            date_from=period_start,
            date_to=period_end,
        ),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _courier_dashboard_response(
    request: Request,
    db: Session,
    user: User,
    *,
    mode: str,
    date_from: date | None,
    date_to: date | None,
    detail: str,
    error: str | None,
    status_code: int = status.HTTP_200_OK,
):
    dashboard = get_courier_dashboard(
        db,
        user,
        mode=mode,
        period_start=date_from,
        period_end=date_to,
        detail=detail or "assigned",
        today=crm_today(),
    )
    detail_links = (
        {
            "assigned": _courier_url(
                mode=dashboard.mode,
                date_from=dashboard.period_start,
                date_to=dashboard.period_end,
                detail="assigned",
            ),
            "delivered": _courier_url(
                mode=dashboard.mode,
                date_from=dashboard.period_start,
                date_to=dashboard.period_end,
                detail="delivered",
            ),
        }
        if dashboard
        else {}
    )

    return templates.TemplateResponse(
        request=request,
        name="couriers/dashboard.html",
        context={
            "user": user,
            "nav_section": "courier",
            "dashboard": dashboard,
            "orders": dashboard.detail_orders if dashboard else [],
            "filters": {
                "mode": dashboard.mode if dashboard else "date",
                "date_from": dashboard.period_start.isoformat() if dashboard else "",
                "date_to": dashboard.period_end.isoformat() if dashboard else "",
                "detail": dashboard.detail if dashboard else "",
            },
            "detail_links": detail_links,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/courier/orders/{order_id}")
def courier_order_detail(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.COURIER)),
):
    order = get_order_for_courier(db, order_id, user)
    if order is None:
        return RedirectResponse("/courier", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="couriers/order.html",
        context={
            "user": user,
            "nav_section": "courier",
            "order": order,
        },
    )


@router.post("/courier/orders/{order_id}/delivered")
def courier_order_delivered(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.COURIER)),
):
    try:
        order = mark_order_delivered_by_courier(db, order_id, user)
    except ValueError as exc:
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "message": str(exc) or "Не удалось сохранить. Проверьте данные."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(f"/courier/orders/{order_id}", status_code=status.HTTP_303_SEE_OTHER)
    if _wants_json(request):
        return {
            "ok": True,
            "status": order.status.value,
            "status_label": "Доставлено",
            "status_class": "status-done",
        }
    return RedirectResponse(f"/courier/orders/{order_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/courier/orders/{order_id}/at-courier")
def courier_order_at_courier(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.COURIER)),
):
    try:
        mark_order_at_courier_by_courier(db, order_id, user)
    except ValueError:
        pass
    return RedirectResponse("/courier", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/couriers")
def couriers_list(
    request: Request,
    route_date: date | None = Query(None, alias="date"),
    courier_id: str = Query("all"),
    detail: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    return _route_response(
        request,
        db,
        user,
        template_name="couriers/list.html",
        route_date=route_date,
        courier_id=courier_id,
        detail=detail,
    )


@router.get("/couriers/route")
def courier_route_redirect(
    courier_id: str = Query("all"),
    route_date: date | None = Query(None, alias="date"),
    date_from: date | None = Query(None),
    detail: str = Query(""),
):
    selected_date = date_from or route_date or crm_today()
    if str(courier_id).isdigit():
        return RedirectResponse(
            _route_url(
                route_date=selected_date,
                courier_id=str(courier_id),
                detail=detail,
                legacy_path=f"/couriers/{courier_id}/route",
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return RedirectResponse(
        _route_url(route_date=selected_date, courier_id="all", detail=detail),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/couriers/{courier_id}/route")
def courier_route(
    courier_id: int,
    request: Request,
    route_date: date | None = Query(None, alias="date"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    detail: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.MANAGER)),
):
    selected_date = date_from or route_date or crm_today()
    selected_to = date_to or selected_date
    route = get_courier_route(db, courier_id, selected_date, selected_to)
    if route.courier is None:
        return RedirectResponse("/couriers", status_code=status.HTTP_303_SEE_OTHER)

    return _route_response(
        request,
        db,
        user,
        template_name="couriers/route.html",
        route_date=selected_date,
        courier_id=str(courier_id),
        detail=detail,
    )


def _route_response(
    request: Request,
    db: Session,
    user: User,
    *,
    template_name: str,
    route_date: date | None,
    courier_id: str,
    detail: str,
):
    route_date = route_date or crm_today()
    if not detail:
        detail = "couriers" if courier_id == "all" else "all"
    summary = get_route_summary(
        db,
        route_date=route_date,
        courier_id=courier_id,
        detail=detail,
    )
    detail_links = {
        key: _route_url(route_date=summary.route_date, courier_id=summary.courier_id, detail=key)
        for key in ("couriers", "all", "in_work", "at_courier", "delivered")
    }
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "user": user,
            "nav_section": "route",
            "route_summary": summary,
            "couriers": list_active_couriers(db),
            "filters": {
                "date": summary.route_date.isoformat(),
                "courier_id": summary.courier_id,
                "detail": summary.detail,
            },
            "detail_links": detail_links,
            "can_view_route_money": user.role == UserRole.ADMIN,
        },
    )


def _parse_date(value: object) -> date | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _wants_json(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def _courier_url(*, mode: str, date_from: date, date_to: date, detail: str = "") -> str:
    selected_mode = "period" if mode == "period" else "date"
    query = {"mode": selected_mode, "date_from": date_from.isoformat()}
    if selected_mode == "period":
        query["date_to"] = date_to.isoformat()
    if detail:
        query["detail"] = detail
    return f"/courier?{urlencode(query)}"


def _route_url(
    *,
    route_date: date,
    courier_id: str,
    detail: str = "",
    legacy_path: str | None = None,
) -> str:
    path = legacy_path or "/couriers"
    query = {
        "date": route_date.isoformat(),
        "courier_id": courier_id,
        "detail": detail,
    }
    cleaned_query = {key: value for key, value in query.items() if value}
    return f"{path}?{urlencode(cleaned_query)}"
