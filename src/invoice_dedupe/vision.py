"""Vision-LLM extraction fallback for scans and photos (charter decision D6).

Layering: ``extraction.extract_pdf`` (pdfplumber) remains the primary path for
PDFs with a text layer. This module handles everything else — text-less PDFs
and uploaded images — by asking a vision language model for a structured
extraction.

Two clients share one contract:

* **Mock** (default, offline): reads fixture photos with the embedded-font
  recognizer (``ocr``) and emits the same JSON a real model would. CI and
  demos never touch the network.
* **Real** (opt-in via ``DEDUPE_VISION_LLM_URL``): an OpenAI-compatible
  ``/chat/completions`` call with the image as a base64 data URL, built with
  stdlib ``urllib`` — deliberately zero new dependencies.

The model returns JSON conforming to :data:`EXTRACTION_SCHEMA`; the raw
response is preserved verbatim for the audit trail (``invoices.raw_text``)
while the validated fields go into the parsed columns. Validation is a
hand-rolled schema check — no ``jsonschema`` dependency.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from typing import Callable

from . import ocr
from .extraction import ExtractionResult, _CORE_FIELDS, parse_amount, parse_date

VISION_URL_ENV = "DEDUPE_VISION_LLM_URL"
VISION_MODEL_ENV = "DEDUPE_VISION_LLM_MODEL"
VISION_API_KEY_ENV = "DEDUPE_VISION_LLM_API_KEY"

#: What we ask the model for. Kept explicit so the real-API path documents
#: the expected response shape (mirrored by ``validate_extraction``).
EXTRACTION_SCHEMA = {
    "vendor_name": "string or null — the vendor/company legal name",
    "invoice_no": "string or null — the invoice number exactly as printed",
    "invoice_date": "string YYYY-MM-DD or null — the invoice date",
    "amount": "number or string or null — the total amount due",
    "tax_id": "string or null — the tax/VAT registration number",
    "extraction_confidence": "number 0.0-1.0 — the model's own confidence",
}

MOCK_MODEL_CONFIDENCE = 0.95

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


class VisionLLMError(RuntimeError):
    """The vision-LLM call or its payload failed validation."""


def vision_mode() -> str:
    """``"api"`` when a real endpoint is configured, ``"mock"`` otherwise."""
    return "api" if os.environ.get(VISION_URL_ENV) else "mock"


def _field_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def validate_extraction(payload: object) -> dict:
    """Validate a model response against the extraction schema.

    Returns a normalized dict with ``vendor_name`` / ``invoice_no`` /
    ``tax_id`` as strings, ``invoice_date`` as a YYYY-MM-DD string or None,
    ``amount`` as a number-like value or None, and a float
    ``extraction_confidence`` in [0, 1]. Raises :class:`VisionLLMError` on
    schema violations (wrong types, unknown date format, confidence out of
    range) so bad model output never reaches the engine.
    """
    if not isinstance(payload, dict):
        raise VisionLLMError("extraction response is not a JSON object")
    out: dict = {}
    for key in ("vendor_name", "invoice_no", "tax_id"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise VisionLLMError(f"{key} must be a string or null")
        out[key] = value.strip() if isinstance(value, str) else ""
    date_value = payload.get("invoice_date")
    if date_value is not None:
        if not isinstance(date_value, str) or parse_date(date_value) is None:
            raise VisionLLMError("invoice_date must be a recognizable date string or null")
        out["invoice_date"] = date_value.strip()
    else:
        out["invoice_date"] = None
    amount = payload.get("amount")
    if amount is not None and not isinstance(amount, bool):
        if isinstance(amount, (int, float)):
            out["amount"] = amount
        elif isinstance(amount, str) and _NUMBER_RE.search(amount):
            out["amount"] = amount
        else:
            raise VisionLLMError("amount must be a number, numeric string, or null")
    else:
        out["amount"] = None
    confidence = payload.get("extraction_confidence", 0.0)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise VisionLLMError("extraction_confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise VisionLLMError("extraction_confidence must be within [0, 1]")
    out["extraction_confidence"] = float(confidence)
    return out


class MockVisionClient:
    """Offline stand-in for the vision model.

    Reads the image with the embedded-font recognizer and produces the same
    JSON contract as the real API. Arbitrary photos (not rendered with the
    fixture font) yield few or no labels and a low confidence — the honest
    behavior of a weak model, never guessed fields.
    """

    def complete(self, image: bytes, content_type: str) -> str:
        try:
            text = ocr.extract_text(image)
        except ocr.UnreadableImage:
            # e.g. a text-less PDF in mock mode: without a rasterizer the mock
            # "sees nothing" — report unreadable honestly, never guess
            text = ""
        from .extraction import parse_fields

        fields = parse_fields(text)
        found = sum(1 for name in _CORE_FIELDS if fields.get(name))
        confidence = round(MOCK_MODEL_CONFIDENCE * found / len(_CORE_FIELDS), 2)
        return json.dumps(
            {
                "vendor_name": _field_text(fields.get("vendor_name")) or None,
                "invoice_no": _field_text(fields.get("invoice_no")) or None,
                "invoice_date": (
                    fields["invoice_date"].isoformat() if fields.get("invoice_date") else None
                ),
                "amount": fields.get("amount"),
                "tax_id": _field_text(fields.get("tax_id")) or None,
                "extraction_confidence": confidence,
            },
            indent=2,
        )


class OpenAICompatibleVisionClient:
    """Minimal OpenAI-compatible chat-completions client (stdlib urllib only).

    Activated by setting ``DEDUPE_VISION_LLM_URL`` (e.g.
    ``https://api.openai.com/v1/chat/completions``), optionally
    ``DEDUPE_VISION_LLM_MODEL`` and ``DEDUPE_VISION_LLM_API_KEY``. The
    ``opener`` parameter exists for tests: inject a fake ``urlopen`` instead
    of hitting the network.
    """

    def __init__(
        self,
        url: str,
        model: str = "gpt-4o-mini",
        api_key: str = "",
        opener: Callable[..., object] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.url = url
        self.model = model
        self.api_key = api_key
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def build_request(self, image: bytes, content_type: str) -> dict:
        prompt = (
            "Extract the invoice fields from this document photo. "
            "Respond with ONLY a JSON object with these keys: "
            + "; ".join(f"{k}: {v}" for k, v in EXTRACTION_SCHEMA.items())
            + ". Use null for fields you cannot read; never guess."
        )
        data_url = (
            f"data:{content_type};base64," + base64.b64encode(image).decode("ascii")
        )
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

    def complete(self, image: bytes, content_type: str) -> str:
        body = json.dumps(self.build_request(image, content_type)).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise VisionLLMError(f"vision API call failed: {exc}") from exc
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionLLMError(f"unexpected vision API response shape: {exc}") from exc


def build_client() -> tuple[object, str]:
    """Return ``(client, method_label)`` per the environment configuration."""
    url = os.environ.get(VISION_URL_ENV)
    if not url:
        return MockVisionClient(), "vision_llm_mock"
    client = OpenAICompatibleVisionClient(
        url,
        model=os.environ.get(VISION_MODEL_ENV, "gpt-4o-mini"),
        api_key=os.environ.get(VISION_API_KEY_ENV, ""),
    )
    return client, "vision_llm_api"


def extract_image(data: bytes, content_type: str = "image/png") -> ExtractionResult:
    """Vision-LLM extraction: image bytes -> validated fields + raw response.

    The raw model response is preserved verbatim in ``raw_text`` (audit
    trail, charter §4); ``confidence`` combines the model's self-reported
    confidence with the fraction of core fields actually found.
    """
    client, method = build_client()
    raw = client.complete(data, content_type)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise VisionLLMError(f"vision response is not valid JSON: {exc}") from exc
    fields_json = validate_extraction(payload)

    fields = {
        "vendor_name": fields_json["vendor_name"],
        "invoice_no": fields_json["invoice_no"],
        "invoice_date": parse_date(fields_json["invoice_date"]),
        "amount": parse_amount(fields_json["amount"]),
        "tax_id": fields_json["tax_id"],
    }
    missing = [name for name in _CORE_FIELDS if not fields.get(name)]
    completeness = round((len(_CORE_FIELDS) - len(missing)) / len(_CORE_FIELDS), 2)
    confidence = round(fields_json["extraction_confidence"] * completeness, 2)
    return ExtractionResult(
        raw_text=raw,
        fields=fields,
        missing=missing,
        confidence=confidence,
    )
