import json
import pytest

from app import llm


def test_llm_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DOCINTEL_LLM_ENABLED", "false")
    assert llm.extract_candidates("Invoice INV-1") is None


def test_llm_json_parser_accepts_fenced_json():
    parsed = llm._extract_json('```json\n{"total":"10.00"}\n```')
    assert parsed["total"] == "10.00"


def test_llm_json_parser_rejects_non_object():
    with pytest.raises(llm.LLMUnavailable):
        llm._extract_json('["not", "an", "object"]')


def test_enrichment_only_fills_missing_fields(monkeypatch):
    monkeypatch.setenv("DOCINTEL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "extract_candidates", lambda _: {
        "invoice_number": "INV-2",
        "total": "99.00",
        "line_items": [],
    })
    result = {
        "extractor": "rules-v1",
        "fields": {
            "invoice_number": {"value": None},
            "total": {"value": "10.00"},
        },
        "line_items": [],
    }
    assert llm.enrich_invoice(result, "synthetic invoice text") is True
    assert result["fields"]["invoice_number"]["value"] == "INV-2"
    assert result["fields"]["total"]["value"] == "10.00"
    assert result["fields"]["invoice_number"]["source"] == "llm"
    assert result["extractor"] == "rules-v1+gemini"


def test_llm_status_never_exposes_key(monkeypatch):
    monkeypatch.setenv("DOCINTEL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    status = llm.llm_status()
    assert status["available"] is True
    assert "secret" not in json.dumps(status)



def test_gemini_request_uses_secret_header_and_parses_json(monkeypatch):
    monkeypatch.setenv("DOCINTEL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-demo-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    payload = {
        "candidates": [{
            "content": {"parts": [{
                "text": '{"invoice_number":"INV-42","total":"125.00","line_items":[]}'
            }]}
        }],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "totalTokenCount": 120,
        },
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert timeout == 25
        assert "secret-demo-key" not in request.full_url
        assert request.get_header("X-goog-api-key") == "secret-demo-key"
        return FakeResponse()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    result = llm.extract_candidates("Synthetic invoice INV-42 total 125.00")
    assert result["invoice_number"] == "INV-42"
    assert result["_usage"]["total_tokens"] == 120
    assert result["_usage"]["latency_ms"] >= 0
