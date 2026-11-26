# Document Intelligence System

Upload invoices (PDF, JPG, PNG, TIFF, XLSX, XLS, CSV) → read text or OCR → extract structured fields with deterministic rules and optional Gemini AI enrichment → validate deterministically → review, correct, export and search. The LLM is optional, disabled by default, and never replaces financial validation.
Built for the Care Guide *Senior AI Engineer* assessment. **Status and honest limits: see [Known limitations](#known-limitations) and `docs/COMPLIANCE.md`.**

## Live demo (Render)

- **Frontend:** [https://docintel-frontend-4noz.onrender.com](https://docintel-frontend-4noz.onrender.com)
- **Backend API:** [https://docintel-api-hsjk.onrender.com](https://docintel-api-hsjk.onrender.com)
- **Backend health / OCR status:** [https://docintel-api-hsjk.onrender.com/api/health](https://docintel-api-hsjk.onrender.com/api/health)
- **Interactive API documentation:** [https://docintel-api-hsjk.onrender.com/docs](https://docintel-api-hsjk.onrender.com/docs)

The deployed backend health check was verified with `status: ok` and Tesseract OCR available (version 5.5.0). The deployed frontend loaded and a scanned PDF (`02_scanned_invoice.pdf`) was observed reaching `completed` status. These are deployment smoke tests, not a claim of production-grade availability or accuracy.

### Deploy configuration

The application is deployed as a Render Static Site (frontend) and a Render Docker Web Service (backend):

1. Set the frontend service root directory to `frontend`, build command to `npm ci && npm run build`, and publish directory to `dist`.
2. Set the frontend build environment variable `VITE_API_BASE_URL=https://docintel-api-hsjk.onrender.com`.
3. Set the backend service root directory to `backend` and use its Dockerfile. The image installs Tesseract OCR and the English language data.
4. On the backend service, set `DOCINTEL_CORS_ORIGINS=https://docintel-frontend-4noz.onrender.com`.
5. If API-key protection is enabled, set `DOCINTEL_API_KEY` as a private backend environment variable. Enter the same key in the frontend when the app requests it. **Never commit or publish the key**; do not put it in `VITE_*` variables or this README. The `/api/health` endpoint is public and does not require the key.

### Demo hosting limitations

The current Render free-tier deployment is intended for assessment/demo use. The backend may spin down after inactivity and the first request after idle time can take longer while it wakes. Its local filesystem is ephemeral: SQLite data and uploaded documents can be lost on restart, redeploy, or instance replacement. Do not treat this deployment as durable storage or upload confidential/production documents. Persistent storage and a managed database are required for a durable production deployment.


![Inspector with validation errors](docs/screenshots/02-validation-error.png)

## Features
- **Documents:** multi-file upload; digital PDFs (text layer), scanned PDFs/images/multi-frame TIFF (Tesseract OCR), XLSX/XLS (all sheets), CSV (delimiter detection); one-invoice-per-row Excel *registers*; multi-invoice files; corrupt/encrypted/empty/unsupported files handled with clear errors.
- **Extraction:** invoice number, date, supplier, customer, subtotal, tax, total, currency, and line items. The deterministic baseline leaves missing or ambiguous values empty. Optional Gemini enrichment can suggest missing values; suggestions are labelled for human verification and cannot overwrite deterministic values. AI confidence is a review heuristic, not a calibrated probability.
- **Validation** (stable rule IDs, `Decimal`, per-currency precision, configurable tolerance): required/invalid fields, dates, currencies, subtotal+tax=total, line arithmetic, duplicates, low OCR confidence, PDF↔PDF and PDF↔Excel cross-verification. Catalogue: `GET /api/rules`.
- **Review and outputs:** inline corrections with audit trail and automatic re-validation, open the immutable original, download a generated corrected-data PDF summary or corrected Excel, and export the active invoice register to CSV/XLSX.
- **Business automation:** accounts-payable dashboard, automatically populated supplier directory, invoice approval/rejection, sample purchase-order matching, and reusable Smart Data Mapping templates.
- **Google Sheets:** optional service-account integration that appends invoice data to a spreadsheet when credentials and sharing permissions are configured; setup: `docs/GOOGLE_SHEETS_SETUP.md`.
- **Search:** keyword (SQLite FTS5 + filters); optional meaning-based hybrid search.
- **Background processing:** DB-backed worker with progress, retry/backoff, atomic job claim, restart recovery, duplicate-upload protection; page-streaming so memory does not grow with page count.

## Architecture
![Architecture](docs/diagrams/architecture.png)

FastAPI + SQLite (WAL, FTS5) + PyMuPDF + Tesseract + React/Vite, one process. Details, trade-offs and failure modes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Schema/ERD: [docs/DATABASE.md](docs/DATABASE.md). API contract: [docs/openapi.json](docs/openapi.json) (live at `/docs`). Security: [docs/SECURITY.md](docs/SECURITY.md).

## Quick start (Windows PowerShell; the candidate ran this flow on Windows 11, see docs/test-results/windows-pytest.txt)
Prerequisites: Python 3.11+ (Linux: 3.12), Node 20+ (tested: 22 on Linux, 26 on Windows), [Tesseract OCR 5](https://github.com/UB-Mannheim/tesseract/wiki) (English; tested 5.3.4 on Linux, 5.4.0 on Windows). On Windows the app finds Tesseract in `C:\Program Files\Tesseract-OCR` automatically; otherwise set `$env:TESSERACT_CMD = "C:\path\to\tesseract.exe"`. `GET /api/health` (and a banner in the UI) report whether OCR is available.
```powershell
cd backend
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000      # API + background worker
```
New terminal:
```powershell
cd frontend; npm install; npm run dev                              # http://localhost:5173 (proxies /api to :8000)
```
Data (SQLite file + uploads) lives in `backend/data/` (`DOCINTEL_DATA_DIR`). The schema is created and migrated automatically on start; there is nothing else to set up. Demo files: `python scripts\seed_demo.py` → `demo_files\`.
On Linux/macOS use `source .venv/bin/activate` and `export` instead of `$env:`.

## Configuration (environment variables)
| variable | default | meaning |
|---|---|---|
| `DOCINTEL_DATA_DIR` | `./data` | database and uploads |
| `DOCINTEL_LLM_ENABLED` | `false` | enable optional Gemini invoice enrichment |
| `GEMINI_API_KEY` | unset | private backend-only Gemini API key; never expose in frontend or commit |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | free-tier Gemini model default; confirm current availability before use |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | unset | private backend-only Google service-account JSON credential |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | unset | destination spreadsheet ID; share it with the service account |
| `GOOGLE_SHEETS_RANGE` | `Invoices!A1` | destination A1 range for appended rows |
| `DOCINTEL_API_KEY` | unset (open) | if set, every endpoint except `/api/health` requires header `X-API-Key` |
| `DOCINTEL_CORS_ORIGINS` | Vite dev origins | comma-separated allowed origins |
| `DOCINTEL_MAX_UPLOAD_MB` | 200 | per-file upload limit |
| `DOCINTEL_WORKERS` | 1 | background worker threads |
| `DOCINTEL_MAX_ATTEMPTS` / `DOCINTEL_RETRY_BACKOFF_S` | 3 / 2 | retries for transient errors |
| `DOCINTEL_OCR_DPI` / `_OCR_LANG` / `_OCR_TIMEOUT_S` | 200 / eng / 90 | OCR rendering and limits |
| `DOCINTEL_OCR_THREADS` | 1 | pages OCR'd concurrently (memory ≈ +80–100 MB each) |
| `DOCINTEL_TESS_OMP_THREADS` | 1 | threads per Tesseract process (0 = Tesseract default) |
| `DOCINTEL_PDF_STORE_SHRINK` | 1 | flush MuPDF image cache per OCR page (halves peak memory) |
| `DOCINTEL_OCR_FIELD_MIN_CONF` | 60 | flag values whose weakest OCR word is below this (0 disables) |
| `DOCINTEL_TOLERANCE_MINOR_UNITS` | 1 | rounding tolerance in minor currency units |
| `DOCINTEL_SEMANTIC` | off | `off` / `glove` / `sbert`; see `docs/SEMANTIC.md` |
| `DOCINTEL_MODELS_DIR`, `DOCINTEL_GLOVE_VOCAB`, `DOCINTEL_SBERT_MODEL`, `DOCINTEL_CHUNK_CHARS` | – | semantic-search model location/size/chunking |
| `TESSERACT_CMD` | `tesseract` | path to the Tesseract executable |
| `DOCINTEL_BACKUP_ENABLED` | `false` | enable the single-instance Render backup scheduler |
| `DOCINTEL_PERSISTENT_DISK_MOUNT` | `/var/data` | Render persistent disk mount point |
| `DOCINTEL_BACKUP_DIR` | `/var/data/backups` | encrypted backup directory on the Render persistent disk |
| `DOCINTEL_BACKUP_PASSWORD` | unset | private AES-256-GCM backup password (12+ characters; required when enabled) |
| `DOCINTEL_BACKUP_UTC_HOUR` / `_UTC_MINUTE` | `20` / `0` | daily UTC schedule (02:00 Bangladesh time) |
| `DOCINTEL_BACKUP_LIMIT_GIB` | `8.5` | maximum encrypted backup allocation |

## API
Runtime OpenAPI contract: `/docs` (generated by FastAPI for the running branch). `docs/openapi.json` is the original assessment snapshot and has not yet been regenerated for these extensions. Main endpoints:
| method | path | purpose |
|---|---|---|
| POST | `/api/documents` | multi-file upload (multipart `files`); per-file result `queued` / `duplicate` / `rejected` |
| GET | `/api/documents`, `/api/documents/{id}` | list with job progress; detail (invoices paginated: `invoice_limit`, `invoice_offset`) |
| GET | `/api/documents/{id}/file` | the original upload (for review) |
| GET | `/api/invoices/{id}/corrected.pdf` · `/corrected.xlsx` | generated corrected-data summary PDF / corrected invoice workbook; original unchanged |
| GET | `/api/exports/invoices.csv` · `/api/exports/invoices.xlsx` | tenant-scoped invoice register export; optional `mapping_id` for mapped columns |
| POST | `/api/exports/google-sheets?mapping_id=` | append invoice register to configured Google Sheet |
| GET · POST | `/api/business/dashboard`, `/api/business/suppliers`, `/api/business/purchase-orders`, `/api/business/mappings` | business dashboard, supplier directory, demo POs and reusable mappings |
| GET · POST | `/api/invoices/{id}/approval` · GET `/api/invoices/{id}/purchase-order-match` | approval state and PO comparison |
| POST | `/api/documents/{id}/reprocess` · DELETE `/api/documents/{id}` | re-run extraction · delete everything about a document |
| GET · POST | `/api/jobs?status=failed` · `/api/jobs/retry-failed` | failed-job inspection and re-queue |
| GET · PATCH | `/api/invoices/{id}` | read / correct fields and line items (audited, re-validated) |
| GET | `/api/invoices/{id}/corrections` | audit trail |
| POST | `/api/issues/{id}/resolve` | mark a warning/error reviewed |
| GET | `/api/search?q=&supplier=&customer=&currency=&status=&review=&date_from=&date_to=` | keyword search + filters |
| GET | `/api/search/semantic?q=&mode=hybrid\|semantic` · `/api/semantic/status` · POST `/api/semantic/reindex` | optional meaning-based search |
| GET | `/api/rules` · `/api/health` | rule catalogue · health |

```http
PATCH /api/invoices/12
{"fields": {"total": "1,234.50"}, "line_items": [{"idx": 2, "quantity": "7"}, {"description": "Freight", "amount": "40"}], "note": "OCR misread"}
```

## Tests
```powershell
cd backend; python -m pytest tests -q
```
Historical Windows verification on the previously deployed source line: **130 passed, 1 skipped** (19.13 s; 2 dependency deprecation warnings). See `docs/test-results/windows-pytest.txt` for that recorded run. Latest verification in the isolated `feature/production-persistence-hardening` worktree (not yet deployed): **238 passed, 3 skipped, 0 failed** (31.44 s; 2 dependency deprecation warnings). See `docs/test-results/production-persistence-hardening-pytest.txt`. The Linux sandbox run is recorded separately in `docs/test-results/day2-pytest.txt`. Tests cover file readers and OCR for every type, corrupt/encrypted/unsupported files, extraction, validation, duplicate and cross-document checks, corrections, worker retry/atomic claim/recovery, API endpoints, security controls, semantic search (fake embedder), and OCR-confidence flagging. Key tests were mutation-checked (they fail when production code is deliberately broken).

## Evaluation and benchmarks (all measured; sandbox = 1 CPU core, 4 GB RAM)
- **Extraction accuracy** on labeled **synthetic** invoices (three layouts, all seven file types, degraded scans, missing/ambiguous fields, tampered totals, cross-document pairs, 18 broken files): header-field exact match 99.4–99.5% on two held-out splits (199 invoices each), line items ≈ 98–99%, 100% of valid files processed, 18/18 broken files handled. **These are upper bounds: the data share an author with the extractor and no real invoices were used.** Methodology, per-file-type tables, calibration and limits: [docs/evaluation/EVALUATION.md](docs/evaluation/EVALUATION.md).
- **Large documents (1,000 pages):** development sandbox (1 core): digital ~3.7 s / 176 MB, scanned ~430 s (~140 pages/min), Python peak ~217 MB, no failures ([docs/benchmarks/RESULTS.md](docs/benchmarks/RESULTS.md)). **Candidate's Windows 11 machine (reported, raw data pending):** digital 9.15 s / 87 MB, scanned **321.9 s (186 pages/min), peak 144 MB, no failures**; throughput flat from 10 to 1,000 pages ([docs/benchmarks/WINDOWS_REPORTED.md](docs/benchmarks/WINDOWS_REPORTED.md)). CPU/core count of that machine was not reported.
- Reproduce: `python eval/make_dataset.py --split test; python eval/run_eval.py --split test; python eval/report_eval.py` and `python eval/bench.py run --kind scanned --pages 100`.

## Investor demo

- Product walkthrough: [docs/INVESTOR_DEMO_SCRIPT.md](docs/INVESTOR_DEMO_SCRIPT.md).
- Architecture: [docs/diagrams/investor-demo-architecture.mmd](docs/diagrams/investor-demo-architecture.mmd).

## Optional AI and Google Sheets integrations

- Gemini setup and data-handling limits: [docs/AI_INTEGRATION.md](docs/AI_INTEGRATION.md).
- Google Sheets service-account setup: [docs/GOOGLE_SHEETS_SETUP.md](docs/GOOGLE_SHEETS_SETUP.md).

## Optional semantic search
Off by default (`DOCINTEL_SEMANTIC=glove` after `python -m app.semantic download-glove`, run in `backend/`). Hybrid keyword+meaning ranking over a small local word-vector model; measured cost and quality, and why it is optional: [docs/SEMANTIC.md](docs/SEMANTIC.md).

## Demo
Script for the recorded walkthrough: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md). Screenshots (from a real run): `docs/screenshots/`.

## Troubleshooting (Windows)
- `node`/`npm` "not recognized" right after installing Node.js: close and reopen PowerShell (PATH is read at start); check with `& "C:\Program Files\nodejs\node.exe" --version`.
- Scanned PDFs/images fail with `ocr_unavailable`, or the tests stop with "Tesseract OCR was not found": install Tesseract, then set `$env:TESSERACT_CMD` (see above), restart the server and use *Retry all failed*.
- `/api/search/semantic` returns 503: semantic search is off by default. Enable it with `DOCINTEL_SEMANTIC=glove` after `python -m app.semantic download-glove`, then `POST /api/semantic/reindex`.
- Windows benchmark commands: `docs/benchmarks/WINDOWS_RUNBOOK.md`.

## Known limitations
- Evaluation data are synthetic; real-world accuracy is unknown and likely lower on unseen layouts, wrapped tables, discounts/shipping lines, tax-inclusive totals and noisy scans. No LLM extraction.
- Confidence scores are heuristic, not calibrated probabilities; 33–88% of extraction errors were flagged for review in the evaluation (depending on split/rule), the rest passed silently.
- The latest isolated-worktree backend test run is 238 passed / 3 skipped (0 failed; 2 warnings); benchmark figures in `docs/benchmarks/WINDOWS_REPORTED.md` are machine-specific and should not be interpreted as hosted Render performance. Multi-core scaling is unmeasured.
- Single-tenant: one optional shared API key, no users/roles, no TLS or encryption at rest, parsers are not sandboxed (see docs/SECURITY.md).
- SQLite single-process design; PostgreSQL/pgvector and a real queue would be the next step beyond roughly 250k vector chunks or multiple server processes.
- Cross-document verification compares header values only (not line items); English-language OCR only.

## Repository layout
`backend/app` (service) · `backend/tests` · `backend/synth` (synthetic invoice generators) · `frontend/` (React + Vite) · `eval/` (dataset, evaluation, benchmarks) · `scripts/` (demo data, docs generators) · `docs/` (architecture, database, security, evaluation, benchmarks, demo, screenshots) · `PROGRESS.md` · `AI_PROMPTS.md`.

## Production persistence and backup

For durable deployments, configure `DOCINTEL_DATA_DIR` on persistent storage rather than an ephemeral container filesystem. DocIntel provides an offline backup/restore utility and an optional single-instance Render scheduler. The Render scheduler pauses API traffic, stops the in-process worker, creates and verifies a SQLite/uploads snapshot, encrypts it, verifies the encrypted copy, and then applies 7-daily / 4-weekly / 3-monthly retention. Configure `/var/data/appdata` and `/var/data/backups` on the same paid Render persistent disk, enable `DOCINTEL_BACKUP_ENABLED=true`, and provide `DOCINTEL_BACKUP_PASSWORD` as a private environment variable. This protects against routine application restarts, not persistent-disk loss; independent off-site copies remain necessary. Full setup and recovery instructions: `docs/BACKUP_RESTORE.md`.
