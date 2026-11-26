import json

import pytest

from app.google_sheets import GoogleSheetsUnavailable, append_rows, google_sheets_status


def test_google_sheets_status_is_unconfigured_by_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
    status = google_sheets_status()
    assert status["configured"] is False
    assert "private_key" not in json.dumps(status)


def test_google_sheets_append_requires_server_side_configuration(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
    with pytest.raises(GoogleSheetsUnavailable, match="not configured"):
        append_rows([["Invoice Number"], ["INV-1"]])
