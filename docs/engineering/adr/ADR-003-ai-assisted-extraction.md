# ADR-003: AI-Assisted Extraction with Deterministic Validation

- Status: Accepted
- Date: 2026-11-16

## Context

DocIntel currently extracts invoice information using deterministic processing and validates financial fields using explicit business rules.

The platform roadmap introduces optional AI-assisted extraction. AI-generated values may be incomplete, ambiguous, or incorrect and must not bypass financial validation.

## Decision

Introduce an independent extraction-provider interface.

Support optional AI-assisted structured extraction with field-level evidence and confidence information. Retain the existing deterministic extractor as a fallback provider.

Keep deterministic financial validation authoritative regardless of which extraction provider supplies the candidate values.

Route uncertain, conflicting, or insufficiently supported fields into human review.

Evaluate extraction quality, latency, operational cost, and fallback behavior before enabling an AI provider in production.

## Consequences

- Extraction providers can evolve independently of validation rules.
- AI capabilities remain optional rather than becoming a hard runtime dependency.
- Field-level evidence supports review and traceability.
- Deterministic fallback preserves a usable processing path when an AI provider is unavailable.
- Provider quality and cost can be compared through measurable evaluations.

## Constraints

AI output must never be treated as verified financial truth. Existing validation and review requirements remain in force.
