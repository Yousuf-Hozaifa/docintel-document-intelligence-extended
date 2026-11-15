# Engineering Baseline

## Verification date
Latest confirmed extended-branch regression run (before merge into extended/master).

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
- Passed: 133
- Skipped: 1
- Failed: 0
- Warnings: 2
- Duration: 21.88 seconds

The tested feature branch was subsequently fast-forwarded into the extended repository's master branch without code changes.

## Warnings
1. PyMuPDF: deprecated ByteString usage.
2. Starlette: deprecation warning concerning httpx in TestClient.

These warnings did not affect the test result.

## Git checkpoint
- Branch: feature/platform-foundation
- Remote: extended
- Working tree: clean at the last confirmed checkpoint.

## Engineering policy
- Preserve existing functionality while refactoring.
- Keep deterministic validation authoritative.
- Add regression tests for behavioral changes.
- Do not claim enterprise or multi-tenant readiness until the relevant security and isolation requirements are implemented and tested.
