"""Truck routes from the HERE Geocoding & Search and Routing v8 APIs.

Without HERE_API_KEY (or in the hosted demo) the bundled samples use routes stored in
samples/routes, so the map and trip plan work without any key.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import PurePath

import flexpolyline
import httpx

from . import extractor
from .schema import Cargo, FreightOrder, Stop

GEOCODE_URL = "https://geocode.search.hereapi.com/v1/geocode"
ROUTES_URL = "https://router.hereapi.com/v8/routes"
ROUTES_DIR = extractor.SAMPLES_DIR / "routes"
MAX_MAP_POINTS = 600

# A standard EU articulated truck: 40 t, 4 m high, 2.55 m wide, 16.5 m long, 5 axles.
TRUCK = {
    "vehicle[grossWeight]": 40000,
    "vehicle[height]": 400,
    "vehicle[width]": 255,
    "vehicle[length]": 1650,
    "vehicle[axleCount]": 5,
    "vehicle[trailerCount]": 1,
}

# ADR class (first digit) -> HERE hazardous goods category.
ADR_TO_HERE = {
    "1": "explosive",
    "2": "gas",
    "3": "flammable",
    "4": "combustible",
    "5": "organic",
    "6": "poison",
    "7": "radioactive",
    "8": "corrosive",
    "9": "other",
}

ISO3 = {
    "AT": "AUT", "BE": "BEL", "BG": "BGR", "CH": "CHE", "CZ": "CZE", "DE": "DEU", "DK": "DNK",
    "EE": "EST", "ES": "ESP", "FI": "FIN", "FR": "FRA", "GB": "GBR", "GR": "GRC", "HR": "HRV",
    "HU": "HUN", "IE": "IRL", "IT": "ITA", "LT": "LTU", "LU": "LUX", "LV": "LVA", "NL": "NLD",
    "NO": "NOR", "PL": "POL", "PT": "PRT", "RO": "ROU", "SE": "SWE", "SI": "SVN", "SK": "SVK",
    "TR": "TUR", "UA": "UKR",
}


class RoutingError(RuntimeError):
    pass


@dataclass
class Route:
    provider: str  # "HERE" or a description of how a demo route was made
    distance_km: float
    drive_seconds: float  # truck driving time, with traffic when the provider supports it
    base_seconds: float  # driving time without traffic
    origin: tuple[float, float]
    destination: tuple[float, float]
    points: list[tuple[float, float]]
    departure: str | None = None
    warnings: list[str] = field(default_factory=list)
    computed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def traffic_delay_seconds(self) -> float:
        return max(0.0, self.drive_seconds - self.base_seconds)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Route:
        data = json.loads(text)
        data["origin"] = tuple(data["origin"])
        data["destination"] = tuple(data["destination"])
        data["points"] = [tuple(p) for p in data["points"]]
        return cls(**data)


def has_key() -> bool:
    return bool(os.getenv("HERE_API_KEY")) and not extractor.is_public_demo()


def route_for(order: FreightOrder, source_name: str, departure: datetime | None) -> Route | None:
    """Live HERE route when a key is configured, a stored route for the bundled samples otherwise."""
    if has_key():
        return here_route(order, departure)
    stored = ROUTES_DIR / f"{PurePath(source_name).stem}.json"
    return Route.from_json(stored.read_text(encoding="utf-8")) if stored.exists() else None


def _address(stop: Stop) -> str:
    parts = [stop.address, " ".join(p for p in (stop.postal_code, stop.city) if p), stop.country]
    return ", ".join(p for p in parts if p)


def geocode(stop: Stop, client: httpx.Client) -> tuple[float, float]:
    if not stop.city and not stop.postal_code:
        raise RoutingError("A city or postal code is needed to find the address on the map.")
    params = {"q": _address(stop), "limit": 1, "apiKey": os.environ["HERE_API_KEY"]}
    if iso3 := ISO3.get((stop.country or "").upper()):
        params["in"] = f"countryCode:{iso3}"
    items = _get(client, GEOCODE_URL, params).get("items") or []
    if not items:
        raise RoutingError(f"HERE could not find '{_address(stop)}'.")
    position = items[0]["position"]
    return position["lat"], position["lng"]


def here_route(order: FreightOrder, departure: datetime | None) -> Route:
    with httpx.Client(timeout=30) as client:
        origin = geocode(order.pickup, client)
        destination = geocode(order.delivery, client)
        params = {
            "transportMode": "truck",
            "origin": f"{origin[0]},{origin[1]}",
            "destination": f"{destination[0]},{destination[1]}",
            "return": "summary,polyline",
            "spans": "notices",
            "apiKey": os.environ["HERE_API_KEY"],
            **TRUCK,
        }
        if departure is not None and departure > datetime.now(timezone.utc):
            params["departureTime"] = departure.isoformat(timespec="seconds")
        warnings: list[str] = []
        hazard = _hazard(order.cargo)
        if hazard:
            params["vehicle[shippedHazardousGoods]"] = hazard
        try:
            data = _get(client, ROUTES_URL, params)
        except RoutingError:
            if not hazard:
                raise
            params.pop("vehicle[shippedHazardousGoods]")
            data = _get(client, ROUTES_URL, params)
            warnings.append(f"HERE did not accept hazardous goods type '{hazard}'; check ADR restrictions manually.")

    routes = data.get("routes") or []
    if not routes:
        notices = "; ".join(n.get("title", "") for n in data.get("notices", []))
        raise RoutingError(f"HERE found no truck route. {notices}".strip())

    sections = routes[0]["sections"]
    points: list[tuple[float, float]] = []
    for section in sections:
        points.extend(flexpolyline.decode(section["polyline"]))
        for notice in section.get("notices", []):
            if notice.get("severity") == "critical" or notice.get("code", "").startswith("violated"):
                warnings.append(notice.get("title") or notice.get("code"))
    return Route(
        provider="HERE",
        distance_km=round(sum(s["summary"]["length"] for s in sections) / 1000, 1),
        drive_seconds=sum(s["summary"]["duration"] for s in sections),
        base_seconds=sum(s["summary"].get("baseDuration", s["summary"]["duration"]) for s in sections),
        origin=origin,
        destination=destination,
        points=simplify(points),
        departure=params.get("departureTime"),
        warnings=sorted(set(warnings)),
    )


def _hazard(cargo: Cargo) -> str | None:
    if not cargo.adr:
        return None
    return ADR_TO_HERE.get((cargo.adr_class or "").strip()[:1], "other")


def _get(client: httpx.Client, url: str, params: dict) -> dict:
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise RoutingError(f"Could not reach HERE: {exc}") from exc
    if response.status_code in (401, 403):
        raise RoutingError("HERE rejected the API key. Check HERE_API_KEY in .env.")
    if response.status_code == 429:
        raise RoutingError("HERE rate limit reached. Try again in a minute.")
    if response.status_code >= 400:
        try:
            detail = response.json().get("title", "")
        except ValueError:
            detail = response.text[:200]
        raise RoutingError(f"HERE error {response.status_code}: {detail}")
    return response.json()


def simplify(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Keep the map light: thin the line to at most MAX_MAP_POINTS, keeping both ends."""
    if len(points) <= MAX_MAP_POINTS:
        return points
    step = len(points) / (MAX_MAP_POINTS - 1)
    return [points[round(i * step)] for i in range(MAX_MAP_POINTS - 1)] + [points[-1]]
