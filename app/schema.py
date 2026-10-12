"""Structured shape of a freight order, shared by the extractor, the database and the UI."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Party(BaseModel):
    company: str | None = Field(None, description="Company name of the customer who sent the request")
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None


class Stop(BaseModel):
    company: str | None = Field(None, description="Company at the pickup or delivery site")
    address: str | None = Field(None, description="Street and house number")
    postal_code: str | None = None
    city: str | None = None
    country: str | None = Field(None, description="ISO 3166-1 alpha-2 country code, e.g. DE, PL, FR")
    date_from: str | None = Field(None, description="Earliest date as YYYY-MM-DD")
    date_to: str | None = Field(None, description="Latest date as YYYY-MM-DD; same as date_from for a fixed day")
    time_window: str | None = Field(None, description="Opening hours or slot, e.g. 08:00-14:00")
    reference: str | None = Field(None, description="Loading or delivery reference number")


class Cargo(BaseModel):
    description: str | None = Field(None, description="What is being shipped, in English")
    weight_kg: float | None = Field(None, description="Total gross weight in kilograms")
    pallets: int | None = Field(None, description="Number of pallets or handling units")
    pallet_type: str | None = Field(None, description="EUR, industrial, half-pallet, etc.")
    loading_meters: float | None = None
    volume_m3: float | None = None
    stackable: bool | None = None
    adr: bool = Field(False, description="True if the cargo is dangerous goods under ADR")
    adr_class: str | None = None
    un_number: str | None = None
    temperature_controlled: bool = False
    temp_min_c: float | None = None
    temp_max_c: float | None = None


class FreightOrder(BaseModel):
    customer: Party
    pickup: Stop
    delivery: Stop
    cargo: Cargo
    vehicle_type: str | None = Field(
        None, description="Requested vehicle, e.g. tautliner 13.6 m, reefer, mega trailer, van 3.5 t"
    )
    price: float | None = Field(None, description="Price offered or requested by the customer")
    currency: str | None = Field(None, description="ISO 4217 code, e.g. EUR")
    customer_reference: str | None = None
    notes: str | None = Field(None, description="Special instructions not captured elsewhere, in English")
    language: str = Field(description="ISO 639-1 code of the language the request is written in")
    clarification_email: str | None = Field(
        None,
        description=(
            "Polite reply to the customer asking for every missing required detail, written in the "
            "request's language. Null when nothing required is missing."
        ),
    )


# Fields a dispatcher needs before a truck can be booked. The extractor is told the same
# rules; the app re-checks them so the status never depends on the model alone.
REQUIRED_FIELDS: dict[str, str] = {
    "pickup.city": "Pickup city",
    "pickup.country": "Pickup country",
    "pickup.date_from": "Pickup date",
    "delivery.city": "Delivery city",
    "delivery.country": "Delivery country",
    "cargo.description": "Cargo description",
    "cargo.weight_kg": "Cargo weight",
}


def get_path(order: FreightOrder, path: str):
    value = order
    for part in path.split("."):
        value = getattr(value, part)
    return value


def missing_fields(order: FreightOrder) -> list[str]:
    missing = [path for path in REQUIRED_FIELDS if get_path(order, path) in (None, "")]
    if order.cargo.adr and not (order.cargo.adr_class or order.cargo.un_number):
        missing.append("cargo.adr_class")
    if order.cargo.temperature_controlled and order.cargo.temp_min_c is None and order.cargo.temp_max_c is None:
        missing.append("cargo.temp_min_c")
    return missing


FIELD_LABELS: dict[str, str] = {
    **REQUIRED_FIELDS,
    "cargo.adr_class": "ADR class or UN number",
    "cargo.temp_min_c": "Temperature range",
}
