# ADR-004: Reliable Background Document Processing

- Status: Accepted
- Date: 2026-11-16

## Context

Document processing includes file reading, OCR, extraction, validation, and indexing. These operations can take significant time and may fail because of process crashes, resource constraints, or transient errors.

The existing worker and queue provide a foundation, while the roadmap identifies job leases, heartbeats, safer recovery, retry classification, and graceful shutdown as required improvements.

## Decision

Keep document processing asynchronous and separate from request-response handling.

Evolve job execution toward lease-based ownership with heartbeats and explicit recovery of expired jobs. Classify retryable and permanent failures, and implement controlled worker shutdown.

Preserve successful document generations when a later processing attempt fails. A new result must become active only after successful completion.

## Consequences

- Long-running processing does not need to block API requests.
- Expired jobs can be recovered after worker failure.
- Lease ownership reduces the risk of concurrent workers processing the same job.
- Retry behavior becomes more predictable.
- Previously successful document results remain available during failed reprocessing attempts.

## Constraints

Job recovery, concurrency, failure handling, and shutdown behavior must be covered by automated tests before being considered production-ready.
