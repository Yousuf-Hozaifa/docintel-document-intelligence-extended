# Final compliance matrix

Legend: ✅ implemented and verified · 🟡 implemented, with a stated limit or evidence still pending · ⬜ not done.
Evidence environments: **S** = development sandbox (Linux, 1 core, 4 GB; run by the assistant). **W** = candidate's Windows machine (reported by the candidate; raw data partly pending, see `docs/test-results/windows-pytest.txt`, `docs/benchmarks/WINDOWS_REPORTED.md`).
Every accuracy number is measured on **synthetic** data written by the same author as the extractor; it is an upper bound for real invoices.

## Assessment tasks
| # | Requirement | Status | Implementation | Evidence | Limits / open points |
|---|---|---|---|---|---|
| 1 | Accept multiple PDF, JPG, PNG, TIFF, XLSX, XLS, CSV | ✅ | `readers.py`, `main.py` (per-file validation) | S: `test_readers.py`, `test_api.py`; eval: all 7 types; W: demo files incl. xlsx/csv/jpg/png processed | |
| 1 | Digital PDF text · OCR for scans/images | ✅ | PyMuPDF text layer, else render + Tesseract | S/W: reader tests (W: 123 pass once Tesseract was configured); demo OCR docs processed on W | English OCR only. A missing Tesseract is now reported clearly (`ocr_unavailable`) |
| 1 | Multi-page, multi-sheet, multi-frame, 1,000-page PDFs | 🟡 | lazy page-streaming, bounded concurrency | S: 1,000 scanned pages 429.6 s, Python peak 217 MB; digital 3.7 s. W (reported): scanned 321.9 s, 144 MB peak; digital 9.15 s | W raw data and CPU/cores not supplied; dense-text 1,000 pages unmeasured |
| 1 | Poor scans, corrupt/unsupported files, status reporting | ✅ | page-level failure isolation, permanent vs transient errors, statuses | eval: degraded images, 18/18 broken files handled (×2 splits); S tests; W demo: corrupt PDF failed with a readable reason | silent OCR misreads remain (see T2) |
| 2 | Extract invoice no., date, supplier, customer, subtotal, tax, total, currency, line items as JSON | ✅ | `extract.py` (rules), register parser | `docs/evaluation/EVALUATION.md`: header exact match 99.5% / 99.4% (two held-out splits), line items P/R ≈ 98–99% | synthetic only; no real invoices |
| 2 | Missing/ambiguous not invented; evidence; review flag | ✅ | null + note, source line, `needs_review` | tests; eval (ambiguous dates, missing fields) | |
| 2 | Confidence: model vs calibrated | 🟡 | heuristic confidence + weakest OCR word confidence | EVALUATION.md §5.5: **not calibrated** (measured) | silent errors: 4/12 (test) and 14/16 (test2, with OCR rule) errors flagged |
| 2 | Schema-constrained LLM output | optional Gemini Developer API adapter; disabled by default; fills missing fields only and is followed by deterministic validation | `backend/app/llm.py`, `docs/AI_INTEGRATION.md` | free-tier content may be used to improve Google's products; synthetic invoices only; no paid fallback |
| 3 | Missing/invalid fields, dates, currencies, numbers | ✅ | `validate.py`, stable IDs (`GET /api/rules`) | `test_extract_validate.py`; eval rule detection | |
| 3 | Subtotal/tax/line/total checks with tolerance & currency precision | ✅ | `Decimal`, per-currency minor units, `DOCINTEL_TOLERANCE_MINOR_UNITS` | tests incl. JPY/rounding; eval | tolerance is global |
| 3 | Duplicates; PDF↔PDF; PDF↔Excel; cross-document | ✅ | `review.py` (symmetric flags, registers) | `test_review.py`; eval: 36/36 flawed documents flagged per split; stress 5,000-row register ~7 s; W demo reproduced the same statuses | header values only, not line items |
| 3 | Manual review and correction | ✅ | `corrections.py`, mark-reviewed, audit trail | tests; S browser run; W: correction of the tampered total verified (reported) | |
| 4 | REST: upload, status, results, search, corrections, failed-job inspection, reprocess | ✅ | `main.py`; also delete, original file, pagination | `docs/openapi.json`; tests | |
| 4 | Async processing, retry, duplicate protection | ✅ | DB-backed worker, backoff, atomic claim, SHA-256 dedupe, restart recovery | `test_api.py`, `test_review.py` | single process |
| 5 | Persist metadata, text, results, line items, issues, corrections, history | ✅ | SQLite schema | `docs/DATABASE.md` (generated from the real schema) | |
| 5 | Keyword search + filters | ✅ | FTS5 + SQL filters | tests; S/W: "Kestrel" search | |
| 5 | Semantic search | 🟡 | optional hybrid (GloVe; sbert adapter unrun) | `docs/SEMANTIC.md`: P@1 0.56 (hybrid) on 9 synthetic paraphrase queries; W: 9/9 documents indexed, queries answered | modest quality; off by default; no transformer measured |
| 6 | React UI: upload, progress, lists, inspect, validation, inline corrections, search | ✅ | `frontend/` | S: headless-Chromium runs (`docs/screenshots/`); W: reported manual checks (upload, inspect, correct, review, search) | cosmetic alignment issues |
| 7 | Automated tests (processing, OCR, spreadsheets, extraction, validation, API, failures) | ✅ | `backend/tests` | S: **131 pass** (mutation-checked key tests); W: 123 pass + 1 skip on the earlier 124-test version | latest code not re-run on W |
| 7 | Labeled dataset + measured metrics, methodology, sample size, environment, limits | ✅ | `eval/` | EVALUATION.md (precision/recall/exact match, per-type, success/failure rates, timing, calibration) | synthetic; W evaluation run not performed |

## Deliverables
| # | Deliverable | Status | Where |
|---|---|---|---|
| 1 | Git repository | 🟡 | complete history in `docintel.bundle`; **you must push it to a remote** |
| 2 | README and setup | ✅ | `README.md` (config table, API, tests, Windows troubleshooting); Windows flow reported working |
| 3 | Architecture diagram and database schema | ✅ | `docs/ARCHITECTURE.md`, `docs/diagrams/`, `docs/DATABASE.md` |
| 4 | Working backend API and frontend | ✅ | verified on S (browser) and W (reported) |
| 5 | PDF, image, Excel, CSV pipelines | ✅ | `backend/app/readers.py`, `extract.py` |
| 6 | Automated tests and results | ✅ | `docs/test-results/` (sandbox and Windows) |
| 7 | Evaluation report with measured metrics | ✅ | `docs/evaluation/EVALUATION.md` (+ `docs/benchmarks/`) |
| 8 | Recorded walkthrough | 🟡 | script, demo data and screenshots ready (`docs/DEMO_SCRIPT.md`); **you must record it** |
| 9 | AI prompts and disclosure | 🟡 | `AI_PROMPTS.md` (two AI assistants disclosed, session log kept); **you must complete the candidate section** |

## Open items before submitting
1. Re-run `python -m pytest tests -q` on Windows with the current code (expect 131 passed).
2. Provide the raw `docs/benchmarks/results.jsonl` (and machine CPU/cores/RAM) so `WINDOWS_REPORTED.md` can be replaced by regenerated tables; optionally run the OCR-threads matrix.
3. Record the walkthrough and add its link to the submission email.
4. Complete the candidate section in `AI_PROMPTS.md`; push the repository.
5. Know the limits: synthetic evaluation, rule-based extraction, single shared API key, no sandboxing of parsers (`docs/SECURITY.md`).
