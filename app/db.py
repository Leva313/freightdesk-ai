"""SQLite storage for freight orders."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .routing import Route
from .schema import FreightOrder, missing_fields

DATA_DIR = Path(os.getenv("FREIGHTDESK_DATA", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "freightdesk.db"
UPLOADS_DIR = DATA_DIR / "uploads"

STATUSES = ("new", "needs_info", "confirmed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_file TEXT,
    source_text TEXT,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    demo INTEGER NOT NULL DEFAULT 0,
    route TEXT,
    route_error TEXT
);
"""


@dataclass
class Order:
    id: int
    created_at: datetime
    updated_at: datetime
    source_name: str
    source_kind: str
    source_file: str | None
    source_text: str | None
    status: str
    data: FreightOrder
    demo: bool
    route: Route | None = None
    route_error: str | None = None

    @property
    def missing(self) -> list[str]:
        return missing_fields(self.data)

    @property
    def lane(self) -> str:
        def place(stop):
            return ", ".join(part for part in (stop.city, stop.country) if part) or "?"

        return f"{place(self.data.pickup)} → {place(self.data.delivery)}"


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        for column in ("route", "route_error"):
            if column not in columns:  # databases created before routing existed
                conn.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        source_name=row["source_name"],
        source_kind=row["source_kind"],
        source_file=row["source_file"],
        source_text=row["source_text"],
        status=row["status"],
        data=FreightOrder.model_validate_json(row["data"]),
        demo=bool(row["demo"]),
        route=Route.from_json(row["route"]) if row["route"] else None,
        route_error=row["route_error"],
    )


def initial_status(order: FreightOrder) -> str:
    return "needs_info" if missing_fields(order) else "new"


def create_order(
    data: FreightOrder,
    source_name: str,
    source_kind: str,
    source_file: str | None,
    source_text: str | None,
    demo: bool,
) -> int:
    now = _now()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO orders (created_at, updated_at, source_name, source_kind, source_file, source_text,"
            " status, data, demo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, now, source_name, source_kind, source_file, source_text, initial_status(data),
             data.model_dump_json(), int(demo)),
        )
        return cursor.lastrowid


def get_order(order_id: int) -> Order | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _row_to_order(row) if row else None


def list_orders(status: str | None = None) -> list[Order]:
    query = "SELECT * FROM orders"
    params: tuple = ()
    if status in STATUSES:
        query += " WHERE status = ?"
        params = (status,)
    with connect() as conn:
        rows = conn.execute(query + " ORDER BY created_at DESC, id DESC", params).fetchall()
    return [_row_to_order(row) for row in rows]


def count_by_status() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()
    counts = {status: 0 for status in STATUSES}
    counts.update({row["status"]: row["n"] for row in rows})
    return counts


def update_order(order_id: int, data: FreightOrder, status: str | None = None) -> None:
    if status is None:
        current = get_order(order_id)
        keep_confirmed = current is not None and current.status == "confirmed"
        status = "confirmed" if keep_confirmed and not missing_fields(data) else initial_status(data)
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET data = ?, status = ?, updated_at = ? WHERE id = ?",
            (data.model_dump_json(), status, _now(), order_id),
        )


def set_route(order_id: int, route: Route | None, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET route = ?, route_error = ? WHERE id = ?",
            (route.to_json() if route else None, error, order_id),
        )


def delete_order(order_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))


def is_empty() -> bool:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def dumps(order: Order) -> dict:
    return {
        "id": order.id,
        "created_at": order.created_at.isoformat(),
        "status": order.status,
        "source_name": order.source_name,
        "missing_fields": order.missing,
        "order": json.loads(order.data.model_dump_json()),
    }
