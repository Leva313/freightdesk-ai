"""Extract a structured FreightOrder from a freight request using Claude.

Without an API key the app runs in demo mode: the bundled sample requests resolve to
pre-extracted results in samples/expected, and any other document is rejected.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path

import anthropic

from .documents import Document
from .schema import FIELD_LABELS, REQUIRED_FIELDS, FreightOrder

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "medium")
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

_required = "\n".join(f"- {label} ({path})" for path, label in REQUIRED_FIELDS.items())

SYSTEM_PROMPT = f"""You are an intake assistant for a road freight forwarder in Europe. Dispatchers \
receive transport requests as emails, PDFs, spreadsheets and photos, in any language. Your job is to \
turn one request into a structured freight order so a dispatcher can book a truck without retyping it.

Extraction rules:
- Extract only what the request states or clearly implies. Leave a field null rather than guessing; \
a dispatcher will fill gaps, but a wrong address or date costs a wasted truck.
- Resolve relative dates ("tomorrow", "next Monday", "KW 43") against the request date given with \
the document, and write dates as YYYY-MM-DD.
- Convert weights to kilograms (1 t = 1000 kg) and countries to ISO 3166-1 alpha-2 codes.
- "customer" is whoever sends the request, not the loading or unloading site.
- Write cargo description and notes in English; keep names, addresses and references as written.
- Mark adr true only for dangerous goods (ADR class, UN number, "Gefahrgut", "towary niebezpieczne", \
etc.), and temperature_controlled true only when a temperature or reefer is requested.

A dispatcher needs these fields before booking:
{_required}
- ADR class or UN number, when the cargo is dangerous goods
- Temperature range, when the cargo is temperature controlled

If any of them are missing or ambiguous, write clarification_email: a short, polite reply to the \
customer in the language of the request that thanks them and asks for exactly the missing details. \
Sign it as "FreightDesk dispatch". If nothing required is missing, set clarification_email to null."""


class ExtractionError(RuntimeError):
    pass


class DemoModeError(ExtractionError):
    pass


def is_public_demo() -> bool:
    """Hosted demo: always demo mode, even if a key is present in the environment."""
    return os.getenv("FREIGHTDESK_DEMO") == "1"


def is_live() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY")) and not is_public_demo()


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract(document: Document, raw: bytes, received: date | None = None) -> FreightOrder:
    if not is_live():
        return _demo_extract(raw)
    return _claude_extract(document, received or date.today())


def _claude_extract(document: Document, received: date) -> FreightOrder:
    client = anthropic.Anthropic()
    content = [
        *document.blocks,
        {
            "type": "text",
            "text": f"Request received on {received.isoformat()} ({received:%A}). Extract the freight order.",
        },
    ]
    try:
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": EFFORT},
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_format=FreightOrder,
        )
    except anthropic.AuthenticationError as exc:
        raise ExtractionError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise ExtractionError("Rate limited by the Anthropic API. Try again in a minute.") from exc
    except anthropic.BadRequestError as exc:
        raise ExtractionError(f"The document could not be processed: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionError(f"Anthropic API error ({exc.status_code}). Try again later.") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionError("Could not reach the Anthropic API. Check the internet connection.") from exc

    if response.stop_reason == "refusal":
        raise ExtractionError("The model declined to process this document.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise ExtractionError("The extraction was cut off before it finished. Try a shorter document.")
    return response.parsed_output


def _demo_extract(raw: bytes) -> FreightOrder:
    digest = fingerprint(raw)
    for sample in (SAMPLES_DIR / "inputs").iterdir():
        if sample.is_file() and fingerprint(sample.read_bytes()) == digest:
            expected = SAMPLES_DIR / "expected" / f"{sample.stem}.json"
            return FreightOrder.model_validate_json(expected.read_text(encoding="utf-8"))
    hint = (
        "This public demo runs on pre-extracted sample requests, so uploading your own documents is disabled."
        if is_public_demo()
        else "Add ANTHROPIC_API_KEY to .env to process your own documents."
    )
    raise DemoModeError("Demo mode only processes the bundled sample requests. " + hint)


def label(path: str) -> str:
    return FIELD_LABELS.get(path, path)
