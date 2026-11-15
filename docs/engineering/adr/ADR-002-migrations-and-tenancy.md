# ADR-002: Versioned Migrations and Tenant-Scoped Data Access

- Status: Accepted
- Date: 2026-11-16

## Context

The current application uses SQLite and maintains database schema changes through initialization-time migration logic. The platform roadmap requires organizations, users, memberships, roles, and tenant-owned documents.

Without explicit tenant-scoped access, adding organization records alone would not provide data isolation.

## Decision

Introduce versioned, ordered database migrations as the authoritative mechanism for future schema changes.

Retain SQLite during the initial platform development phase. Design the persistence layer so that database-specific behavior can be isolated and a future database migration remains feasible.

Introduce organization ownership and enforce tenant scope at the data-access and service boundaries. Authorization must be checked server-side for every tenant-owned resource.

## Consequences

- Schema changes become explicit, reviewable, and testable.
- Existing databases require a safe migration path.
- Tenant isolation becomes a backend responsibility rather than a frontend filter.
- Cross-tenant access tests become mandatory.
- SQLite remains suitable for the current development and deployment scope, but its production limitations must be evaluated before scaling.

## Constraints

Do not claim multi-tenant security until tenant-scoped access controls and cross-tenant isolation tests are implemented.
