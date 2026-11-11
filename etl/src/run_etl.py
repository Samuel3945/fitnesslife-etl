"""Run the full ETL: MongoDB -> star schema -> Azure SQL.

Usage:
    python src/run_etl.py

Exit code is 0 on success and 1 on failure (the previous data stays published).
"""

import logging
import sys
from datetime import datetime

from config import load_settings
from db import azure_engine, error_summary, mongo_database, wait_for_azure
from extract import extract_all
from load import load, published_fact_count, read_history, write_log
from transform import build_star_schema


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    now = lambda: datetime.now(settings.local_timezone).replace(tzinfo=None, microsecond=0)  # noqa: E731
    started = now()

    engine = azure_engine(settings)
    wait_for_azure(engine)
    counts: dict = {}
    published = False
    try:
        client, db = mongo_database(settings)
        try:
            data = extract_all(db)
        finally:
            client.close()
        logging.info("Extracted: %s", {name: len(docs) for name, docs in data.items()})

        with engine.connect() as conn:
            history = read_history(conn)
            previous_fact_count = published_fact_count(conn)
        logging.info("Churn history rows already stored: %d", len(history))

        star = build_star_schema(data, history, settings)
        for warning in star.warnings:
            logging.warning(warning)

        counts = load(engine, star, previous_fact_count, settings.min_publish_ratio)
        published = True
        counts["facts_by_event_type"] = star.expected_fact_counts
        logging.info("Published: %s", counts)
        write_log(engine, started, now(), "SUCCESS", "; ".join(star.warnings) or None, counts)
        return 0
    except Exception as exc:
        summary = error_summary(exc)
        if published:
            logging.error("Data was published, but writing the load log failed: %s", summary)
            return 0
        logging.error("ETL failed; previously published data was kept: %s", summary)
        try:
            write_log(engine, started, now(), "FAILED", summary, counts)
        except Exception as log_exc:
            logging.error("Could not write the load log: %s", error_summary(log_exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
