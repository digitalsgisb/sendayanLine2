"""Pure production-shift schedule calculations for Sendayan trackers."""

from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta
import re


DEFAULT_WORKING_TIMES = {
    ("DAY", False): "8:00 AM to 4:15 PM",
    ("DAY", True): "8:00 AM to 8:00 PM",
    ("NIGHT", False): "4:15 PM to 12:30 AM",
    ("NIGHT", True): "8:00 PM to 8:00 AM",
}


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ot"}


def _parse_production_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for date_format in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid production date: {value!r}")


def _parse_working_time(value: str) -> tuple[clock_time, clock_time]:
    parts = re.split(r"\s+to\s+", str(value or "").strip(), flags=re.IGNORECASE)
    if len(parts) != 2:
        raise ValueError(f"Invalid working time: {value!r}")
    try:
        start_time = datetime.strptime(parts[0].strip(), "%I:%M %p").time()
        end_time = datetime.strptime(parts[1].strip(), "%I:%M %p").time()
    except ValueError as exc:
        raise ValueError(f"Invalid working time: {value!r}") from exc
    return start_time, end_time


def calculate_shift_end_datetime(production_date, shift, overtime, working_time) -> datetime:
    production_day = _parse_production_date(production_date)
    normalized_shift = str(shift or "").strip().upper()
    overtime_enabled = coerce_bool(overtime)
    try:
        start_time, end_time = _parse_working_time(working_time)
    except ValueError:
        fallback = DEFAULT_WORKING_TIMES.get((normalized_shift, overtime_enabled))
        if fallback is None:
            raise ValueError(
                f"Cannot determine schedule for shift={shift!r}, overtime={overtime!r}"
            )
        start_time, end_time = _parse_working_time(fallback)
    start_datetime = datetime.combine(production_day, start_time)
    end_datetime = datetime.combine(production_day, end_time)
    if end_datetime <= start_datetime:
        end_datetime += timedelta(days=1)
    return end_datetime


def hour_slot_for_shift_end(shift_end: datetime) -> str:
    if shift_end.minute == 0 and shift_end.second == 0:
        end_hour = shift_end.hour
        start_hour = (end_hour - 1) % 24
    else:
        start_hour = shift_end.hour
        end_hour = (start_hour + 1) % 24
    return f"{start_hour}.00-{end_hour}.00"
