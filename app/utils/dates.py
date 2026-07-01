from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


CRM_TIMEZONE = ZoneInfo("Europe/Moscow")
DELIVERY_TOMORROW_FROM = time(15, 1)


def crm_today(now: datetime | None = None) -> date:
    return _crm_datetime(now).date()


def default_delivery_date(now: datetime | None = None) -> date:
    current = _crm_datetime(now)
    if current.time() < DELIVERY_TOMORROW_FROM:
        return current.date()
    return current.date() + timedelta(days=1)


def _crm_datetime(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(CRM_TIMEZONE)
    if now.tzinfo is None:
        return now.replace(tzinfo=CRM_TIMEZONE)
    return now.astimezone(CRM_TIMEZONE)
