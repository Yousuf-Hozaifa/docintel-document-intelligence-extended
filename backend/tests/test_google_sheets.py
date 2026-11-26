import json

import pytest

from app.google_sheets import GoogleSheetsUnavailable, append_rows, google_sheets_status


def test_google_sheets_status_is_unconfigured_by_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_MAP", raising=False)
    status = google_sheets_status()
    assert status["configured"] is False
    assert "private_key" not in json.dumps(status)


def test_google_sheets_append_requires_server_side_configuration(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_MAP", raising=False)
    with pytest.raises(GoogleSheetsUnavailable, match="not configured"):
        append_rows([["Invoice Number"], ["INV-1"]], tenant_id=1)



def test_google_sheets_spreadsheet_mapping_is_tenant_specific(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type":"service_account","private_key":"hidden"}')
    monkeypatch.setenv(
        "GOOGLE_SHEETS_SPREADSHEET_MAP",
        '{"1":"tenant-one-sheet","2":"tenant-two-sheet"}',
    )
    assert google_sheets_status(tenant_id=1)["configured"] is True
    assert google_sheets_status(tenant_id=2)["configured"] is True
    assert google_sheets_status(tenant_id=3)["configured"] is False
    assert "private_key" not in json.dumps(google_sheets_status(tenant_id=1))
