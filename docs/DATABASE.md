# Database schema

_Base schema from `backend/app/db.py` plus versioned migrations in `backend/app/db_migrations/`. SQLite in WAL mode; one short transaction per unit of work._

## Entity-relationship diagram

```mermaid
erDiagram
  tenants {
    INTEGER id PK
  }
  documents {
    INTEGER id PK
    INTEGER tenant_id FK
    TEXT filename
    TEXT stored_name
    TEXT sha256
    TEXT file_type
    INTEGER size_bytes
    TEXT status
    TEXT error_code
    TEXT error_message
    INTEGER page_count
    INTEGER pages_failed
    INTEGER review_required
    TEXT created_at
    TEXT updated_at
  }
  jobs {
    INTEGER id PK
    INTEGER document_id FK
    TEXT status
    INTEGER attempts
    INTEGER max_attempts
    REAL run_after
    INTEGER progress_done
    INTEGER progress_total
    TEXT error
    TEXT created_at
    TEXT started_at
    TEXT finished_at
  }
  pages {
    INTEGER id PK
    INTEGER document_id FK
    INTEGER page_no
    TEXT label
    TEXT method
    REAL ocr_confidence
    TEXT text
    TEXT error
  }
  invoices {
    INTEGER id PK
    INTEGER document_id FK
    INTEGER seq
    INTEGER page_start
    INTEGER page_end
    TEXT fields_json
    TEXT invoice_number
    TEXT invoice_date
    TEXT supplier
    TEXT customer
    TEXT subtotal
    TEXT tax
    TEXT total
    TEXT currency
    TEXT dup_key
    TEXT num_key
    INTEGER needs_review
    TEXT extractor
  }
  line_items {
    INTEGER id PK
    INTEGER invoice_id FK
    INTEGER idx
    TEXT description
    TEXT quantity
    TEXT unit_price
    TEXT amount
    TEXT evidence
    REAL ocr_conf
  }
  validation_issues {
    INTEGER id PK
    INTEGER invoice_id FK
    TEXT rule_id
    TEXT severity
    TEXT field
    TEXT message
    INTEGER resolved
  }
  corrections {
    INTEGER id PK
    INTEGER invoice_id FK
    TEXT field
    TEXT old_value
    TEXT new_value
    TEXT note
    TEXT created_at
  }
  chunks {
    INTEGER id PK
    INTEGER document_id FK
    INTEGER page_no
    INTEGER chunk_idx
    TEXT text
    TEXT model
    INTEGER dim
    BLOB embedding
  }
  suppliers {
    INTEGER id PK
    INTEGER tenant_id FK
    TEXT name
    TEXT tax_id
    TEXT email
    TEXT phone
    TEXT payment_terms
    TEXT created_at
    TEXT updated_at
  }
  purchase_orders {
    INTEGER id PK
    INTEGER tenant_id FK
    INTEGER supplier_id FK
    TEXT po_number
    TEXT supplier_name
    TEXT currency
    TEXT total
    TEXT status
    TEXT created_at
  }
  purchase_order_lines {
    INTEGER id PK
    INTEGER purchase_order_id FK
    INTEGER idx
    TEXT description
    TEXT quantity
    TEXT unit_price
    TEXT amount
  }
  invoice_approvals {
    INTEGER id PK
    INTEGER tenant_id FK
    INTEGER invoice_id FK
    TEXT status
    TEXT submitted_by
    TEXT approved_by
    TEXT note
    TEXT updated_at
  }
  mapping_templates {
    INTEGER id PK
    INTEGER tenant_id FK
    TEXT name
    TEXT mapping_json
    TEXT created_at
    TEXT updated_at
  }
  events {
    INTEGER id PK
    INTEGER document_id FK
    TEXT ts
    TEXT event
    TEXT detail
  }
  documents ||--o{ jobs : "has"
  documents ||--o{ pages : "has"
  documents ||--o{ invoices : "has"
  invoices ||--o{ line_items : "has"
  invoices ||--o{ validation_issues : "has"
  invoices ||--o{ corrections : "has"
  documents ||--o{ chunks : "has"
  documents ||--o{ events : "has"
  tenants ||--o{ documents : "owns"
  tenants ||--o{ suppliers : "owns"
  suppliers ||--o{ purchase_orders : "supplier"
  tenants ||--o{ purchase_orders : "owns"
  purchase_orders ||--o{ purchase_order_lines : "contains"
  tenants ||--o{ invoice_approvals : "owns"
  invoices ||--o| invoice_approvals : "approval"
  tenants ||--o{ mapping_templates : "owns"
```

