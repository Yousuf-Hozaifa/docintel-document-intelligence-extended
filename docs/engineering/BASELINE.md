# Engineering Baseline

## Verification date
2026-11-08

## Environment
- Development environment: Windows
- Backend: FastAPI
- Database: SQLite
- Frontend: React and Vite
- Test runner: pytest

## Baseline test results

Command:
`python -m pytest tests -q`

Results:
- Passed: 130
- Skipped: 1
- Failed: 0
- Warnings: 2
- Duration: 20.20 seconds

## Warnings
1. PyMuPDF: deprecated ByteString usage.
2. Starlette: deprecation warning concerning httpx in TestClient.

These warnings did not affect the test result.

## Git checkpoint
- Branch: feature/platform-foundation
- Remote: extended
- Working tree: clean

## Engineering policy
- Preserve existing functionality while refactoring.
- Keep deterministic validation authoritative.
- Add regression tests for behavioral changes.
- Do not claim enterprise or multi-tenant readiness until the relevant security and isolation requirements are implemented and tested.
