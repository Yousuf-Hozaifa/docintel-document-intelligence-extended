# Original assessment walkthrough: script (target 7–7:30 min)

> This script documents the original Care Guide assessment build and its deterministic-only configuration. For the expanded investor-demo product, use [INVESTOR_DEMO_SCRIPT.md](INVESTOR_DEMO_SCRIPT.md); do not use the original script to describe the new LLM and business integrations.

Everything below was executed against the real app with the files from `scripts/seed_demo.py`; the outcomes quoted are what the app actually showed (sandbox, GloVe semantic search on). Screenshots from that run: `docs/screenshots/`. **Rehearse once on your machine before recording; OCR speed and exact confidence percentages can differ.**

## Before recording
```powershell
cd backend; .\.venv\Scripts\Activate.ps1
python ..\scripts\seed_demo.py                      # writes demo_files\ (11 files, deterministic)
Remove-Item -Recurse -Force data -ErrorAction SilentlyContinue      # start from an empty database
# optional meaning-search segment (one-time, ~66 MB):  python -m app.semantic download-glove ; $env:DOCINTEL_SEMANTIC = "glove"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# second terminal:  cd frontend; npm run dev   -> http://localhost:5173
```
Have ready: browser at 100% zoom on the app, a terminal in `backend` (for `python -m pytest tests -q`, about 20 s), `docs/benchmarks/RESULTS.md` and `docs/evaluation/EVALUATION.md` open in an editor, http://localhost:8000/docs in a tab.
Do **not** claim anything you have not run on your own machine (see "Say / don't say").

## Timeline
| time | do | say | expect on screen |
|---|---|---|---|
| 0:00 | Title screen / UI | The problem: thousands of invoices a day, manual checking. This system extracts, validates and makes them searchable. CPU only, no paid API, runs on this 8 GB laptop. Extraction is rule-based; every value shows its evidence. | – |
| 0:40 | Drag **all 11 files** from `demo_files` onto the drop zone | Multiple formats at once: digital PDF, scanned PDF, JPG, Excel (register and two-sheet workbook), CSV, PNG, plus a corrupt PDF and a Word file. | Upload report: 10 “queued”, `11_not_an_invoice.docx` **rejected: Unsupported extension '.docx'**. List shows progress bars, then statuses (see below). |
| 1:20 | Wait for processing; point at the list | Processing is background jobs with progress and retry. OCR is only used where there is no text layer. | `01` needs review, `02` completed, `03` completed, `04` needs review, `05` needs review, `06` needs review, `07` completed, `08` completed, `09` needs review, `10_corrupt.pdf` **failed** with a readable reason. |
| 1:50 | Open **01_clean_invoice.pdf** | Eight fields, each with its source text and a confidence. That confidence is a heuristic, not a calibrated probability: the review flags rely on the validation rules. Line items extracted. | Fields + 3 line items. Issues: **VAL-DUP-001** (same invoice as `05_duplicate_of_01.pdf`) and info **VAL-XDOC-002** “Verified against `06_invoice_register.xlsx`”. |
| 2:30 | Explain the two issues | Same invoice arrived twice: flagged on both files. The Excel register has a row for it and the values match, so the PDF is cross-verified against Excel. | – |
| 2:50 | Open **04_tampered_total.pdf** | Deterministic arithmetic, with currency precision and a rounding tolerance, not blind equality. | **VAL-SUB-001** “Subtotal 760.00 + tax 76.00 = 836.00, but total is 1086.00 (tolerance 0.01)” and **VAL-XDOC-001** “Does not match `06_invoice_register.xlsx` … total 1086.00 vs 836”. |
| 3:20 | **Edit** → Total = `836.00`, note “printed total wrong; confirmed with register” → **Save corrections** | Corrections are audited and validation re-runs automatically. | Field shows “corrected manually (was 1086.00)”, badge “1 correction(s)”, only **VAL-XDOC-002 verified** remains, status **completed**. |
| 3:50 | Open **06_invoice_register.xlsx** | The register's mismatch cleared by itself: related documents are re-validated. | Four invoices (one per row); the rows now show verified results. |
| 4:20 | Open **09_low_quality_scan.png** | A 60-dpi scan. The customer could not be read, so it is left empty, not invented, and flagged. One line has weak OCR confidence. | **VAL-REQ-001** customer missing; **VAL-OCR-001** “OCR confidence for line 1 is 44% (below 60%)”. |
| 4:40 | **Open original** → read “Soylent Foods” → close tab → **Edit** → Customer → Save | A reviewer can always check the source. | Original opens in a new tab; after saving, only the OCR warning remains. |
| 5:10 | **Mark reviewed** on the OCR warning | Reviewed warnings stay marked even if the document is re-validated. | Status → **completed**. |
| 5:30 | Search box → *Keywords*, type `Kestrel` → Search | Full-text search over everything extracted, with highlighted snippets. | 6 hits across PDFs and the register, `<mark>` highlights. |
| 5:50 | *(Optional, if semantic is on)* select **Meaning + keywords**, search `stand for notebook computer` | Optional meaning-based search. It is a lightweight baseline, not a transformer: on my evaluation it puts the right document first about half the time, and keyword search stays the reliable path. | `03_photo_invoice.jpg` first, chips “semantic” and “keyword”. **Rehearse queries first; other paraphrases on this small set missed.** |
| 6:15 | Show `10_corrupt.pdf` → **Retry all failed** | Failed jobs can be inspected and reprocessed; permanent errors are not retried forever. | Banner “Processing failed (corrupt_file)”; stays failed after retry. |
| 6:30 | Terminal: `python -m pytest tests -q`; then `docs/benchmarks/RESULTS.md`, `docs/evaluation/EVALUATION.md`, `/docs` | Automated tests; latest recorded Windows run: 130 passed, 1 skipped; API contract; measured benchmarks: **1,000 scanned pages in about 7 minutes in a 1-core sandbox, Python memory ~217 MB**; evaluation on synthetic data. | Latest Windows run: 130 passed, 1 skipped, 0 failed (18.10 s). Rerun before the demo. |
| 7:00 | Back to UI | Limits, honestly: evaluation is synthetic (upper bound); no LLM; rule-based extraction on unseen layouts will be weaker; not benchmarked at scale on this machine until you do; shared API key only. | – |

Optional beat (verified by an automated test, not in the screen recording): **Delete** `05_duplicate_of_01.pdf` → `01` loses its duplicate flag and becomes completed.

## Say / don't say
- Say: results are from synthetic data; sandbox benchmarks; confidence is heuristic; OCR errors are caught only partly (evaluation: 33–88% of extraction errors flagged depending on split and rule).
- Don't say: “99% accurate on invoices” without “synthetic”; “production ready”; any speed number you did not measure on this laptop.
- If OCR is slow on your machine, narrate the architecture while it runs, or start with the digital files and add the scans afterwards.
- If something fails on camera, show the failure panel: handling failures is part of the design.