`page_fts` is an FTS5 virtual table (`text`, plus unindexed `document_id`, `page_no`; Porter stemming) that powers keyword search. SQLite cannot cascade deletes into it, so the API removes its rows explicitly.

## Tables

### `documents`

One row per uploaded file: name, SHA-256 (duplicate-upload protection), detected type, status, page counters, error code/message, review flag.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| filename | TEXT | NOT NULL |
| stored_name | TEXT | NOT NULL |
| sha256 | TEXT | NOT NULL |
| file_type | TEXT |  |
| size_bytes | INTEGER | NOT NULL |
| status | TEXT | NOT NULL, default 'queued' |
| error_code | TEXT |  |
| error_message | TEXT |  |
| page_count | INTEGER |  |
| pages_failed | INTEGER | NOT NULL, default 0 |
| review_required | INTEGER | NOT NULL, default 0 |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

Indexes: `ix_documents_status` (status), `ix_documents_sha` (sha256)

### `jobs`

Background-processing queue and history: status, attempts/max_attempts, `run_after` (retry backoff), progress counters. A document can have several jobs (reprocessing).

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| document_id | INTEGER | → documents.id (ON DELETE CASCADE), NOT NULL |
| status | TEXT | NOT NULL, default 'queued' |
| attempts | INTEGER | NOT NULL, default 0 |
| max_attempts | INTEGER | NOT NULL |
| run_after | REAL | NOT NULL, default 0 |
| progress_done | INTEGER | NOT NULL, default 0 |
| progress_total | INTEGER |  |
| error | TEXT |  |
| created_at | TEXT | NOT NULL |
| started_at | TEXT |  |
| finished_at | TEXT |  |

Indexes: `ix_jobs_claim` (status, run_after)

### `pages`

Extracted text per page/sheet with the method used (text layer, OCR, sheet, csv) and OCR mean confidence. Failed pages are kept with their error.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| document_id | INTEGER | → documents.id (ON DELETE CASCADE), NOT NULL |
| page_no | INTEGER | NOT NULL |
| label | TEXT |  |
| method | TEXT |  |
| ocr_confidence | REAL |  |
| text | TEXT |  |
| error | TEXT |  |

### `invoices`

One row per invoice found in a document (a PDF or workbook can hold many). `fields_json` keeps the full extraction (value, confidence, evidence, OCR word confidence); the plain columns are the current values used for search/filters. `num_key` = normalised invoice number for cross-document matching.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| document_id | INTEGER | → documents.id (ON DELETE CASCADE), NOT NULL |
| seq | INTEGER | NOT NULL |
| page_start | INTEGER | NOT NULL |
| page_end | INTEGER | NOT NULL |
| fields_json | TEXT | NOT NULL |
| invoice_number | TEXT |  |
| invoice_date | TEXT |  |
| supplier | TEXT |  |
| customer | TEXT |  |
| subtotal | TEXT |  |
| tax | TEXT |  |
| total | TEXT |  |
| currency | TEXT |  |
| dup_key | TEXT |  |
| num_key | TEXT |  |
| needs_review | INTEGER | NOT NULL, default 0 |
| extractor | TEXT | NOT NULL |

Indexes: `ix_invoices_numkey` (num_key), `ix_invoices_dup` (dup_key), `ix_invoices_doc` (document_id)

### `line_items`

