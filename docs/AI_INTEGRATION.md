# Optional Gemini Invoice Enrichment

## Purpose
DocIntel retains its deterministic extractor as the baseline. When enabled, Gemini is called from the backend worker after deterministic extraction. It may fill missing header fields and line items only. Existing deterministic values are never overwritten. Every proposed field is marked as an AI candidate, then processed by the existing deterministic validation rules.

## Enable locally
Set these variables in the backend process environment:

```powershell
$env:DOCINTEL_LLM_ENABLED = "true"
$env:GEMINI_API_KEY = "YOUR_KEY"
$env:GEMINI_MODEL = "gemini-2.5-flash"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The key must remain in the backend environment. Never use a `VITE_*` variable, commit the key, paste it into an issue, or include it in screenshots. Restart the backend after changing environment variables.

To disable the provider, set `DOCINTEL_LLM_ENABLED=false` or remove it. The deterministic pipeline then makes no LLM request.

## Privacy and cost boundary
Google's published Gemini API pricing currently describes a free tier for selected models and states that free-tier content may be used to improve Google products. Free quotas, model availability and provider terms can change. This is not an appropriate default for confidential invoices. Use generated synthetic invoices for the public demo. Only send real customer documents after the data owner has explicitly approved the transfer and reviewed the provider's current terms. No paid fallback is configured.

## Reliability and validation
- Provider calls have a bounded timeout and input length.
- Responses are parsed as JSON and reduced to an allowlisted schema.
- Provider/network/response failures are recorded as safe document events; deterministic extraction continues.
- LLM suggestions fill gaps only and are labelled as unverified.
- Existing Decimal arithmetic, date, currency, duplicate and cross-document checks remain authoritative.
- AI confidence is a review indicator, not an empirically calibrated probability.

## Verify
1. With LLM disabled, upload a synthetic invoice and confirm ordinary deterministic extraction.
2. Enable the provider with a valid backend key and upload a synthetic invoice containing a field the rules extractor misses.
3. Confirm the health endpoint reports LLM enabled/configured.
4. Confirm the invoice displays the AI-assisted indicator and suggestion note.
5. Correct the suggested value manually and confirm correction history and deterministic validation update.
6. Remove the key or disable LLM and confirm ordinary processing continues.

Do not interpret a health response of `available: true` as proof of successful live inference; it confirms only that the feature is enabled and a key is configured.
