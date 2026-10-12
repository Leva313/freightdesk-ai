"""Precompute routes for the sample orders so the demo shows maps without any API key.

    python samples/make_routes.py

With HERE_API_KEY set, routes come from HERE truck routing (with predicted traffic).
Without it, they are built from OpenStreetMap data: Nominatim for addresses and the public OSRM
server for the road geometry, with truck driving time estimated from distance. The public
servers are rate limited, so requests are spaced out.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import routing, schedule  # noqa: E402
from app.schema import FreightOrder, Stop  # noqa: E402

TRUCK_AVERAGE_KMH = 68
OSM_PROVIDER = f"OpenStreetMap route, truck time estimated at {TRUCK_AVERAGE_KMH} km/h average"
HEADERS = {"User-Agent": "FreightDesk-AI demo route builder (github.com/Leva313/freightdesk-ai)"}


def nominatim(stop: Stop, client: httpx.Client) -> tuple[float, float]:
    params = {"format": "json", "limit": 1, "countrycodes": (stop.country or "").lower()}
    if stop.address:
        params["street"] = stop.address.replace("ul. ", "")
    if stop.city:
        params["city"] = stop.city
    if stop.postal_code:
        params["postalcode"] = stop.postal_code
    for attempt in (params, {k: v for k, v in params.items() if k not in ("street", "postalcode")}):
        time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
        results = client.get("https://nominatim.openstreetmap.org/search", params=attempt).json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    raise RuntimeError(f"Nominatim could not find {stop}")


def osm_route(order: FreightOrder) -> routing.Route:
    with httpx.Client(timeout=60, headers=HEADERS) as client:
        origin = nominatim(order.pickup, client)
        destination = nominatim(order.delivery, client)
        coords = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        data = client.get(
            f"https://router.project-osrm.org/route/v1/driving/{coords}",
            params={"overview": "full", "geometries": "geojson"},
        ).json()
    route = data["routes"][0]
    distance_km = route["distance"] / 1000
    drive_seconds = distance_km / TRUCK_AVERAGE_KMH * 3600
    return routing.Route(
        provider=OSM_PROVIDER,
        distance_km=round(distance_km, 1),
        drive_seconds=round(drive_seconds),
        base_seconds=round(drive_seconds),
        origin=origin,
        destination=destination,
        points=routing.simplify([(round(lat, 5), round(lng, 5)) for lng, lat in route["geometry"]["coordinates"]]),
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    routing.ROUTES_DIR.mkdir(exist_ok=True)
    for expected in sorted((ROOT / "samples" / "expected").glob("*.json")):
        order = FreightOrder.model_validate_json(expected.read_text(encoding="utf-8"))
        if routing.has_key():
            route = routing.here_route(order, schedule.loading_start(order))
        else:
            route = osm_route(order)
        (routing.ROUTES_DIR / expected.name).write_text(route.to_json(), encoding="utf-8")
        print(f"{expected.stem}: {route.distance_km} km, {route.drive_seconds / 3600:.1f} h ({route.provider})")


if __name__ == "__main__":
    main()
