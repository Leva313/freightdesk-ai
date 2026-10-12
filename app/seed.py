"""Load the sample requests into the database.

    python -m app.seed            # only if the database is empty
    python -m app.seed --reset    # wipe orders and reload
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from . import db, documents, extractor, service

SAMPLE_INPUTS = extractor.SAMPLES_DIR / "inputs"

# Shown as already confirmed by a dispatcher, so the demo dashboard has every status.
PRECONFIRMED = {"01_email_en_paper_manchester_lyon", "05_transport_order_pdf_rotterdam_prague"}


def sample_files() -> list[Path]:
    return sorted(p for p in SAMPLE_INPUTS.iterdir() if p.suffix.lower() in documents.ACCEPTED_SUFFIXES)


def seed_on_start() -> None:
    """Fill an empty database at startup when FREIGHTDESK_SEED_ON_START=1 (used by the hosted demo)."""
    if os.getenv("FREIGHTDESK_SEED_ON_START") == "1":
        seed()


def seed(reset: bool = False) -> int:
    if reset:
        for order in db.list_orders():
            db.delete_order(order.id)
    elif not db.is_empty():
        return 0

    created = 0
    for path in sample_files():
        order_id = service.ingest_upload(path.name, path.read_bytes())
        if path.stem in PRECONFIRMED:
            order = db.get_order(order_id)
            db.update_order(order_id, order.data, status="confirmed")
        created += 1
    return created


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete existing orders first")
    args = parser.parse_args()
    mode = "live extraction with " + extractor.MODEL if extractor.is_live() else "demo data"
    print(f"Loaded {seed(args.reset)} sample orders ({mode}).")


if __name__ == "__main__":
    main()
