# DocIntel Investor Demo Walkthrough

**Target length:** 6–8 minutes. Use synthetic invoices only.

## Before the demo
1. Pull the investor-demo feature branch and install backend/frontend dependencies.
2. Configure the backend Gemini API key and enable enrichment:
   - `DOCINTEL_LLM_ENABLED=true`
   - `GEMINI_API_KEY=<private key>`
   - `GEMINI_MODEL=gemini-3.5-flash-lite`
3. Confirm `GET /api/health` reports `llm.available=true`. This confirms configuration only; run a synthetic invoice through the pipeline to verify actual inference.
4. If demonstrating Google Sheets, configure `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEETS_SPREADSHEET_MAP` with a separate spreadsheet ID for the demo tenant, and share that spreadsheet with the service-account email. Confirm `GET /api/business/google-sheets/status` returns `configured=true` for the demo tenant.
5. Prepare a sample PO whose supplier, currency and total match one synthetic invoice.
6. Do not upload real customer invoices to the Gemini free tier. Free-tier content may be used by Google to improve its products.

## Walkthrough
| Step | Show | Explain |
|---|---|---|
| 1 | Accounts Payable Overview dashboard | The dashboard calculates document/invoice counts, review backlog, approvals, exceptions and totals separated by currency from this workspace's records. |
| 2 | Upload a batch of synthetic PDF, image, Excel and CSV invoices | One upload workflow accepts multiple formats. OCR is used for scanned documents; digital text is extracted directly. |
| 3 | Open an invoice with an AI-assisted field, if the test invoice produces one | Gemini is an optional second extraction opinion. It fills missing values only; it cannot overwrite deterministic values. The field is explicitly marked as an AI suggestion and must be reviewed. |
| 4 | Open original file and compare with invoice fields | The original upload is immutable. Evidence, validation findings and corrections remain traceable. |
| 5 | Correct an intentionally inaccurate value and save | DocIntel records the correction and re-runs deterministic validation. |
| 6 | Show the corrected PDF summary and corrected Excel download beside Open original | These are separate generated outputs. The PDF is a clean corrected-data summary, not a pixel-perfect reconstruction of the source invoice layout. |
| 7 | Approve the invoice | Approval is audited. Unresolved blocking validation errors prevent approval. |
| 8 | Add a sample purchase order and click Check PO match | The demo currently compares supplier, currency and invoice total against POs entered in this workspace. It is not yet a full goods-received / three-way match. |
| 9 | Create a Smart Data Mapping template | Map DocIntel fields to the company's spreadsheet column names once, then reuse the mapping for later exports. |
| 10 | Export mapped Excel/CSV; optionally send to Google Sheets | Excel/CSV downloads work locally. Google Sheets writes only when the real service-account connector is configured and authorized. |
| 11 | Return to dashboard and supplier directory | Supplier names are populated from extracted invoices; metrics refresh as records are processed. |

## What to say accurately
- The LLM is real when enabled and configured; it is optional and disabled by default.
- The deterministic extractor remains the baseline. AI suggestions are not verified financial facts.
- The free-tier Gemini model and quota can change. The current default is `gemini-3.5-flash-lite`; verify the current pricing and model availability before each public demo.
- Gemini free-tier requests may be used to improve Google's products. Use synthetic demo invoices only.
- Google Sheets is a real service-account connector when configured; otherwise it is not connected.
- PO matching is currently a supplier/currency/total comparison, not full three-way matching.
- Current Render Free storage is ephemeral. Do not represent this demo deployment as production-grade persistent storage.
- No live ERP integration is claimed.

## If integrations are not configured
Skip the corresponding live action and state clearly that the integration is optional and not configured in this demo environment. Do not simulate a successful provider response.
