"""Turn driving time into a realistic trip plan for a single-driver truck.

Applies a simplified version of EU Regulation 561/2006 and weekend driving bans:
- a 45 min break after 4.5 h of driving
- at most 9 h of driving per day, then an 11 h daily rest
- no driving on Sundays 00:00-22:00 in countries with a truck ban, unless the cargo is
  temperature controlled (perishables are exempt in most of them)

It is a planning estimate for dispatchers, not a tachograph calculation: duty time, reduced
rests, 10 h days and national holidays are not modelled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .schema import FreightOrder

LOADING = timedelta(hours=1, minutes=30)
UNLOADING = timedelta(hours=1, minutes=30)
MAX_CONTINUOUS_DRIVING = timedelta(hours=4, minutes=30)
BREAK = timedelta(minutes=45)
MAX_DAILY_DRIVING = timedelta(hours=9)
DAILY_REST = timedelta(hours=11)
TIGHT_MARGIN = timedelta(hours=2)
DEFAULT_LOADING_START = time(8, 0)

SUNDAY_BAN_COUNTRIES = {"AT", "CH", "CZ", "DE", "FR", "HU", "IT", "LU", "PL", "SI", "SK"}

TIMEZONES = {
    "AT": "Europe/Vienna", "BE": "Europe/Brussels", "BG": "Europe/Sofia", "CH": "Europe/Zurich",
    "CZ": "Europe/Prague", "DE": "Europe/Berlin", "DK": "Europe/Copenhagen", "EE": "Europe/Tallinn",
    "ES": "Europe/Madrid", "FI": "Europe/Helsinki", "FR": "Europe/Paris", "GB": "Europe/London",
    "GR": "Europe/Athens", "HR": "Europe/Zagreb", "HU": "Europe/Budapest", "IE": "Europe/Dublin",
    "IT": "Europe/Rome", "LT": "Europe/Vilnius", "LU": "Europe/Luxembourg", "LV": "Europe/Riga",
    "NL": "Europe/Amsterdam", "NO": "Europe/Oslo", "PL": "Europe/Warsaw", "PT": "Europe/Lisbon",
    "RO": "Europe/Bucharest", "SE": "Europe/Stockholm", "SI": "Europe/Ljubljana", "SK": "Europe/Bratislava",
    "TR": "Europe/Istanbul", "UA": "Europe/Kyiv",
}

_RANGE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–]\s*(\d{1,2})[:.](\d{2})")
_TIME = re.compile(r"(\d{1,2})[:.](\d{2})")
_NAMED = {"morning": (time(6), time(12)), "afternoon": (time(12), time(18)), "evening": (time(18), time(22))}


def zone(country: str | None) -> ZoneInfo:
    return ZoneInfo(TIMEZONES.get((country or "").upper(), "Europe/Berlin"))


def parse_window(date_from: str | None, date_to: str | None, text: str | None, country: str | None):
    """Return (start, end) as aware datetimes, or None when there is no date."""
    try:
        first = date.fromisoformat(date_from) if date_from else None
        last = date.fromisoformat(date_to) if date_to else first
    except ValueError:
        return None
    if first is None:
        return None
    start_t, end_t = time(0, 0), time(23, 59)
    lowered = (text or "").lower()
    if match := _RANGE.search(lowered):
        h1, m1, h2, m2 = map(int, match.groups())
        start_t, end_t = time(h1, m1), time(h2, m2)
    elif match := _TIME.search(lowered):
        moment = time(*map(int, match.groups()))
        if any(word in lowered for word in ("before", "by", "until", "bis", "до")):
            end_t = moment
        else:
            start_t = moment
    else:
        for word, (named_start, named_end) in _NAMED.items():
            if word in lowered:
                start_t, end_t = named_start, named_end
    tz = zone(country)
    return datetime.combine(first, start_t, tz), datetime.combine(last or first, end_t, tz)


def loading_start(order: FreightOrder) -> datetime | None:
    window = parse_window(order.pickup.date_from, order.pickup.date_to, order.pickup.time_window, order.pickup.country)
    if window is None:
        return None
    start = window[0]
    if start.time() == time(0, 0) and not order.pickup.time_window:
        start = start.replace(hour=DEFAULT_LOADING_START.hour, minute=DEFAULT_LOADING_START.minute)
    return start


@dataclass
class Step:
    kind: str  # loading | drive | break | rest | ban | wait | unloading
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass
class TripPlan:
    steps: list[Step] = field(default_factory=list)
    arrival: datetime | None = None
    unloaded: datetime | None = None
    deadline: datetime | None = None
    status: str = "unknown"  # on_time | tight | late | no_deadline | unknown
    slack: timedelta | None = None
    driving: timedelta = timedelta()
    applies_sunday_ban: bool = False

    @property
    def breaks(self) -> int:
        return sum(1 for step in self.steps if step.kind == "break")

    @property
    def rests(self) -> int:
        return sum(1 for step in self.steps if step.kind == "rest")


def _ban_end(moment: datetime) -> datetime | None:
    """If driving is banned at this moment, return when the ban ends."""
    if moment.weekday() == 6 and moment.time() < time(22, 0):
        return moment.replace(hour=22, minute=0, second=0, microsecond=0)
    return None


def _next_ban_start(moment: datetime) -> datetime:
    days_ahead = (6 - moment.weekday()) % 7
    candidate = (moment + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    return candidate if candidate > moment else candidate + timedelta(days=7)


def plan_trip(order: FreightOrder, drive_seconds: float) -> TripPlan:
    plan = TripPlan(driving=timedelta(seconds=round(drive_seconds)))
    start = loading_start(order)
    if start is None:
        return plan

    countries = {(order.pickup.country or "").upper(), (order.delivery.country or "").upper()}
    plan.applies_sunday_ban = bool(countries & SUNDAY_BAN_COUNTRIES) and not order.cargo.temperature_controlled

    now = start + LOADING
    plan.steps.append(Step("loading", start, now))

    remaining = plan.driving
    since_break = timedelta()
    driven_today = timedelta()
    while remaining > timedelta():
        if plan.applies_sunday_ban and (ban_end := _ban_end(now)):
            plan.steps.append(Step("ban", now, ban_end))
            if ban_end - now >= DAILY_REST:
                driven_today = timedelta()
            since_break = timedelta()
            now = ban_end
            continue
        if driven_today >= MAX_DAILY_DRIVING:
            plan.steps.append(Step("rest", now, now + DAILY_REST))
            now += DAILY_REST
            driven_today = since_break = timedelta()
            continue
        if since_break >= MAX_CONTINUOUS_DRIVING:
            plan.steps.append(Step("break", now, now + BREAK))
            now += BREAK
            since_break = timedelta()
            continue

        chunk = min(remaining, MAX_CONTINUOUS_DRIVING - since_break, MAX_DAILY_DRIVING - driven_today)
        if plan.applies_sunday_ban:
            chunk = min(chunk, _next_ban_start(now) - now)
        plan.steps.append(Step("drive", now, now + chunk))
        now += chunk
        remaining -= chunk
        since_break += chunk
        driven_today += chunk

    plan.arrival = now
    window = parse_window(order.delivery.date_from, order.delivery.date_to, order.delivery.time_window, order.delivery.country)
    if window is not None:
        opens, plan.deadline = window
        if now < opens:
            plan.steps.append(Step("wait", now, opens))
            now = opens
    plan.steps.append(Step("unloading", now, now + UNLOADING))
    plan.unloaded = now + UNLOADING

    if plan.deadline is None:
        plan.status = "no_deadline"
    else:
        plan.slack = plan.deadline - plan.arrival
        if plan.slack < timedelta():
            plan.status = "late"
        elif plan.slack < TIGHT_MARGIN:
            plan.status = "tight"
        else:
            plan.status = "on_time"
    return plan
