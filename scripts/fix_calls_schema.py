from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Call

load_dotenv(ROOT / ".env")


def resolve_sqlite_db_path(database_url: str) -> Path:
    """Resolve the actual SQLite file path from SQLAlchemy DATABASE_URL."""
    if not database_url.startswith("sqlite"):
        raise RuntimeError(f"This migration is only for SQLite; got: {database_url}")

    if database_url.startswith("sqlite:///./"):
        relative = database_url.replace("sqlite:///./", "", 1)
        return (ROOT / relative).resolve()
    if database_url.startswith("sqlite:///"):
        relative = database_url.replace("sqlite:///", "", 1)
        return (ROOT / relative).resolve()
    if database_url.startswith("sqlite://"):
        relative = database_url.replace("sqlite://", "", 1)
        return (ROOT / relative).resolve()

    raise RuntimeError(f"Unsupported SQLite URL format: {database_url}")


def sqlite_type_for(column) -> str:
    """Map a SQLAlchemy column to the SQLite type string used in ALTER TABLE."""
    from sqlalchemy import Boolean, DateTime, Integer, String, Text

    col_type = column.type
    if col_type.__class__.__name__ == "GUID":
        return "CHAR(36)"
    if isinstance(col_type, Text):
        return "TEXT"
    if isinstance(col_type, String):
        return f"VARCHAR({col_type.length})" if col_type.length else "VARCHAR"
    if isinstance(col_type, Integer):
        return "INTEGER"
    if isinstance(col_type, Boolean):
        return "BOOLEAN"
    if isinstance(col_type, DateTime):
        return "DATETIME"
    type_name = str(col_type).upper()
    if "TEXT" in type_name:
        return "TEXT"
    if "INTEGER" in type_name:
        return "INTEGER"
    if "DATETIME" in type_name:
        return "DATETIME"
    return type_name


def column_sql_definition(column) -> str:
    column_type = sqlite_type_for(column)
    nullable = " NOT NULL" if not column.nullable else ""
    default = ""

    if column.default is not None and not callable(column.default):
        default_value = column.default.arg if hasattr(column.default, "arg") else column.default
        default = f" DEFAULT {default_value}"
    elif column.server_default is not None:
        default_sql = str(column.server_default.arg)
        default = f" DEFAULT {default_sql}"

    return f"{column_type}{nullable}{default}"


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./voice_lead_tracker.db")
    db_path = resolve_sqlite_db_path(database_url)
    model_columns = {column.name: column for column in Call.__table__.columns}

    print(f"Database URL: {database_url}")
    print(f"Resolved SQLite file: {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    existing = {row[1] for row in cur.execute("PRAGMA table_info(calls)").fetchall()}

    missing = [name for name in model_columns if name not in existing]
    print("Columns currently present in 'calls':")
    print(sorted(existing))
    print("Model columns missing from the table:")
    print(missing)

    if not missing:
        print("No schema changes required.")
        conn.close()
        return

    for name in missing:
        column = model_columns[name]
        definition = column_sql_definition(column)
        print(f"Adding column: {name} {definition}")
        cur.execute(f"ALTER TABLE calls ADD COLUMN {name} {definition}")

    conn.commit()
    print("Schema update complete.")
    print("Current PRAGMA table_info(calls):")
    for row in cur.execute("PRAGMA table_info(calls)").fetchall():
        print(row)
    conn.close()


if __name__ == "__main__":
    main()
