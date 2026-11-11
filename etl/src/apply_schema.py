"""Create (or verify) the data warehouse schema in Azure SQL.

Usage:
    python src/apply_schema.py
"""

import logging
import re
from pathlib import Path

from config import load_settings
from db import azure_engine, wait_for_azure

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
GO_SEPARATOR = re.compile(r"^\s*GO\s*$", re.MULTILINE | re.IGNORECASE)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    engine = azure_engine(settings)
    wait_for_azure(engine)

    for script in sorted(SQL_DIR.glob("*.sql")):
        batches = [b.strip() for b in GO_SEPARATOR.split(script.read_text(encoding="utf-8")) if b.strip()]
        with engine.begin() as conn:
            for batch in batches:
                conn.exec_driver_sql(batch)
        logging.info("Applied %s (%d batches)", script.name, len(batches))

    with engine.connect() as conn:
        tables = conn.exec_driver_sql(
            "SELECT s.name + '.' + t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name IN ('dw', 'stg', 'hist', 'etl') ORDER BY 1"
        ).scalars().all()
    logging.info("Tables present (%d): %s", len(tables), ", ".join(tables))


if __name__ == "__main__":
    main()
