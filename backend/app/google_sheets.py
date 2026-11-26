"""Optional Google Sheets export using a server-side service account."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import time
from typing import Any


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class GoogleSheetsUnavailable(RuntimeError):
    """Google Sheets integration is not configured or could not be reached."""


def google_sheets_status() -> dict[str, Any]:
    configured = bool(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        and os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    )
    return {
        "configured": configured,
        "provider": "google_sheets",
        "mode": "service_account_append",
        "range": os.environ.get("GOOGLE_SHEETS_RANGE", "Invoices!A1"),
    }


def append_rows(values: list[list[Any]]) -> dict[str, Any]:
    """Append rows to the configured spreadsheet; credentials never leave server."""
    settings = google_sheets_status()
    if not settings["configured"]:
        raise GoogleSheetsUnavailable(
            "Google Sheets is not configured on the backend"
        )

    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[SHEETS_SCOPE]
        )
        credentials.refresh(Request())
    except (ImportError, ValueError, KeyError, TypeError) as error:
        raise GoogleSheetsUnavailable(
            "Google service-account configuration is invalid"
        ) from error
    except Exception as error:  # noqa: BLE001
        raise GoogleSheetsUnavailable(
            f"Google authentication failed: {type(error).__name__}"
        ) from error

    spreadsheet_id = os.environ["GOOGLE_SHEETS_SPREADSHEET_ID"].strip()
    range_name = os.environ.get("GOOGLE_SHEETS_RANGE", "Invoices!A1").strip()
    encoded_range = urllib.parse.quote(range_name, safe="!:$")
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        + urllib.parse.quote(spreadsheet_id, safe="")
        + "/values/" + encoded_range
        + ":append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"values": values}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + credentials.token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    result = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise GoogleSheetsUnavailable(
                f"Google Sheets API returned HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise GoogleSheetsUnavailable(
                f"Google Sheets request failed: {type(error).__name__}"
            ) from error

    if result is None:
        raise GoogleSheetsUnavailable("Google Sheets request did not return a response")

    updates = result.get("updates", {})
    return {
        "spreadsheet_id": spreadsheet_id,
        "updated_range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows", 0),
    }
