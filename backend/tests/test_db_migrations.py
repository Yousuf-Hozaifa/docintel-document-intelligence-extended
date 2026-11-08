import subprocess
import sys
from pathlib import Path


def test_legacy_database_migration():
    """Verify legacy migration, data preservation and repeatability."""

    backend_dir = Path(__file__).resolve().parents[1]
    script = backend_dir / "test_legacy_migration.py"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr

    assert result.returncode == 0, output

    expected_checks = [
        "PASS: Repeated database initialization",
        "PASS: document preserved",
        "PASS: page preserved",
        "PASS: invoice preserved",
        "PASS: line item preserved",
        "PASS: validation issue preserved",
        "PASS: correction preserved",
        "PASS: semantic chunk preserved",
        "PASS: invoice number key migrated",
        "PASS: FTS search works",
        "PASS: foreign keys valid",
        "Legacy migration test PASSED.",
    ]

    for check in expected_checks:
        assert check in output, f"Missing expected check: {check}"

def test_missing_fts_index_is_rebuilt():
    """Verify that a missing FTS index is rebuilt without duplicate rows."""

    backend_dir = Path(__file__).resolve().parents[1]
    script = backend_dir / "test_missing_fts.py"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr

    assert result.returncode == 0, output

    expected_checks = [
        "PASS: Generation 1 searchable",
        "PASS: Generation 2 searchable with correct generation ID",
        "PASS: Exactly two page records indexed",
        "Missing FTS recovery test PASSED.",
    ]

    for check in expected_checks:
        assert check in output, f"Missing expected check: {check}"