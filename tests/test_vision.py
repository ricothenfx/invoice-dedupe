"""vision: mock extraction, schema validation, and the opt-in API client."""
import json
import urllib.request

import pytest

from invoice_dedupe import photogen, vision


def make_photo(**over):
    fields = dict(
        vendor="Northwind Logistics, Ltd.",
        invoice_no="INV-2026-0001",
        invoice_date="2026-09-14",
        amount="12,500.00",
        tax_id="12-3456789",
        seed=3,
    )
    fields.update(over)
    return photogen.make_photo(**fields)


def test_mock_extracts_all_fields_from_fixture_photo():
    result = vision.extract_image(make_photo(), "image/png")
    assert result.fields["invoice_no"] == "INV-2026-0001"
    assert result.fields["amount"] == 12500
    assert result.fields["vendor_name"].startswith("NORTHWIND LOGISTICS")
    assert result.fields["tax_id"] == "12-3456789"
    assert str(result.fields["invoice_date"]) == "2026-09-14"
    assert result.confidence == 0.95
    assert result.missing == []
    # raw model response preserved verbatim for the audit trail
    payload = json.loads(result.raw_text)
    assert payload["invoice_no"] == "INV-2026-0001"
    assert 0.0 <= payload["extraction_confidence"] <= 1.0


def test_mock_reports_low_confidence_when_nothing_readable():
    result = vision.extract_image(photogen.render_text(["   "], seed=5), "image/png")
    assert result.confidence == 0.0
    assert result.missing == ["vendor_name", "invoice_no", "invoice_date", "amount"]
    assert not result.fields["invoice_no"]


def test_mode_is_mock_without_env(monkeypatch):
    monkeypatch.delenv(vision.VISION_URL_ENV, raising=False)
    assert vision.vision_mode() == "mock"
    monkeypatch.setenv(vision.VISION_URL_ENV, "https://example.invalid/v1/chat/completions")
    assert vision.vision_mode() == "api"


@pytest.mark.parametrize(
    "payload",
    [
        "not an object",
        {"vendor_name": 5},
        {"invoice_date": "tomorrow"},
        {"extraction_confidence": 1.5},
        {"extraction_confidence": "high"},
        {"amount": {"currency": "USD"}},
    ],
)
def test_validate_rejects_schema_violations(payload):
    with pytest.raises(vision.VisionLLMError):
        vision.validate_extraction(payload)


def test_validate_normalizes_valid_payload():
    out = vision.validate_extraction(
        {
            "vendor_name": " Acme ",
            "invoice_no": "INV-1",
            "invoice_date": "2026-01-02",
            "amount": "1,250.00",
            "tax_id": None,
            "extraction_confidence": 0.8,
        }
    )
    assert out["vendor_name"] == "Acme"
    assert out["tax_id"] == ""
    assert out["extraction_confidence"] == 0.8


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(
            {"choices": [{"message": {"content": '{"vendor_name": "Acme", "extraction_confidence": 0.9}'}}]}
        ).encode()


def test_api_client_builds_openai_compatible_request(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setenv(vision.VISION_URL_ENV, "https://api.example.com/v1/chat/completions")
    monkeypatch.setenv(vision.VISION_API_KEY_ENV, "secret")
    client, method = vision.build_client()
    assert method == "vision_llm_api"
    assert isinstance(client, vision.OpenAICompatibleVisionClient)
    client.opener = fake_urlopen

    raw = client.complete(make_photo(), "image/png")
    assert raw.startswith('{"vendor_name"')
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert "Bearer secret" in captured["headers"].get("Authorization", "")
    content = captured["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text" and "JSON" in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert captured["body"]["temperature"] == 0


def test_api_client_wraps_transport_errors():
    client = vision.OpenAICompatibleVisionClient("https://api.example.com")
    def boom(request, timeout=None):
        raise OSError("no route to host")
    client.opener = boom
    with pytest.raises(vision.VisionLLMError, match="vision API call failed"):
        client.complete(make_photo(), "image/png")
