# Investor Demo Product Roadmap

## Product position
**DocIntel — AI-Powered Invoice Processing and Business Data Automation.**

This branch extends the assessment application without changing the original repository. The current deterministic extractor remains the baseline and financial validation remains rule-based. Optional LLM enrichment is a second extraction opinion: it fills gaps only, is labelled as an AI candidate, and must be reviewed.

## Architecture
- React/Vite workspace remains the client.
- FastAPI remains the tenant-authenticated API.
- Existing SQLite records, document generations, audit events, and background worker remain the demo persistence layer.
- Gemini Developer API is an optional backend-only provider configured through environment variables. No API key is sent to the browser or stored in Git.
- Existing deterministic extraction and validation remain available when the provider is disabled or unavailable.
- Generated corrected outputs are separate derived artifacts; original uploads are immutable.

## Delivery phases
1. **LLM extraction foundation:** provider adapter, structured JSON, timeouts, safe errors, opt-in config, candidate provenance, deterministic validation, unit tests.
2. **Corrected outputs:** tenant-scoped CSV/XLSX export and corrected invoice report; correction history alongside original file.
3. **Business data automation:** invoice register, reusable column mapping templates, batch export, Google Sheets OAuth connector (only after user authorizes credentials).
4. **Finance operations:** review/approval states, supplier directory, sample purchase-order matching, audit trail.
5. **Investor experience:** accounts-payable dashboard, calculated metrics, synthetic demo dataset, guided end-to-end walkthrough.

## Initial configuration
Set only on the backend:
- `DOCINTEL_LLM_ENABLED=true`
- `GEMINI_API_KEY=<secret>`
- `GEMINI_MODEL=gemini-3.5-flash-lite` (override when a currently supported free-tier model changes)

Gemini free-tier data may be used by Google to improve its products. Therefore this demo integration must use synthetic/non-confidential invoices unless the data owner has explicitly approved the transfer after reviewing current provider terms. Free-tier quotas and model availability can change. No paid fallback is implemented.

## Demo-vs-production boundary
- Functional demo: Gemini enrichment when configured, existing OCR/rules/corrections, derived exports, and sample-data workflows.
- Connector not live until configured: Google Sheets requires OAuth or service-account setup and explicit spreadsheet access.
- Demo workflows must be labelled as such. Do not imply a live ERP connection.
- Current Render Free filesystem is ephemeral. Durable customer use requires managed database and object storage; no paid infrastructure is provisioned by this roadmap.

## Acceptance criteria
- LLM disabled by default and no network request is made in that state.
- Missing key or provider failure does not corrupt or replace the deterministic result.
- LLM suggestions never silently overwrite deterministic values.
- Every LLM suggestion is visibly marked and passed through existing validation.
- Keys are server-side only; provider error messages and logs never contain keys.
- Tenant authorization is enforced for all generated downloads and exports.
- Original document bytes remain unchanged.
