from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect, text

from trailforge.database.session import Database
from trailforge.models.audit import SchemaMigration


@dataclass(frozen=True)
class Migration:
    version: str
    description: str


MIGRATIONS = [
    Migration(version="0001", description="Initial TrailForge schema"),
    Migration(
        version="0002",
        description="Configurable eligibility policies, frozen decisions and outdoor experiences",
    ),
]

# Raw DDL for upgrading databases created under migration 0001. New databases
# get every table directly from SQLAlchemy metadata via create_all, so only the
# ALTER performed on an existing table is required here.
MIGRATION_STATEMENTS: dict[str, list[str]] = {
    "0002": [
        "ALTER TABLE expedition_registrations ADD COLUMN latest_decision_id INTEGER",
    ],
}


def initialize_database(database: Database) -> list[str]:
    database.create_schema()
    applied: list[str] = []
    with database.session() as session:
        known = {
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        }
        for migration in MIGRATIONS:
            if migration.version in known:
                continue
            for statement in MIGRATION_STATEMENTS.get(migration.version, []):
                _execute_if_needed(session, statement)
            session.add(
                SchemaMigration(
                    version=migration.version,
                    description=migration.description,
                )
            )
            applied.append(migration.version)
    return applied


def _execute_if_needed(session, statement: str) -> None:
    """Apply an idempotent DDL statement, skipping columns that already exist."""
    normalized = statement.strip().upper()
    if normalized.startswith("ALTER TABLE") and "ADD COLUMN" in normalized:
        table_name = statement.strip().split()[2]
        column_name = statement.strip().split(
            "ADD COLUMN", 1
        )[1].strip().split()[0]
        columns = {
            column["name"]
            for column in inspect(session.connection()).get_columns(table_name)
        }
        if column_name in columns:
            return
    session.execute(text(statement))


def migration_status(database: Database) -> dict[str, object]:
    inspector = inspect(database.engine)
    if "schema_migrations" not in inspector.get_table_names():
        return {
            "initialized": False,
            "applied": [],
            "pending": [item.version for item in MIGRATIONS],
        }
    with database.session() as session:
        applied = [
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        ]
    pending = [item.version for item in MIGRATIONS if item.version not in set(applied)]
    return {"initialized": True, "applied": applied, "pending": pending}


def assert_database_integrity(database: Database) -> dict[str, object]:
    with database.engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        foreign_key_rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    return {
        "integrity_check": str(integrity),
        "foreign_key_violations": [list(row) for row in foreign_key_rows],
        "healthy": integrity == "ok" and not foreign_key_rows,
    }
