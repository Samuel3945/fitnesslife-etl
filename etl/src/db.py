"""Connections to MongoDB (source) and Azure SQL (target)."""

import logging
import time

from pymongo import MongoClient
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import OperationalError

from config import Settings

log = logging.getLogger(__name__)

# SQL Server allows at most 2100 parameters per statement.
MAX_PARAMS_PER_STATEMENT = 2000


def mongo_database(settings: Settings):
    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=30000, tz_aware=False)
    return client, client[settings.mongodb_db]


def azure_engine(settings: Settings) -> Engine:
    url = URL.create(
        "mssql+pymssql",
        username=settings.azure_user,
        password=settings.azure_password,
        host=settings.azure_server,
        database=settings.azure_database,
    )
    # hide_parameters keeps row values (emails, IDs, phones) out of error messages and logs.
    return create_engine(
        url,
        hide_parameters=True,
        connect_args={"login_timeout": 90, "timeout": 300, "tds_version": "7.4"},
    )


def error_summary(exc: BaseException, limit: int = 1000) -> str:
    """Short, data-free description of an exception for logs and etl.LoadLog."""
    original = getattr(exc, "orig", None) or exc
    return f"{type(original).__name__}: {str(original).splitlines()[0] if str(original) else ''}"[:limit]


def wait_for_azure(engine: Engine, attempts: int = 6, delay_seconds: int = 30) -> None:
    """The serverless database auto-pauses; the first connection wakes it and may fail meanwhile."""
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return
        except OperationalError as exc:
            if attempt == attempts:
                raise
            log.warning("Azure SQL not ready (attempt %d/%d): %s", attempt, attempts, error_summary(exc, 200))
            time.sleep(delay_seconds)


def insert_rows(conn: Connection, table: str, columns: list[str], rows: list[tuple]) -> int:
    """Insert rows using multi-row VALUES statements, chunked under the parameter limit."""
    if not rows:
        return 0
    per_row = len(columns)
    chunk_size = max(1, min(1000, MAX_PARAMS_PER_STATEMENT // per_row))
    column_list = ", ".join(f"[{c}]" for c in columns)
    row_placeholder = "(" + ", ".join(["%s"] * per_row) + ")"
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        sql = f"INSERT INTO {table} ({column_list}) VALUES " + ", ".join([row_placeholder] * len(chunk))
        params = tuple(value for row in chunk for value in row)
        conn.exec_driver_sql(sql, params)
    return len(rows)
