# ADR-001: Modular Monolith as the Initial Backend Architecture

- Status: Accepted
- Date: 2026-11-16

## Context

The existing application uses FastAPI and already separates important capabilities such as document processing, extraction, validation, review, corrections, semantic search, and background jobs.

However, API setup and route handling are concentrated in the main application entry point. A full microservices conversion would add deployment and operational complexity before the product requires it.

## Decision

Retain FastAPI as a modular monolith.

Organize backend responsibilities into clearly defined API routers, application services, and existing domain modules. Keep the application deployable as one backend service during this foundation phase.

## Consequences

- Existing functionality can be preserved while modules are reorganized.
- Domain logic can be tested independently of HTTP routes.
- Internal module boundaries become clearer.
- A future service extraction remains possible if operational requirements justify it.
- The initial architecture avoids premature distributed-system complexity.

## Constraints

This decision does not imply that the application is already enterprise-ready or that its modules are fully isolated. Refactoring must be incremental and covered by regression tests.
