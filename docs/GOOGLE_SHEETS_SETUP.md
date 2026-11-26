# Google Sheets Export Setup

DocIntel can append the active tenant's invoice register to a spreadsheet through the Google Sheets API. This is a real server-to-server connector, not a simulated success response.

## Configure Google Cloud
1. Create or select a Google Cloud project.
2. Enable the Google Sheets API.
3. Create a service account and generate a JSON key for the demo.
4. Open the destination spreadsheet and share it with the service-account email address as an Editor.
5. Copy each spreadsheet ID from its URL. Get tenant IDs from the application database (`SELECT id, name FROM tenants`).

Google documents service accounts as suitable for application-owned data and specific shared documents. The service account must be granted access to the individual spreadsheet. See [Google service-account authentication](https://developers.google.com/identity/protocols/oauth2/service-account) and [credential setup](https://developers.google.com/workspace/guides/create-credentials).

## Backend environment variables
Set these only on the backend service or local backend process:

- `GOOGLE_SERVICE_ACCOUNT_JSON`: the complete service-account JSON credential, stored as a private secret.
- `GOOGLE_SHEETS_SPREADSHEET_MAP`: a JSON object mapping each tenant ID to that tenant's spreadsheet ID, for example `{"1":"spreadsheet_id_for_tenant_1"}`.
- `GOOGLE_SHEETS_RANGE`: optional A1 range, default `Invoices!A1`.

Never put the credential in frontend variables, source control, screenshots, or client-side storage. The service account key is a powerful secret; restrict spreadsheet sharing and rotate/revoke the key if exposed.

After configuration, `GET /api/business/google-sheets/status` reports `configured=true` for the authenticated tenant. This checks configuration presence, not live access. The export action verifies actual authorization and writes rows.

## Export behavior
- The default export appends invoice register columns and serialized line-item detail.
- A saved Smart Data Mapping template can be selected to export company-specific column names.
- Exports append rows; they do not overwrite the spreadsheet.
- The API returns the updated range and row count.
- Provider errors are surfaced without exposing the credential or access token.

Google states that Sheets API use has no additional charge and documents per-minute quotas (300 reads/writes per project per minute and 60 per user per project per minute in the published baseline). Quotas can vary by project and can change; use exponential backoff for rate limits. See [Sheets API usage limits](https://developers.google.com/workspace/sheets/api/limits).

## Privacy
Export only invoice data that the workspace owner has approved for the destination spreadsheet. Google Sheets access is controlled by the sharing permissions on that spreadsheet. This integration is not a substitute for a production data-governance review.
