"""Load: stage, validate and atomically publish the star schema.

Everything happens inside one transaction: if staging, validation or publishing
fails, the transaction rolls back and Power BI keeps reading the previous data.
Azure SQL uses READ_COMMITTED_SNAPSHOT, so readers are never blocked meanwhile.
"""

import json
import logging
from datetime import datetime

from sqlalchemy.engine import Connection, Engine

from db import insert_rows
from transform import SNAPSHOT_COLUMNS, StarSchema

log = logging.getLogger(__name__)

# Dimensions first, fact last (insert order); reverse for deletes.
PUBLISH_ORDER = [
    "DimDate", "DimTime", "DimEventType", "DimUser", "DimPlan",
    "DimTraining", "DimPaymentStatus", "DimChurnRisk", "FactMemberActivity",
]


class ValidationError(RuntimeError):
    pass


def read_history(conn: Connection) -> list[dict]:
    result = conn.exec_driver_sql(f"SELECT {', '.join(SNAPSHOT_COLUMNS)} FROM hist.ChurnPredictionSnapshot")
    return [dict(zip(SNAPSHOT_COLUMNS, row)) for row in result.fetchall()]


def published_fact_count(conn: Connection) -> int:
    return conn.exec_driver_sql("SELECT COUNT(*) FROM dw.FactMemberActivity").scalar()


def stage(conn: Connection, star: StarSchema) -> dict[str, int]:
    counts = {}
    for name in PUBLISH_ORDER:
        table = star.tables[name]
        conn.exec_driver_sql(f"DELETE FROM stg.{name}")
        counts[name] = insert_rows(conn, f"stg.{name}", table.columns, table.rows)
    conn.exec_driver_sql("DELETE FROM stg.ChurnPredictionSnapshot")
    counts["ChurnPredictionSnapshot"] = insert_rows(
        conn, "stg.ChurnPredictionSnapshot", star.snapshots_to_stage.columns, star.snapshots_to_stage.rows
    )
    return counts


REQUIRED_SOURCES = ("users", "plans")
MAX_ORPHAN_RATIO = 0.01


def validate_staged(conn: Connection, star: StarSchema, previous_fact_count: int, min_ratio: float) -> None:
    problems = []

    for source in REQUIRED_SOURCES:
        if not star.source_counts.get(source):
            problems.append(f"source collection '{source}' returned no documents")

    total_facts = len(star.tables["FactMemberActivity"].rows)
    if total_facts and star.orphan_rows / total_facts > MAX_ORPHAN_RATIO:
        problems.append(
            f"{star.orphan_rows} of {total_facts} fact rows reference missing users/plans "
            f"(above {MAX_ORPHAN_RATIO:.0%} tolerance)"
        )

    for name in PUBLISH_ORDER:
        staged = conn.exec_driver_sql(f"SELECT COUNT(*) FROM stg.{name}").scalar()
        if staged != len(star.tables[name].rows):
            problems.append(f"stg.{name}: staged {staged}, expected {len(star.tables[name].rows)}")
        if staged == 0:
            problems.append(f"stg.{name} is empty")

    by_type = dict(conn.exec_driver_sql(
        "SELECT e.EventType, COUNT(*) FROM stg.FactMemberActivity f "
        "JOIN stg.DimEventType e ON e.EventTypeKey = f.EventTypeKey GROUP BY e.EventType"
    ).fetchall())
    for event_type, expected in star.expected_fact_counts.items():
        if by_type.get(event_type, 0) != expected:
            problems.append(f"{event_type}: staged {by_type.get(event_type, 0)}, source {expected}")

    duplicates = conn.exec_driver_sql(
        "SELECT COUNT(*) FROM (SELECT SourceEventId FROM stg.FactMemberActivity GROUP BY SourceEventId HAVING COUNT(*) > 1) d"
    ).scalar()
    if duplicates:
        problems.append(f"{duplicates} duplicated SourceEventId values")

    new_fact_count = len(star.tables["FactMemberActivity"].rows)
    if previous_fact_count and new_fact_count < previous_fact_count * min_ratio:
        problems.append(
            f"new load has {new_fact_count} fact rows, below {min_ratio:.0%} of the "
            f"{previous_fact_count} currently published (safety threshold)"
        )

    if problems:
        raise ValidationError("Staging validation failed: " + "; ".join(problems))


def publish(conn: Connection, star: StarSchema) -> None:
    snapshot_columns = star.snapshots_to_stage.columns
    updates = ", ".join(f"t.{c} = s.{c}" for c in snapshot_columns if c not in ("SnapshotDate", "UserIdMongo"))
    conn.exec_driver_sql(
        "MERGE hist.ChurnPredictionSnapshot AS t "
        "USING stg.ChurnPredictionSnapshot AS s "
        "ON t.SnapshotDate = s.SnapshotDate AND t.UserIdMongo = s.UserIdMongo "
        f"WHEN MATCHED THEN UPDATE SET {updates} "
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(snapshot_columns)}) "
        f"VALUES ({', '.join('s.' + c for c in snapshot_columns)});"
    )

    for name in reversed(PUBLISH_ORDER):
        conn.exec_driver_sql(f"DELETE FROM dw.{name}")
    for name in PUBLISH_ORDER:
        columns = ", ".join(f"[{c}]" for c in star.tables[name].columns)
        order = " ORDER BY SourceEventId" if name == "FactMemberActivity" else ""
        conn.exec_driver_sql(f"INSERT INTO dw.{name} ({columns}) SELECT {columns} FROM stg.{name}{order}")


def load(engine: Engine, star: StarSchema, previous_fact_count: int, min_ratio: float) -> dict[str, int]:
    with engine.begin() as conn:
        counts = stage(conn, star)
        validate_staged(conn, star, previous_fact_count, min_ratio)
        publish(conn, star)
    return counts


def write_log(engine: Engine, started: datetime, finished: datetime, status: str, message: str | None, counts: dict) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO etl.LoadLog (StartedAt, FinishedAt, Status, Message, RowCounts) VALUES (%s, %s, %s, %s, %s)",
            (started, finished, status, (message or "")[:4000] or None, json.dumps(counts) if counts else None),
        )
