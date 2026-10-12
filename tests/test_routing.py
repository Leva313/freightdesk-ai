import json
from datetime import datetime, timedelta

import flexpolyline
import httpx
import pytest

from app import routing, schedule
from app.schema import FreightOrder
from tests.test_app import load_expected


def order_with(**changes) -> FreightOrder:
    order = load_expected("01_email_en_paper_manchester_lyon")
    for path, value in changes.items():
        target = order
        *parents, leaf = path.split("__")
        for part in parents:
            target = getattr(target, part)
        setattr(target, leaf, value)
    return order


@pytest.mark.parametrize(
    "text, start, end",
    [
        ("08:00-12:00", "08:00", "12:00"),
        ("before 15:00", "00:00", "15:00"),
        ("from 05:00", "05:00", "23:59"),
        ("06:00 - 10:00 Uhr", "06:00", "10:00"),
        ("morning", "06:00", "12:00"),
        (None, "00:00", "23:59"),
    ],
)
def test_parse_window(text, start, end):
    opens, closes = schedule.parse_window("2026-10-14", None, text, "DE")
    assert (opens.strftime("%H:%M"), closes.strftime("%H:%M")) == (start, end)


def test_long_trip_gets_breaks_and_daily_rests():
    order = load_expected("06_booking_sheet_xlsx_barcelona_munich")
    plan = schedule.plan_trip(order, drive_seconds=20.3 * 3600)
    assert plan.rests == 2
    assert plan.breaks == 2
    assert plan.driving == timedelta(hours=20.3)
    driving = sum((s.duration for s in plan.steps if s.kind == "drive"), timedelta())
    assert driving == plan.driving
    for step in plan.steps:
        if step.kind == "drive":
            assert step.duration <= schedule.MAX_CONTINUOUS_DRIVING
    assert plan.status == "late"  # one driver cannot reach Munich by 08:00 on the 15th


def test_sunday_ban_stops_non_perishable_cargo_but_not_reefers():
    saturday_evening = dict(pickup__date_from="2026-10-17", pickup__date_to="2026-10-17", pickup__time_window="18:00-19:00")
    dry = order_with(**saturday_evening, delivery__date_from=None, delivery__date_to=None, delivery__country="DE")
    plan = schedule.plan_trip(dry, drive_seconds=8 * 3600)
    ban = [s for s in plan.steps if s.kind == "ban"]
    assert ban and ban[0].end.hour == 22 and ban[0].end.weekday() == 6
    assert plan.status == "no_deadline"

    reefer = dry.model_copy(deep=True)
    reefer.cargo.temperature_controlled = True
    assert not any(s.kind == "ban" for s in schedule.plan_trip(reefer, 8 * 3600).steps)


def test_truck_waits_for_the_delivery_window():
    order = load_expected("07_chat_screenshot_png_antwerp_warsaw")
    plan = schedule.plan_trip(order, drive_seconds=10 * 3600)
    assert plan.steps[-2].kind == "wait"
    assert plan.steps[-1].start.strftime("%a %H:%M") == "Mon 06:00"
    assert plan.status == "on_time"


def test_no_pickup_date_means_no_plan():
    plan = schedule.plan_trip(load_expected("04_email_ru_riga_warsaw_missing_info"), 3600)
    assert plan.status == "unknown" and not plan.steps


def fake_here(monkeypatch, routes_status=200, reject_hazard=False):
    calls = []
    polyline = flexpolyline.encode([(53.47, -2.31), (51.0, 1.5), (45.77, 4.88)])

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append((request.url.path, params))
        if "geocode" in request.url.host:
            lat = 53.47 if "Manchester" in params["q"] else 45.77
            return httpx.Response(200, json={"items": [{"position": {"lat": lat, "lng": 1.0}}]})
        if reject_hazard and "vehicle[shippedHazardousGoods]" in params:
            return httpx.Response(400, json={"title": "Invalid value"})
        return httpx.Response(
            routes_status,
            json={"routes": [{"sections": [{
                "summary": {"length": 1357400, "duration": 50000, "baseDuration": 46000},
                "polyline": polyline,
                "notices": [{"title": "Violated vehicle restriction.", "code": "violatedVehicleRestriction", "severity": "critical"}],
            }]}]},
        )

    real_client = httpx.Client
    monkeypatch.setattr(routing.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("HERE_API_KEY", "test-key")
    monkeypatch.delenv("FREIGHTDESK_DEMO", raising=False)
    return calls


def test_here_route_request_and_parsing(monkeypatch):
    calls = fake_here(monkeypatch)
    order = order_with(cargo__adr=True, cargo__adr_class="3")
    route = routing.here_route(order, departure=datetime(2099, 10, 14, 8, 0, tzinfo=schedule.zone("GB")))

    geocode_params = calls[0][1]
    assert geocode_params["in"] == "countryCode:GBR" and "Manchester" in geocode_params["q"]
    params = calls[-1][1]
    assert params["transportMode"] == "truck"
    assert params["vehicle[grossWeight]"] == "40000" and params["vehicle[height]"] == "400"
    assert params["vehicle[shippedHazardousGoods]"] == "flammable"
    assert params["departureTime"].startswith("2099-10-14T08:00:00")
    assert route.distance_km == 1357.4
    assert route.traffic_delay_seconds == 4000
    assert route.points[0] == (53.47, -2.31) and len(route.points) == 3
    assert route.warnings == ["Violated vehicle restriction."]
    assert routing.Route.from_json(route.to_json()) == route


def test_here_route_retries_without_unsupported_hazard(monkeypatch):
    fake_here(monkeypatch, reject_hazard=True)
    route = routing.here_route(order_with(cargo__adr=True, cargo__adr_class="2"), departure=None)
    assert any("did not accept hazardous goods type 'gas'" in w for w in route.warnings)


def test_bad_key_is_reported(monkeypatch):
    fake_here(monkeypatch, routes_status=401)
    with pytest.raises(routing.RoutingError, match="HERE rejected the API key"):
        routing.here_route(load_expected("01_email_en_paper_manchester_lyon"), departure=None)


def test_samples_have_stored_routes_and_orders_show_them(client):
    from app import db

    for path in routing.ROUTES_DIR.iterdir():
        assert routing.Route.from_json(path.read_text()).points
    client.post("/samples/06_booking_sheet_xlsx_barcelona_munich.xlsx")
    order = db.list_orders()[0]
    assert order.route is not None and order.route.distance_km > 1000
    page = client.get(f"/orders/{order.id}").text
    assert "Late by" in page and "Daily rest" in page
