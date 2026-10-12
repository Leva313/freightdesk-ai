"""FreightDesk AI: turns freight requests into structured orders for dispatchers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from . import db, documents, extractor, routing, schedule, seed, service  # noqa: E402
from .schema import FIELD_LABELS, FreightOrder, missing_fields  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent



@asynccontextmanager
async def lifespan(_: FastAPI):
    seed.seed_on_start()
    yield


app = FastAPI(title="FreightDesk AI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals.update(
    field_label=extractor.label, is_live=extractor.is_live, is_public_demo=extractor.is_public_demo, model=extractor.MODEL
)

STATUS_LABELS = {"new": "Ready to book", "needs_info": "Needs info", "confirmed": "Confirmed"}
templates.env.globals["status_label"] = STATUS_LABELS
templates.env.filters["num"] = lambda value: f"{value:f}".rstrip("0").rstrip(".")


def _span(delta: timedelta) -> str:
    minutes = max(0, round(delta.total_seconds() / 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} d" if days else "", f"{hours} h" if hours else "", f"{mins} min" if mins and not days else ""]
    return " ".join(p for p in parts if p) or "0 min"


templates.env.filters["span"] = _span
templates.env.filters["seconds_span"] = lambda seconds: _span(timedelta(seconds=seconds))
templates.env.filters["when"] = lambda moment: moment.strftime("%a %d %b, %H:%M")
templates.env.globals.update(
    in_tz=lambda moment, country: moment.astimezone(schedule.zone(country)),
    has_here_key=routing.has_key,
)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str | None = None):
    orders = db.list_orders(status)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "orders": orders,
            "plans": {order.id: service.trip_plan(order) for order in orders},
            "counts": db.count_by_status(),
            "active": status,
        },
    )


@app.get("/orders/new", response_class=HTMLResponse)
def new_order(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "new.html",
        {"samples": seed.sample_files(), "error": error, "accepted": ",".join(documents.ACCEPTED_SUFFIXES)},
    )


@app.post("/orders")
async def create_order(request: Request, text: str = Form(""), file: UploadFile | None = File(None)):
    try:
        if file is not None and file.filename:
            order_id = service.ingest_upload(file.filename, await file.read())
        elif text.strip():
            order_id = service.ingest_text(text)
        else:
            raise documents.UnsupportedDocument("Paste a request or choose a file.")
    except (documents.UnsupportedDocument, extractor.ExtractionError) as exc:
        return new_order(request, error=str(exc))
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.post("/samples/{name}")
def load_sample(request: Request, name: str):
    path = seed.SAMPLE_INPUTS / name
    if path not in seed.sample_files():
        raise HTTPException(404)
    try:
        order_id = service.ingest_upload(path.name, path.read_bytes())
    except extractor.ExtractionError as exc:
        return new_order(request, error=str(exc))
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


def _get_or_404(order_id: int) -> db.Order:
    order = db.get_order(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(request: Request, order_id: int, saved: bool = False, error: str | None = None):
    order = _get_or_404(order_id)
    return templates.TemplateResponse(
        request,
        "order.html",
        {"order": order, "plan": service.trip_plan(order), "saved": saved, "error": error, "schedule": schedule},
    )


def _form_to_order(form: dict[str, Any], current: FreightOrder) -> FreightOrder:
    data = current.model_dump()
    for key in ("cargo.adr", "cargo.temperature_controlled"):
        form.setdefault(key, "")
    for key, raw in form.items():
        if "." not in key and key not in data:
            continue
        value: Any = raw.strip() if isinstance(raw, str) else raw
        if key in ("cargo.adr", "cargo.temperature_controlled"):
            value = value == "on"
        elif key == "cargo.stackable":
            value = {"yes": True, "no": False}.get(value)
        elif value == "":
            value = None
        target = data
        *parents, leaf = key.split(".")
        for part in parents:
            target = target[part]
        target[leaf] = value
    return FreightOrder.model_validate(data)


@app.post("/orders/{order_id}")
async def save_order(request: Request, order_id: int):
    order = _get_or_404(order_id)
    form = dict(await request.form())
    action = form.pop("action", "save")
    try:
        updated = _form_to_order(form, order.data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return order_detail(request, order_id, error=f"Some values are invalid - {problems}")
    reroute = service.route_inputs_changed(order.data, updated)
    if action == "confirm":
        missing = [FIELD_LABELS.get(path, path) for path in missing_fields(updated)]
        if missing:
            db.update_order(order_id, updated)
            return order_detail(request, order_id, error="Fill in before confirming: " + ", ".join(missing))
        db.update_order(order_id, updated, status="confirmed")
    else:
        db.update_order(order_id, updated)
    if reroute and routing.has_key():
        service.refresh_route(order_id)
    return RedirectResponse(f"/orders/{order_id}?saved=1", status_code=303)


@app.post("/orders/{order_id}/route")
def recalculate_route(order_id: int):
    _get_or_404(order_id)
    service.refresh_route(order_id)
    return RedirectResponse(f"/orders/{order_id}#route", status_code=303)


@app.post("/orders/{order_id}/delete")
def delete_order(order_id: int):
    _get_or_404(order_id)
    db.delete_order(order_id)
    return RedirectResponse("/", status_code=303)


@app.get("/orders/{order_id}/source")
def order_source(order_id: int):
    order = _get_or_404(order_id)
    if not order.source_file:
        raise HTTPException(404)
    return FileResponse(db.UPLOADS_DIR / order.source_file)


@app.get("/export.xlsx")
def export(status: str = "confirmed"):
    content = service.export_xlsx(db.list_orders(status if status != "all" else None))
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="freightdesk-{status}-{date.today()}.xlsx"'},
    )


@app.get("/api/orders")
def api_orders(status: str | None = None):
    return JSONResponse([db.dumps(order) for order in db.list_orders(status)])


@app.get("/api/orders/{order_id}")
def api_order(order_id: int):
    return JSONResponse(db.dumps(_get_or_404(order_id)))


@app.get("/health")
def health():
    return {
        "status": "ok",
        "mode": "live" if extractor.is_live() else "demo",
        "routing": "HERE" if routing.has_key() else "stored sample routes",
    }