Line items of an invoice (values kept as decimal strings); `evidence` is the source line; `ocr_conf` the weakest OCR word (cleared when a human edits the row).

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| invoice_id | INTEGER | → invoices.id (ON DELETE CASCADE), NOT NULL |
| idx | INTEGER | NOT NULL |
| description | TEXT |  |
| quantity | TEXT |  |
| unit_price | TEXT |  |
| amount | TEXT |  |
| evidence | TEXT |  |
| ocr_conf | REAL |  |

### `validation_issues`

Output of the deterministic rule engine, one row per finding with a stable `rule_id`; `resolved` marks human review.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| invoice_id | INTEGER | → invoices.id (ON DELETE CASCADE), NOT NULL |
| rule_id | TEXT | NOT NULL |
| severity | TEXT | NOT NULL |
| field | TEXT |  |
| message | TEXT | NOT NULL |
| resolved | INTEGER | NOT NULL, default 0 |

### `corrections`

Audit trail of manual edits: field, old value, new value, note, timestamp.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| invoice_id | INTEGER | → invoices.id (ON DELETE CASCADE), NOT NULL |
| field | TEXT | NOT NULL |
| old_value | TEXT |  |
| new_value | TEXT |  |
| note | TEXT |  |
| created_at | TEXT | NOT NULL |

### `chunks`

Optional semantic index: text chunks with an embedding BLOB (float32) and the model name that produced it.

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| document_id | INTEGER | → documents.id (ON DELETE CASCADE), NOT NULL |
| page_no | INTEGER | NOT NULL |
| chunk_idx | INTEGER | NOT NULL |
| text | TEXT | NOT NULL |
| model | TEXT | NOT NULL |
| dim | INTEGER | NOT NULL |
| embedding | BLOB | NOT NULL |

Indexes: `ix_chunks_model` (model), `ix_chunks_doc` (document_id)

### `events`

Processing history per document (uploaded, read_started, page_failed, completed, retry_scheduled, corrected, ...).

| column | type | notes |
|---|---|---|
| id | INTEGER | primary key |
| document_id | INTEGER | → documents.id (ON DELETE CASCADE), NOT NULL |
| ts | TEXT | NOT NULL |
| event | TEXT | NOT NULL |
| detail | TEXT |  |

### Business workflow tables (migration v004)

The following tenant-scoped tables support the investor-demo business workflows:

- `suppliers`: supplier master records, including optional tax ID, contact details and payment terms. Supplier names are auto-created from extracted invoice data.
- `purchase_orders`: demo or manually entered purchase orders linked to a supplier, with currency, total and status.
- `purchase_order_lines`: line-level PO details for later three-way matching.
- `invoice_approvals`: one approval state per invoice (`pending_review`, `approved`, `rejected`), with actor, note and update timestamp. Approval events are also written to the document event log.
- `mapping_templates`: reusable company-specific spreadsheet column mappings stored as JSON.

All business endpoints scope reads and writes by `tenant_id`. The purchase-order demo currently compares supplier, currency and total; it does not yet perform full line-level goods-received matching.
## Design notes

- **Why SQLite (not PostgreSQL/pgvector) for now:** one cohesive CPU-first deployment, zero services to run on an 8 GB Windows machine. Measured limits and the point where a move is justified are in `docs/SEMANTIC.md` (brute-force vectors up to ~250k chunks) and `docs/benchmarks/`.
- **Money is stored as decimal strings**, never floats; arithmetic uses Python `Decimal` with per-currency precision.
- **Denormalised columns on `invoices`** (number, date, supplier, customer, totals, currency) mirror `fields_json` so search/filters need no JSON parsing; they are updated together by the correction code.
- **Concurrency:** WAL journal, `BEGIN IMMEDIATE` transactions, jobs claimed atomically (`UPDATE … WHERE status='queued'` inside one transaction), so two workers never run the same job.
- **Migrations:** `init_db()` creates the base schema and applies ordered versioned migrations (v001–v004); migrations are transactional and idempotent.
- **Deletion:** `DELETE /api/documents/{id}` removes the file, FTS rows and (via cascade) everything derived from it.
