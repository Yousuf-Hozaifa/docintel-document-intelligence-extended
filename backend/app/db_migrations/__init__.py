
"""Versioned SQLite database migration runner."""

from datetime import datetime, timezone
from importlib import import_module
from pkgutil import iter_modules


def run_migrations(conn) -> None:
    """Discover and apply unapplied migrations in version order."""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)

    migrations = []

    for item in iter_modules(__path__):
        if not item.name.startswith("v"):
            continue

        version = int(item.name.split("_", 1)[0][1:])
        module = import_module(f"{__name__}.{item.name}")

        if not callable(getattr(module, "upgrade", None)):
            raise RuntimeError(
                f"Migration {item.name} has no upgrade(conn) function"
            )

        migrations.append((version, item.name, module))

    migrations.sort(key=lambda item: item[0])

    for version, name, module in migrations:
        conn.execute("BEGIN IMMEDIATE")

        try:
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",
                (version,),
            ).fetchone()

            if applied:
                conn.execute("COMMIT")
                continue

            module.upgrade(conn)

            conn.execute(
                """
                INSERT INTO schema_migrations (
                    version, name, applied_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    version,
                    name,
                    datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                ),
            )

            conn.execute("COMMIT")

        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
